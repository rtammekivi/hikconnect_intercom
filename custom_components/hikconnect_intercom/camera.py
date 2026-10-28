"""Camera entities for Hik-Connect Intercom — native CPD7 LAN stream.

One entity per real channel (door station).  The station only serves a couple of
concurrent CPD7 streams, so each camera keeps **one** shared upstream connection
open while at least one consumer (live viewer, snapshot or native stream) is
active, plus **one** shared ffmpeg encoder while a JPEG consumer is.  N browsers
viewing the same camera therefore cost one station connection and one encoder,
not N.

Pipeline (all local, no cloud/phone/frida):
  Cpd7LanClient (9010/9020, AES-128 control key from CAS, per-channel)
    -> HikStreamDecoder (nested RTP depacketization + optional AES -> H.264/H.265)
    -> shared ffmpeg (H.264/H.265 -> JPEG) -> newest frame -> browsers / snapshots.
    -> optional MPEG-TS mux (no re-encode) -> ``stream_source()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time

from aiohttp import web
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_NATIVE_STREAM,
    DOMAIN,
    MJPEG_FPS,
    MJPEG_HEIGHT,
    MJPEG_QUALITY,
    MJPEG_WIDTH,
)
from .hikconnect_api import HikCamera
from .lib.hik_decoder import HikStreamDecoder
from .lib.lan_client import ControlKeyError, Cpd7LanClient
from .lib.mpegts import TsMuxer

_LOGGER = logging.getLogger(__name__)

_SC = b"\x00\x00\x00\x01"     # H.264 Annex-B start code
_EOI = b"\xff\xd9"            # JPEG end-of-image
_MJPEG_BOUNDARY = "frame"
_MAX_EMPTY_READS = 3
_MAX_STREAMS_PER_DEVICE = 2   # concurrent *upstreams* (channels), not viewers
_ACQUIRE_TIMEOUT = 6.0
_LIVE_ACQUIRE_TIMEOUT = 25.0  # allow in-flight previews to finish before live video
_LINGER_SEC = 1.0             # return scarce slots before snapshot acquire times out
_FEED_QUEUE_MAX = 240         # bounded backlog into ffmpeg; drops whole GOPs
_FIRST_FRAME_TIMEOUT = 12.0
_FRAME_TIMEOUT = 10.0
_NATIVE_STREAMS = f"{DOMAIN}_native_streams"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client = data["client"]
    quality = data["quality"]
    status = data["status_coordinator"]
    native = entry.options.get(CONF_NATIVE_STREAM, False)
    if native and _NATIVE_STREAMS not in hass.data:
        hass.data[_NATIVE_STREAMS] = {}
        hass.http.register_view(_NativeStreamView(hass.data[_NATIVE_STREAMS]))
    sems: dict[str, asyncio.Semaphore] = {}
    entities = []
    for cam in data["cameras"]:
        sem = sems.setdefault(cam.serial, asyncio.Semaphore(_MAX_STREAMS_PER_DEVICE))
        entities.append(HikLocalCamera(hass, client, cam, sem, quality, status, native))
    async_add_entities(entities)


class _Sink:
    """One consumer's view of the decoded elementary stream."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_FEED_QUEUE_MAX)
        self.synced = False


async def _pipe(sink: _Sink, stdin: asyncio.StreamWriter) -> None:
    """A sink's queue -> an ffmpeg's stdin."""
    try:
        while True:
            item = await sink.queue.get()
            if item is None:  # upstream ended
                break
            stdin.write(item[2])
            await stdin.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            stdin.close()


