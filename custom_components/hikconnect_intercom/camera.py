"""Camera entities for Hik-Connect Intercom — native CPD7 LAN stream.

One entity per real channel (door station).  The station only serves a couple of
concurrent CPD7 streams, so each camera keeps **one** shared upstream connection
open while at least one consumer (live viewer or snapshot) is active and
broadcasts the decoded H.264 to all of them.  N browsers viewing the same camera
therefore cost one station connection, not N.

Pipeline (all local, no cloud/phone/frida):
  Cpd7LanClient (9010/9020, AES-128 control key from CAS, per-channel)
    -> HikStreamDecoder (strip $01 framing + 12B RTP + 13B Hik header -> H.264)
    -> per-viewer ffmpeg (H.264 -> MJPEG) -> browser / snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import logging

from aiohttp import web
from homeassistant.components.camera import Camera
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MJPEG_FPS, MJPEG_HEIGHT, MJPEG_QUALITY, MJPEG_WIDTH
from .hikconnect_api import HikCamera
from .lib.hik_decoder import HikStreamDecoder
from .lib.lan_client import ControlKeyError, Cpd7LanClient

_LOGGER = logging.getLogger(__name__)

_SC = b"\x00\x00\x00\x01"     # H.264 Annex-B start code
_MJPEG_BOUNDARY = "ffmpeg"
_MAX_EMPTY_READS = 3
_SNAPSHOT_TTL = 10.0          # reuse a recent still instead of re-opening a stream
_MAX_STREAMS_PER_DEVICE = 2   # concurrent *upstreams* (channels), not viewers
_ACQUIRE_TIMEOUT = 6.0
_LINGER_SEC = 4.0             # hold the upstream briefly after the last viewer leaves
_SUB_QUEUE_MAX = 240          # bounded per-viewer backlog; slow viewers drop frames


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client = data["client"]
    quality = data["quality"]
    status = data["status_coordinator"]
    sems: dict[str, asyncio.Semaphore] = {}
    entities = []
    for cam in data["cameras"]:
        sem = sems.setdefault(cam.serial, asyncio.Semaphore(_MAX_STREAMS_PER_DEVICE))
        entities.append(HikLocalCamera(hass, client, cam, sem, quality, status))
    async_add_entities(entities)


class _ChannelStream:
    """One shared CPD7 upstream for a single camera channel, fanned out to N viewers.

    HA calls the camera's snapshot/stream handlers once per viewer; each used to
    open its own station socket, exhausting the station's few stream slots.  This
    keeps exactly one upstream open while >=1 consumer is subscribed and broadcasts
    the decoded H.264 to every subscriber's queue.
    """

    def __init__(
        self, hass: HomeAssistant, client, cam: HikCamera, sem: asyncio.Semaphore,
        quality: dict[str, str], qkey: str, status,
    ) -> None:
        self._hass = hass
        self._client = client
        self._cam = cam
        self._sem = sem
        self._quality = quality
        self._qkey = qkey
        self._status = status
        self._ip: str | None = None
        self._key: str | None = None
        self._lan: Cpd7LanClient | None = None
        self._pump: asyncio.Task | None = None
        self._stopping = False
        self._subs: set[asyncio.Queue] = set()
        self._sps = b""
        self._pps = b""
        self._lock = asyncio.Lock()
        self._linger: asyncio.TimerHandle | None = None

    # -- subscription -----------------------------------------------------
    async def subscribe(self) -> asyncio.Queue | None:
        """Attach a consumer, opening the shared upstream on the first one.

        Returns a queue that yields Annex-B H.264 (and finally ``None`` when the
        upstream ends), or ``None`` if no live feed could be opened.
        """
        async with self._lock:
            if self._linger is not None:
                self._linger.cancel()
                self._linger = None
            if self._lan is not None and self._pump is not None and self._pump.done():
                await self._teardown()  # upstream died — reopen a fresh one
            if self._lan is None and not await self._open():
                return None
            q: asyncio.Queue = asyncio.Queue(maxsize=_SUB_QUEUE_MAX)
            if self._sps and self._pps:  # prime late joiners with SPS/PPS
                q.put_nowait((False, self._sps + self._pps))
            self._subs.add(q)
            return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subs.discard(q)
            if self._subs or self._lan is None:
                return
            loop = asyncio.get_running_loop()
            self._linger = loop.call_later(
                _LINGER_SEC, lambda: asyncio.create_task(self._linger_teardown())
            )

    async def _linger_teardown(self) -> None:
        async with self._lock:
            self._linger = None
            if not self._subs:
                await self._teardown()

    # -- upstream lifecycle ----------------------------------------------
    async def _open(self) -> bool:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=_ACQUIRE_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.warning(
                "%s ch%d (%s): no free stream slot after %.0fs — %d upstream(s) in use",
                self._cam.serial, self._cam.channel, self._cam.name,
                _ACQUIRE_TIMEOUT, _MAX_STREAMS_PER_DEVICE,
            )
            return False
        lan = await self._open_lan()
        if lan is None:
            self._sem.release()
            return False
        self._lan = lan
        self._stopping = False
        self._pump = asyncio.create_task(self._pump_loop())
        return True

    def _current_ip(self) -> str | None:
        """The station's LAN IP as of the latest status poll.

        The cloud's address follows the station's DHCP lease, so the value read
        once at setup goes stale the moment it changes and the feed stays dead
        until someone reloads the entry.  The status coordinator already
        refreshes it every poll, so read it from there.
        """
        st = (self._status.data or {}).get(self._cam.serial) or {}
        ip = st.get("local_ip") or self._cam.local_ip
        if ip and ip != self._ip:
            if self._ip is not None:
                _LOGGER.info(
                    "%s ch%d (%s): station moved %s -> %s",
                    self._cam.serial, self._cam.channel, self._cam.name, self._ip, ip,
                )
            self._ip = ip
        return ip

    async def _open_lan(self) -> Cpd7LanClient | None:
        """Open a CPD7 stream, or None if the channel has no live feed.

        The station rotates its control key across firmware/security changes; a
        stale cached key makes it reject the stream (``Result 3``).  Retry once
        with a freshly fetched key so the feed self-heals without a reload.
        """
        host = self._current_ip()
        if host is None:
            return None
        for refresh in (False, True):
            try:
                if self._key is None or refresh:
                    self._key, _ = await self._hass.async_add_executor_job(
                        self._client.get_control_key, self._cam.serial
                    )
                c = Cpd7LanClient(
                    host,
                    self._cam.serial,
                    self._key.encode("ascii"),
                    channel=self._cam.channel,
                    encrypt_stream=True,
                    stream_quality=self._quality.get(self._qkey, "MAIN"),
                )
                await self._hass.async_add_executor_job(c.start)
                return c
            except ControlKeyError as err:
                self._key = None  # drop the stale key so the retry refetches
                if refresh:
                    _LOGGER.warning(
                        "live feed still refused for %s ch%d (%s) after key refresh: %s",
                        self._cam.serial, self._cam.channel, self._cam.name, err,
                    )
                    return None
                _LOGGER.debug(
                    "control key stale for %s ch%d — refetching and retrying",
                    self._cam.serial, self._cam.channel,
                )
            except Exception as err:  # noqa: BLE001 - offline sub-stations error here
                _LOGGER.warning(
                    "no live feed for %s ch%d (%s) at %s: %s",
                    self._cam.serial, self._cam.channel, self._cam.name, host, err,
                )
                return None
        return None

    async def _pump_loop(self) -> None:
        decoder = HikStreamDecoder(self._cam.channel)
        empty = 0
        try:
            while not self._stopping:
                buf = await self._hass.async_add_executor_job(self._lan.read_chunk)
                if not buf:
                    empty += 1
                    if empty >= _MAX_EMPTY_READS:
                        break
                    continue
                empty = 0
                decoder.feed(buf)
                h = decoder.take()
                if not h:
                    continue
                rap = self._scan(h)
                for q in list(self._subs):
                    if q.full():
                        self._resync(q)
                    with contextlib.suppress(asyncio.QueueFull):
                        q.put_nowait((rap, h))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "CPD7 pump %s ch%d ended: %s", self._cam.serial, self._cam.channel, err
            )
        finally:
            for q in list(self._subs):
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(None)  # signal EOF so viewers close

    async def _teardown(self) -> None:
        self._stopping = True
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
            self._pump = None
        if self._lan is not None:
            with contextlib.suppress(Exception):
                await self._hass.async_add_executor_job(self._lan.close)
            self._lan = None
            self._sem.release()

    def _scan(self, h: bytes) -> bool:
        """Cache the latest SPS/PPS; report whether this chunk opens a new GOP."""
        rap = False
        for seg in h.split(_SC)[1:]:
            if not seg:
                continue
            t = seg[0] & 0x1F
            if t == 7:
                self._sps = _SC + seg
                rap = True
            elif t == 8:
                self._pps = _SC + seg
            elif t == 5:
                rap = True
        return rap

    def _resync(self, q: asyncio.Queue) -> None:
        """Restart a saturated viewer at the newest keyframe.

        Evicting single chunks would punch a hole into the middle of a GOP, so
        ffmpeg decodes garbage until the next IDR.  Dropping whole GOPs instead
        costs the viewer a jump forward and nothing else.
        """
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        keep: list = []
        for i in range(len(items) - 1, 0, -1):
            if items[i][0]:
                keep = items[i:]
                break
        if len(keep) >= _SUB_QUEUE_MAX - 1:  # no room won back — drop the lot
            keep = []
        if keep and self._sps and self._pps:
            q.put_nowait((False, self._sps + self._pps))
        for item in keep:
            q.put_nowait(item)


class _MjpegRelay:
    """Serve ffmpeg's mpjpeg output, skipping frames a slow viewer can't keep up with.

    Piping ffmpeg straight at the client makes the chain a strict FIFO: a viewer
    consuming below realtime never catches up, so every stale frame still has to
    go out and the picture falls further behind for the life of the session.
    Holding only the newest complete part bounds that to one frame.
    """

    def __init__(self) -> None:
        self._latest: bytes | None = None
        self._ready = asyncio.Event()
        self._eof = False

    async def read(self, stdout: asyncio.StreamReader) -> None:
        buf = bytearray()
        sep = b"--" + _MJPEG_BOUNDARY.encode()
        while True:
            chunk = await stdout.read(64 * 1024)
            if not chunk:
                self._eof = True
                self._ready.set()
                return
            buf.extend(chunk)
            idx = buf.rfind(sep)
            if idx <= 0:
                continue
            prev = buf.rfind(sep, 0, idx)
            if prev < 0:
                continue
            self._latest = bytes(buf[prev:idx])
            del buf[:idx]
            self._ready.set()

    async def write(self, response: web.StreamResponse) -> None:
        while True:
            await self._ready.wait()
            self._ready.clear()
            frame, self._latest = self._latest, None
            if frame:
                try:
                    await response.write(frame)
                except (ConnectionResetError, ConnectionAbortedError):
                    return
            if self._eof and self._latest is None:
                return


class HikLocalCamera(Camera):
    """A door-station channel served over the local CPD7 stream."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        client,
        cam: HikCamera,
        sem: asyncio.Semaphore,
        quality: dict[str, str],
        status,
    ) -> None:
        super().__init__()
        self.hass = hass
        self._cam = cam
        self._qkey = f"{cam.serial}_ch{cam.channel}"
        self._source = _ChannelStream(
            hass, client, cam, sem, quality, self._qkey, status
        )
        self._jpeg: bytes | None = None
        self._jpeg_ts = 0.0
        self._attr_name = cam.name
        self._attr_unique_id = f"{DOMAIN}_{cam.serial}_ch{cam.channel}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._cam.serial)})

    # -- stream plumbing --------------------------------------------------
    def _ffmpeg(self) -> str:
        return get_ffmpeg_manager(self.hass).binary

    async def _feed(self, q: asyncio.Queue, writer) -> None:
        """Pump shared H.264 from the subscription queue into an ffmpeg stdin."""
        try:
            while True:
                item = await q.get()
                if item is None:  # upstream ended
                    break
                writer.write(item[1])
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _cleanup(self, feed: asyncio.Task, q: asyncio.Queue, proc) -> None:
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await feed
        with contextlib.suppress(Exception):
            proc.kill()
        await self._source.unsubscribe(q)

    # -- snapshot ---------------------------------------------------------
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        now = time.monotonic()
        if self._jpeg and now - self._jpeg_ts < _SNAPSHOT_TTL:
            return self._jpeg
        q = await self._source.subscribe()
        if q is None:
            return self._jpeg
        proc = await asyncio.create_subprocess_exec(
            self._ffmpeg(), "-loglevel", "error",
            "-fflags", "+discardcorrupt", "-f", "h264", "-i", "pipe:0",
            "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        feed = asyncio.create_task(self._feed(q, proc.stdin))
        try:
            jpeg = await asyncio.wait_for(proc.stdout.read(), timeout=12)
        except (TimeoutError, asyncio.TimeoutError):
            jpeg = b""
        finally:
            await self._cleanup(feed, q, proc)
        if jpeg:
            self._jpeg, self._jpeg_ts = jpeg, time.monotonic()
        return jpeg or self._jpeg

    # -- live MJPEG -------------------------------------------------------
    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse:
        q = await self._source.subscribe()
        if q is None:
            return web.Response(status=503, text="no live feed")
        proc = await asyncio.create_subprocess_exec(
            self._ffmpeg(), "-loglevel", "warning",
            "-fflags", "+discardcorrupt", "-f", "h264", "-i", "pipe:0",
            "-an", "-c:v", "mjpeg", "-q:v", str(MJPEG_QUALITY), "-r", str(MJPEG_FPS),
            "-vf", f"scale={MJPEG_WIDTH}:{MJPEG_HEIGHT}",
            "-f", "mpjpeg", "-boundary_tag", _MJPEG_BOUNDARY, "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        feed = asyncio.create_task(self._feed(q, proc.stdin))
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type":
                    f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}"
            },
        )
        await response.prepare(request)
        relay = _MjpegRelay()
        reader = asyncio.create_task(relay.read(proc.stdout))
        try:
            await relay.write(response)
        except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
            pass
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
            await self._cleanup(feed, q, proc)
        return response
