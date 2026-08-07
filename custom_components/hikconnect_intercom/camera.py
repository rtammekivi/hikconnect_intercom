"""Camera entities for Hik-Connect Intercom — native CPD7 LAN stream.

One entity per real channel (door station).  The station only serves a couple of
concurrent CPD7 streams, so each camera keeps **one** shared upstream connection
*and* **one** shared ffmpeg encoder open while at least one consumer (live viewer
or snapshot) is active.  N browsers viewing the same camera therefore cost one
station connection and one encoder, not N.

Pipeline (all local, no cloud/phone/frida):
  Cpd7LanClient (9010/9020, AES-128 control key from CAS, per-channel)
    -> HikStreamDecoder (strip $01 framing + 12B RTP + 13B Hik header -> H.264)
    -> shared ffmpeg (H.264 -> JPEG) -> newest frame -> browsers / snapshots.
"""

from __future__ import annotations

import asyncio
import contextlib

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
_EOI = b"\xff\xd9"            # JPEG end-of-image
_MJPEG_BOUNDARY = "frame"
_MAX_EMPTY_READS = 3
_MAX_STREAMS_PER_DEVICE = 2   # concurrent *upstreams* (channels), not viewers
_ACQUIRE_TIMEOUT = 6.0
_LINGER_SEC = 30.0            # keep the pipeline warm so reopening is instant
_FEED_QUEUE_MAX = 240         # bounded backlog into ffmpeg; drops whole GOPs
_FIRST_FRAME_TIMEOUT = 12.0
_FRAME_TIMEOUT = 10.0


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
    """One CPD7 upstream and one ffmpeg encoder per channel, shared by all viewers.

    Every consumer used to spawn its own ffmpeg, so each open paid the encoder's
    full start-up and a card preview followed by a live view cost two processes.
    One encoder per channel publishes the newest JPEG instead; consumers read
    that, and the pipeline lingers after the last one leaves so reopening it is
    instant rather than a fresh cold start.
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
        self._proc: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue | None = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._users = 0
        self._jpeg: bytes | None = None
        self._seq = 0
        self._waiters: set[asyncio.Event] = set()
        self._sps = b""
        self._pps = b""
        self._lock = asyncio.Lock()
        self._linger: asyncio.TimerHandle | None = None

    # -- consumer lifecycle ----------------------------------------------
    async def acquire(self) -> bool:
        """Register a consumer, starting the pipeline if it isn't already up."""
        async with self._lock:
            if self._linger is not None:
                self._linger.cancel()
                self._linger = None
            if self._proc is not None and any(t.done() for t in self._tasks):
                await self._teardown()  # pipeline died — rebuild it
            if self._proc is None and not await self._open():
                return False
            self._users += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._users = max(0, self._users - 1)
            if self._users or self._proc is None:
                return
            loop = asyncio.get_running_loop()
            self._linger = loop.call_later(
                _LINGER_SEC, lambda: asyncio.create_task(self._linger_teardown())
            )

    async def _linger_teardown(self) -> None:
        async with self._lock:
            self._linger = None
            if not self._users:
                await self._teardown()

    # -- frame delivery ---------------------------------------------------
    @property
    def latest(self) -> tuple[int, bytes] | None:
        return (self._seq, self._jpeg) if self._jpeg is not None else None

    async def frame_after(self, seq: int, timeout: float) -> tuple[int, bytes] | None:
        """Wait for a frame newer than ``seq``; None if none arrives in time.

        Consumers always jump to the newest frame rather than draining a queue,
        so one that reads slowly skips frames instead of falling progressively
        further behind for the rest of the session.
        """
        if self._seq > seq and self._jpeg is not None:
            return self._seq, self._jpeg
        ev = asyncio.Event()
        self._waiters.add(ev)
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return None
        finally:
            self._waiters.discard(ev)
        return self.latest

    def _publish(self, jpeg: bytes) -> None:
        self._jpeg = jpeg
        self._seq += 1
        for ev in self._waiters:
            ev.set()

    # -- pipeline lifecycle ----------------------------------------------
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
        try:
            proc = await asyncio.create_subprocess_exec(
                get_ffmpeg_manager(self._hass).binary, "-loglevel", "error",
                "-fflags", "+discardcorrupt", "-f", "h264", "-i", "pipe:0",
                "-an", "-c:v", "mjpeg", "-q:v", str(MJPEG_QUALITY), "-r", str(MJPEG_FPS),
                "-vf", f"scale={MJPEG_WIDTH}:{MJPEG_HEIGHT}",
                "-f", "image2pipe", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("ffmpeg failed to start for %s: %s", self._cam.name, err)
            with contextlib.suppress(Exception):
                await self._hass.async_add_executor_job(lan.close)
            self._sem.release()
            return False
        self._lan = lan
        self._proc = proc
        self._stopping = False
        self._jpeg = None
        self._queue = asyncio.Queue(maxsize=_FEED_QUEUE_MAX)
        if self._sps and self._pps:  # let ffmpeg decode before the next IDR
            self._queue.put_nowait((False, self._sps + self._pps))
        self._tasks = [
            asyncio.create_task(self._pump_loop()),
            asyncio.create_task(self._feed_loop()),
            asyncio.create_task(self._read_loop()),
        ]
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
        """Station -> decoder -> the queue feeding ffmpeg."""
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
                if self._queue.full():
                    self._resync(self._queue)
                with contextlib.suppress(asyncio.QueueFull):
                    self._queue.put_nowait((rap, h))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "CPD7 pump %s ch%d ended: %s", self._cam.serial, self._cam.channel, err
            )
        finally:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)

    async def _feed_loop(self) -> None:
        """The queue -> ffmpeg's stdin."""
        try:
            while True:
                item = await self._queue.get()
                if item is None:  # upstream ended
                    break
                self._proc.stdin.write(item[1])
                await self._proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()

    async def _read_loop(self) -> None:
        """ffmpeg's stdout -> the newest published JPEG.

        ``image2pipe`` writes complete JPEGs back to back.  0xFF bytes inside
        entropy-coded data are stuffed as ``FF 00``, so ``FF D9`` only ever
        appears as a real end-of-image marker and splitting on it is safe.
        """
        buf = bytearray()
        while True:
            chunk = await self._proc.stdout.read(64 * 1024)
            if not chunk:
                return
            buf.extend(chunk)
            while True:
                idx = buf.find(_EOI)
                if idx < 0:
                    break
                end = idx + len(_EOI)
                self._publish(bytes(buf[:end]))
                del buf[:end]

    async def _teardown(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.kill()
            self._proc = None
        self._queue = None
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
        """Restart a saturated encoder feed at the newest keyframe.

        Evicting single chunks would punch a hole into the middle of a GOP, so
        ffmpeg decodes garbage until the next IDR.  Dropping whole GOPs instead
        costs a jump forward and nothing else.
        """
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        keep: list = []
        for i in range(len(items) - 1, 0, -1):
            if items[i][0]:
                keep = items[i:]
                break
        if len(keep) >= _FEED_QUEUE_MAX - 1:  # no room won back — drop the lot
            keep = []
        if keep and self._sps and self._pps:
            q.put_nowait((False, self._sps + self._pps))
        for item in keep:
            q.put_nowait(item)


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
        self._last: bytes | None = None
        self._attr_name = cam.name
        self._attr_unique_id = f"{DOMAIN}_{cam.serial}_ch{cam.channel}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._cam.serial)})

    # -- snapshot ---------------------------------------------------------
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """The newest frame from the shared encoder — no process of its own."""
        if not await self._source.acquire():
            return self._last
        try:
            got = self._source.latest
            if got is None:
                got = await self._source.frame_after(0, _FIRST_FRAME_TIMEOUT)
            if got is not None:
                self._last = got[1]
        finally:
            await self._source.release()
        return self._last

    # -- live MJPEG -------------------------------------------------------
    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse:
        if not await self._source.acquire():
            return web.Response(status=503, text="no live feed")
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type":
                    f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}"
            },
        )
        await response.prepare(request)
        seq = 0
        try:
            while True:
                got = await self._source.frame_after(seq, _FRAME_TIMEOUT)
                if got is None:
                    break
                seq, jpeg = got
                self._last = jpeg
                await response.write(
                    b"--" + _MJPEG_BOUNDARY.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                    + jpeg + b"\r\n"
                )
        except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
            pass
        finally:
            await self._source.release()
        return response