class _ChannelStream:
    """One CPD7 upstream per channel, fanned out to every consumer.

    Every consumer used to spawn its own ffmpeg, so each open paid the encoder's
    full start-up and a card preview followed by a live view cost two processes.
    One encoder per channel publishes the newest JPEG instead; consumers read
    that, and the pipeline lingers after the last one leaves so reopening it is
    instant rather than a fresh cold start.  Native stream consumers get the
    elementary stream itself, starting at the cached current GOP.
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
        self._proc: asyncio.subprocess.Process | None = None
        self._encoder: _Sink | None = None
        self._enc_tasks: list[asyncio.Task] = []
        self._sinks: set[_Sink] = set()
        self._subs: set[_Sink] = set()
        self._gop: list[tuple[int, bytes]] | None = None
        self._stopping = False
        self._users = 0
        self._jpeg: bytes | None = None
        self._seq = 0
        self._waiters: set[asyncio.Event] = set()
        self._vps = b""
        self._decoder: HikStreamDecoder | None = None
        self._video_key: str | None = None
        self._sps = b""
        self._pps = b""
        self._lock = asyncio.Lock()
        self._linger: asyncio.TimerHandle | None = None

    @property
    def codec(self) -> str | None:
        return self._decoder.codec if self._decoder else None

    # -- consumer lifecycle ----------------------------------------------
    async def acquire(self, timeout: float = _ACQUIRE_TIMEOUT) -> bool:
        """Register a JPEG consumer, starting the pipeline if it isn't already up."""
        async with self._lock:
            if not await self._ensure_upstream(timeout):
                return False
            if self._proc is not None and any(t.done() for t in self._enc_tasks):
                await self._stop_encoder()  # encoder died — rebuild it
            if self._proc is None and not await self._start_encoder():
                self._schedule_linger()
                return False
            self._users += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._users = max(0, self._users - 1)
            self._schedule_linger()

    async def subscribe(self, timeout: float = _LIVE_ACQUIRE_TIMEOUT) -> _Sink | None:
        """Register a native stream consumer; None if there is no live feed."""
        async with self._lock:
            if not await self._ensure_upstream(timeout):
                return None
            sink = _Sink()
            self._attach(sink)
            self._subs.add(sink)
            self._schedule_linger()
            return sink

    async def unsubscribe(self, sink: _Sink) -> None:
        async with self._lock:
            self._subs.discard(sink)
            self._sinks.discard(sink)
            self._schedule_linger()

    async def _ensure_upstream(self, timeout: float) -> bool:
        if self._linger is not None:
            self._linger.cancel()
            self._linger = None
        if self._pump is not None and self._pump.done():
            await self._teardown()  # upstream died — rebuild it
        if self._lan is None:
            return await self._open(timeout)
        return True

    def _schedule_linger(self) -> None:
        if self._users or self._linger is not None:
            return
        if self._proc is None and (self._subs or self._lan is None):
            return
        loop = asyncio.get_running_loop()
        self._linger = loop.call_later(
            _LINGER_SEC, lambda: asyncio.create_task(self._linger_teardown())
        )

    async def _linger_teardown(self) -> None:
        async with self._lock:
            self._linger = None
            if self._users:
                return
            await self._stop_encoder()
            if not self._subs:
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
    async def _open(self, timeout: float = _ACQUIRE_TIMEOUT) -> bool:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.warning(
                "%s ch%d (%s): no free stream slot after %.0fs — %d upstream(s) in use",
                self._cam.serial, self._cam.channel, self._cam.name,
                timeout, _MAX_STREAMS_PER_DEVICE,
            )
            return False
        try:
            lan = await self._open_lan()
        except BaseException:
            self._sem.release()
            raise
        if lan is None:
            self._sem.release()
            return False
        try:
            # Discover the codec before consumers attach. Keep the first packets
            # so encrypted headers can be replayed after fetching the video key.
            decoder = HikStreamDecoder(self._cam.channel, self._video_key)
            initial = bytearray()
            deadline = time.monotonic() + 8
            while decoder.codec is None and time.monotonic() < deadline:
                chunk = await self._hass.async_add_executor_job(lan.read_chunk)
                initial.extend(chunk)
                if len(initial) > 2 * 1024 * 1024:
                    raise ValueError("Camera codec discovery exceeded buffer limit")
                decoder.feed(chunk)
            if decoder.codec is None:
                raise ValueError("Camera sent no supported video headers")
            if decoder.requires_key and self._video_key is None:
                self._video_key = await self._hass.async_add_executor_job(
                    self._client.get_video_key, self._cam.serial
                )
                decoder = HikStreamDecoder(self._cam.channel, self._video_key)
                decoder.feed(bytes(initial))
        except (Exception, asyncio.CancelledError) as err:
            # HA cancels snapshot requests at its timeout. Cancellation must
            # release the slot too, or two slow/offline channels exhaust it.
            if not isinstance(err, asyncio.CancelledError):
                _LOGGER.warning(
                    "video pipeline failed to start for %s: %s", self._cam.name, err
                )
            with contextlib.suppress(Exception):
                await self._hass.async_add_executor_job(lan.close)
            self._sem.release()
            if isinstance(err, asyncio.CancelledError):
                raise
            return False
        self._lan = lan
        self._decoder = decoder
        self._stopping = False
        self._gop = None
        for ts, unit in decoder.take_units():
            self._ingest(ts, unit)
        self._pump = asyncio.create_task(self._pump_loop())
        return True

    async def _start_encoder(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                get_ffmpeg_manager(self._hass).binary, "-loglevel", "error",
                "-fflags", "+discardcorrupt", "-f", self.codec, "-i", "pipe:0",
                "-an", "-c:v", "mjpeg", "-q:v", str(MJPEG_QUALITY), "-r", str(MJPEG_FPS),
                "-vf", f"scale={MJPEG_WIDTH}:{MJPEG_HEIGHT}",
                "-f", "image2pipe", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("JPEG encoder failed to start for %s: %s", self._cam.name, err)
            return False
        self._proc = proc
        self._jpeg = None
        self._encoder = _Sink()
        self._attach(self._encoder)
        self._enc_tasks = [
            asyncio.create_task(_pipe(self._encoder, proc.stdin)),
            asyncio.create_task(self._read_loop(proc)),
        ]
        return True

    async def _stop_encoder(self) -> None:
        for task in self._enc_tasks:
            task.cancel()
        for task in self._enc_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._enc_tasks = []
        if self._encoder is not None:
            self._sinks.discard(self._encoder)
            self._encoder = None
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.kill()
            self._proc = None

    def _current_ip(self) -> str | None:
        """The station's LAN IP as of the latest status poll.

        The cloud's address follows the station's DHCP lease, so the value read
        once at setup goes stale the moment it changes and the feed stays dead
        until someone reloads the entry.  The status coordinator already
        refreshes it every poll, so read it from there.
        """
        st = (self._status.data or {}).get(self._cam.serial) or {}
        ip = st.get("local_ip") or self._ip or self._cam.local_ip
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
                start_job = self._hass.async_add_executor_job(c.start)
                try:
                    await asyncio.shield(start_job)
                except asyncio.CancelledError:
                    # The blocking socket job continues after cancellation;
                    # close its result when it finishes, rather than leaking it.
                    def close_cancelled(
                        future: asyncio.Future, client: Cpd7LanClient = c
                    ) -> None:
                        with contextlib.suppress(Exception):
                            future.result()
                        self._hass.async_add_executor_job(client.close)

                    start_job.add_done_callback(close_cancelled)
                    raise
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
        """Station -> decoder -> every attached sink."""
        decoder = self._decoder
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
                for ts, unit in decoder.take_units():
                    self._ingest(ts, unit)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "CPD7 pump %s ch%d ended: %s", self._cam.serial, self._cam.channel, err
            )
        finally:
            for sink in self._sinks:
                self._end(sink)

    def _ingest(self, ts: int, h: bytes) -> None:
        """Track the current GOP and hand the access unit to every sink."""
        rap = self._scan(h)
        if rap:
            self._gop = [(ts, h)]
        elif self._gop is not None:
            self._gop.append((ts, h))
            if len(self._gop) >= _FEED_QUEUE_MAX - 1:
                self._gop = None
        for sink in self._sinks:
            self._offer(sink, rap, ts, h)

    def _attach(self, sink: _Sink) -> None:
        """Start a sink at the current GOP so it can decode immediately."""
        if self._gop:
            sink.synced = True
            sink.queue.put_nowait((False, None, self._vps + self._sps + self._pps))
            for i, (ts, h) in enumerate(self._gop):
                sink.queue.put_nowait((i == 0, ts, h))
        self._sinks.add(sink)

    def _offer(self, sink: _Sink, rap: bool, ts: int, h: bytes) -> None:
        if not sink.synced:
            if not rap:
                return
            sink.synced = True
            sink.queue.put_nowait((False, None, self._vps + self._sps + self._pps))
        if sink.queue.full():
            self._resync(sink.queue)
        with contextlib.suppress(asyncio.QueueFull):
            sink.queue.put_nowait((rap, ts, h))

    @staticmethod
    def _end(sink: _Sink) -> None:
        if sink.queue.full():
            sink.queue.get_nowait()
        sink.queue.put_nowait(None)

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        """ffmpeg's stdout -> the newest published JPEG.

        ``image2pipe`` writes complete JPEGs back to back.  0xFF bytes inside
        entropy-coded data are stuffed as ``FF 00``, so ``FF D9`` only ever
        appears as a real end-of-image marker and splitting on it is safe.
        """
        buf = bytearray()
        while True:
            chunk = await proc.stdout.read(64 * 1024)
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
        await self._stop_encoder()
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
            self._pump = None
        for sink in self._subs:
            self._end(sink)
        self._subs.clear()
        self._sinks.clear()
        self._gop = None
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
            if self._decoder and self._decoder.codec == "hevc":
                t = (seg[0] >> 1) & 63
                if t == 32:
                    self._vps = _SC + seg
                elif t == 33:
                    self._sps = _SC + seg
                elif t == 34:
                    self._pps = _SC + seg
                if 16 <= t <= 21:
                    rap = True
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
        """Restart a saturated sink at the newest keyframe.

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
            q.put_nowait((False, None, self._vps + self._sps + self._pps))
        for item in keep:
            q.put_nowait(item)


class _NativeStreamView(HomeAssistantView):
    """Serves each camera's elementary stream as MPEG-TS for ``stream_source()``.

    The stream worker, go2rtc and HomeKit's ffmpeg open the URL without HA
    credentials, so access is gated by a per-entity random token instead.
    """

    url = f"/api/{DOMAIN}/stream/{{token}}"
    name = f"api:{DOMAIN}:stream"
    requires_auth = False

    def __init__(self, cameras: dict[str, HikLocalCamera]) -> None:
        self._cameras = cameras

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        camera = self._cameras.get(token)
        if camera is None:
            return web.Response(status=404)
        return await camera.handle_native_stream(request)


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
        native: bool = False,
    ) -> None:
        super().__init__()
        self.hass = hass
        self._cam = cam
        self._qkey = f"{cam.serial}_ch{cam.channel}"
        self._source = _ChannelStream(
            hass, client, cam, sem, quality, self._qkey, status
        )
        self._last: bytes | None = None
        self._native = native
        self._token = secrets.token_urlsafe(32)
        if native:
            self._attr_supported_features = CameraEntityFeature.STREAM
        self._attr_name = cam.name
        self._attr_unique_id = f"{DOMAIN}_{cam.serial}_ch{cam.channel}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._cam.serial)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._native:
            self.hass.data[_NATIVE_STREAMS][self._token] = self

    async def async_will_remove_from_hass(self) -> None:
        if self._native:
            self.hass.data[_NATIVE_STREAMS].pop(self._token, None)
        await super().async_will_remove_from_hass()

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

    # -- native stream ----------------------------------------------------
    async def stream_source(self) -> str | None:
        if not self._native:
            return None
        api = self.hass.config.api
        scheme = "https" if api.use_ssl else "http"
        return f"{scheme}://127.0.0.1:{api.port}/api/{DOMAIN}/stream/{self._token}"

    async def handle_native_stream(self, request: web.Request) -> web.StreamResponse:
        """The station's own H.264/H.265 as MPEG-TS, never re-encoded."""
        sink = await self._source.subscribe(_LIVE_ACQUIRE_TIMEOUT)
        if sink is None:
            return web.Response(status=503, text="no live feed")
        mux = TsMuxer(self._source.codec)
        response = web.StreamResponse(status=200, headers={"Content-Type": "video/mp2t"})
        pending = b""
        try:
            await response.prepare(request)
            while True:
                try:
                    item = await asyncio.wait_for(sink.queue.get(), _FRAME_TIMEOUT)
                except (TimeoutError, asyncio.TimeoutError):
                    break
                if item is None:
                    break
                rap, ts, data = item
                if ts is None:
                    pending += data
                    continue
                await response.write(mux.mux(ts, pending + data, rap))
                pending = b""
        except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
            pass
        finally:
            await self._source.unsubscribe(sink)
        return response

    # -- live MJPEG -------------------------------------------------------
    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse:
        if not await self._source.acquire(_LIVE_ACQUIRE_TIMEOUT):
            return web.Response(status=503, text="no live feed")
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type":
                    f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}"
            },
        )
        seq = 0
        try:
            await response.prepare(request)
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
