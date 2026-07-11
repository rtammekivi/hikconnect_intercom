"""Camera entities for Hik-Connect Intercom — native CPD7 LAN stream.

One entity per real channel (door station).  A CPD7 client is opened **per active
view/snapshot** and closed as soon as the viewer disconnects or the snapshot is
taken — nothing streams while idle.  A short snapshot cache and a per-device
concurrency cap keep HA from over-subscribing the station's limited stream slots.

Pipeline (all local, no cloud/phone/frida):
  Cpd7LanClient (9010/9020, AES-128 control key from CAS, per-channel)
    -> HikStreamDecoder (strip $01 framing + 12B RTP + 13B Hik header -> H.264)
    -> ffmpeg (H.264 -> MJPEG) -> browser / snapshot.
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
from .lib.lan_client import Cpd7LanClient

_LOGGER = logging.getLogger(__name__)

_MAX_EMPTY_READS = 3
_SNAPSHOT_TTL = 10.0          # reuse a recent still instead of re-opening a stream
_MAX_STREAMS_PER_DEVICE = 2   # keep within the station's concurrent-stream limit
_ACQUIRE_TIMEOUT = 6.0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client = data["client"]
    quality = data["quality"]
    sems: dict[str, asyncio.Semaphore] = {}
    entities = []
    for cam in data["cameras"]:
        sem = sems.setdefault(cam.serial, asyncio.Semaphore(_MAX_STREAMS_PER_DEVICE))
        entities.append(HikLocalCamera(hass, client, cam, sem, quality))
    async_add_entities(entities)


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
    ) -> None:
        super().__init__()
        self.hass = hass
        self._client = client
        self._cam = cam
        self._sem = sem  # shared across the device's channels
        self._quality = quality  # shared with the Stream-quality select
        self._qkey = f"{cam.serial}_ch{cam.channel}"
        self._key: str | None = None
        self._jpeg: bytes | None = None
        self._jpeg_ts = 0.0
        self._attr_name = cam.name
        self._attr_unique_id = f"{DOMAIN}_{cam.serial}_ch{cam.channel}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._cam.serial)})

    # -- stream plumbing --------------------------------------------------
    async def _control_key(self) -> str:
        if self._key is None:
            self._key, _ = await self.hass.async_add_executor_job(
                self._client.get_control_key, self._cam.serial
            )
        return self._key

    async def _open_client(self) -> Cpd7LanClient | None:
        """Open a CPD7 stream, or None if the channel has no live feed."""
        try:
            key = await self._control_key()
            c = Cpd7LanClient(
                self._cam.local_ip,
                self._cam.serial,
                key.encode("ascii"),
                channel=self._cam.channel,
                encrypt_stream=True,
                stream_type=self._quality.get(self._qkey, "MAIN"),
            )
            await self.hass.async_add_executor_job(c.start)
            return c
        except Exception as err:  # noqa: BLE001 - offline sub-stations error here
            _LOGGER.debug(
                "no live feed for %s ch%d (%s): %s",
                self._cam.serial, self._cam.channel, self._cam.name, err,
            )
            return None

    async def _pump(self, client: Cpd7LanClient, decoder: HikStreamDecoder, writer) -> None:
        empty = 0
        try:
            while True:
                buf = await self.hass.async_add_executor_job(client.read_chunk)
                if not buf:
                    empty += 1
                    if empty >= _MAX_EMPTY_READS:
                        break
                    continue
                empty = 0
                decoder.feed(buf)
                h = decoder.take()
                if h:
                    writer.write(h)
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    def _ffmpeg(self) -> str:
        return get_ffmpeg_manager(self.hass).binary

    async def _cleanup(self, pump: asyncio.Task, client: Cpd7LanClient, proc) -> None:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump
        with contextlib.suppress(Exception):
            await self.hass.async_add_executor_job(client.close)
        with contextlib.suppress(Exception):
            proc.kill()

    async def _acquire(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=timeout)
            return True
        except (TimeoutError, asyncio.TimeoutError):
            return False

    # -- snapshot ---------------------------------------------------------
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        now = time.monotonic()
        if self._jpeg and now - self._jpeg_ts < _SNAPSHOT_TTL:
            return self._jpeg
        if not await self._acquire(2.0):
            return self._jpeg  # busy — return last known still
        try:
            client = await self._open_client()
            if client is None:
                return self._jpeg
            decoder = HikStreamDecoder(self._cam.channel)
            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg(), "-loglevel", "error",
                "-fflags", "+discardcorrupt", "-f", "h264", "-i", "pipe:0",
                "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            pump = asyncio.create_task(self._pump(client, decoder, proc.stdin))
            try:
                jpeg = await asyncio.wait_for(proc.stdout.read(), timeout=12)
            except (TimeoutError, asyncio.TimeoutError):
                jpeg = b""
            finally:
                await self._cleanup(pump, client, proc)
            if jpeg:
                self._jpeg, self._jpeg_ts = jpeg, time.monotonic()
            return jpeg or self._jpeg
        finally:
            self._sem.release()

    # -- live MJPEG -------------------------------------------------------
    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse:
        if not await self._acquire(_ACQUIRE_TIMEOUT):
            return web.Response(status=503, text="camera busy")
        try:
            client = await self._open_client()
            if client is None:
                return web.Response(status=503, text="no live feed")
            decoder = HikStreamDecoder(self._cam.channel)
            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg(), "-loglevel", "warning",
                "-fflags", "+discardcorrupt", "-f", "h264", "-i", "pipe:0",
                "-an", "-c:v", "mjpeg", "-q:v", str(MJPEG_QUALITY), "-r", str(MJPEG_FPS),
                "-vf", f"scale={MJPEG_WIDTH}:{MJPEG_HEIGHT}", "-f", "mpjpeg", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            pump = asyncio.create_task(self._pump(client, decoder, proc.stdin))
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "multipart/x-mixed-replace; boundary=ffmpeg"},
            )
            await response.prepare(request)
            try:
                while True:
                    chunk = await proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    await response.write(chunk)
            except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
                pass
            finally:
                await self._cleanup(pump, client, proc)
            return response
        finally:
            self._sem.release()
