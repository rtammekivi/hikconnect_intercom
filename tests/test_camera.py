"""Regression coverage for scarce stream slots and cancellation cleanup."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests._modules import load_camera, load_module

camera = load_camera()


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def make_stream(self, slots=1):
        cam = SimpleNamespace(
            serial="TEST_RECORDER", channel=4, name="Test camera", local_ip="192.0.2.1"
        )
        return camera._ChannelStream(
            None, None, cam, asyncio.Semaphore(slots), {}, "test", None
        )

    async def test_cancel_during_connect_releases_slot(self):
        stream = self.make_stream()
        entered = asyncio.Event()

        async def connect():
            entered.set()
            await asyncio.Event().wait()

        stream._open_lan = connect
        task = asyncio.create_task(stream._open())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(stream._sem._value, 1)

    async def test_cancel_during_codec_discovery_closes_lan(self):
        stream = self.make_stream()
        entered = asyncio.Event()
        lan = SimpleNamespace(read_chunk=Mock(), close=Mock())

        async def job(fn):
            if fn is lan.close:
                fn()
                return
            entered.set()
            await asyncio.Event().wait()

        stream._hass = SimpleNamespace(async_add_executor_job=job)
        stream._open_lan = AsyncMock(return_value=lan)
        task = asyncio.create_task(stream._open())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(stream._sem._value, 1)
        lan.close.assert_called_once()

    async def test_live_can_wait_for_preview_slot(self):
        stream = self.make_stream(slots=0)
        stream._open_lan = AsyncMock(return_value=None)
        task = asyncio.create_task(stream._open(timeout=1))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        stream._sem.release()
        self.assertFalse(await task)
        stream._open_lan.assert_awaited_once()
        self.assertEqual(stream._sem._value, 1)

    async def test_cancel_while_waiting_does_not_release_unowned_slot(self):
        stream = self.make_stream(slots=0)
        task = asyncio.create_task(stream._open(timeout=1))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(stream._sem._value, 0)

    async def test_acquire_forwards_live_timeout(self):
        stream = self.make_stream()
        stream._open = AsyncMock(return_value=True)
        stream._start_encoder = AsyncMock(return_value=True)
        self.assertTrue(await stream.acquire(camera._LIVE_ACQUIRE_TIMEOUT))
        stream._open.assert_awaited_once_with(camera._LIVE_ACQUIRE_TIMEOUT)
        self.assertEqual(stream._users, 1)
        self.assertLess(camera._LINGER_SEC, camera._ACQUIRE_TIMEOUT)

    async def test_cancelled_start_closes_connection_when_executor_finishes(self):
        stream = self.make_stream()
        stream._key = "0123456789abcdef"
        stream._current_ip = Mock(return_value="192.0.2.1")
        started = asyncio.Event()
        start_result = asyncio.get_running_loop().create_future()
        lan = SimpleNamespace(start=Mock(), close=Mock())

        def executor(fn):
            if fn is lan.start:
                started.set()
                return start_result
            fn()
            done = asyncio.get_running_loop().create_future()
            done.set_result(None)
            return done

        stream._hass = SimpleNamespace(async_add_executor_job=executor)
        with patch.object(camera, "Cpd7LanClient", return_value=lan):
            task = asyncio.create_task(stream._open_lan())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(start_result.cancelled())
            lan.close.assert_not_called()
            start_result.set_result(None)
            await asyncio.sleep(0)
            lan.close.assert_called_once()

    async def test_response_prepare_failure_releases_consumer(self):
        source = SimpleNamespace(
            acquire=AsyncMock(return_value=True), release=AsyncMock()
        )
        entity = SimpleNamespace(_source=source)
        response = SimpleNamespace(prepare=AsyncMock(side_effect=ConnectionResetError))
        with patch.object(camera.web, "StreamResponse", return_value=response):
            await camera.HikLocalCamera.handle_async_mjpeg_stream(entity, Mock())
        source.acquire.assert_awaited_once_with(camera._LIVE_ACQUIRE_TIMEOUT)
        source.release.assert_awaited_once()


SPS = b"\x00\x00\x00\x01\x67sps"
PPS = b"\x00\x00\x00\x01\x68pps"
IDR = b"\x00\x00\x00\x01\x65idr"
P = b"\x00\x00\x00\x01\x41p"


def drain(sink):
    items = []
    while not sink.queue.empty():
        items.append(sink.queue.get_nowait())
    return items


class FanOutTests(unittest.IsolatedAsyncioTestCase):
    def make_stream(self):
        cam = SimpleNamespace(serial="S", channel=1, name="Door", local_ip="192.0.2.1")
        return camera._ChannelStream(None, None, cam, asyncio.Semaphore(1), {}, "q", None)

    async def test_late_sink_starts_at_current_gop(self):
        stream = self.make_stream()
        stream._ingest(0, P)
        stream._ingest(3600, SPS + PPS + IDR)
        stream._ingest(7200, P)
        sink = camera._Sink()
        stream._attach(sink)
        stream._ingest(10800, P)
        self.assertEqual(
            [(ts, h) for _, ts, h in drain(sink)],
            [(None, SPS + PPS), (3600, SPS + PPS + IDR), (7200, P), (10800, P)],
        )

    async def test_sink_without_cached_gop_waits_for_keyframe(self):
        stream = self.make_stream()
        sink = camera._Sink()
        stream._attach(sink)
        stream._ingest(0, P)
        stream._ingest(3600, SPS + PPS + IDR)
        self.assertEqual([h for _, _, h in drain(sink)], [SPS + PPS, SPS + PPS + IDR])

    async def test_ended_upstream_reaches_full_sink(self):
        sink = camera._Sink()
        for _ in range(camera._FEED_QUEUE_MAX):
            sink.queue.put_nowait((False, 0, P))
        camera._ChannelStream._end(sink)
        self.assertIsNone(drain(sink)[-1])

    async def test_native_subscriber_does_not_start_encoder(self):
        stream = self.make_stream()
        stream._open = AsyncMock(side_effect=lambda timeout: setattr(stream, "_lan", object()) or True)
        stream._start_encoder = AsyncMock(return_value=True)
        sink = await stream.subscribe(1)
        self.assertIsNotNone(sink)
        stream._start_encoder.assert_not_awaited()
        self.assertIsNone(stream._linger)

    async def test_last_subscriber_leaving_schedules_teardown(self):
        stream = self.make_stream()
        stream._open = AsyncMock(side_effect=lambda timeout: setattr(stream, "_lan", object()) or True)
        sink = await stream.subscribe(1)
        await stream.unsubscribe(sink)
        self.assertIsNotNone(stream._linger)
        stream._linger.cancel()

    async def test_encoder_lingers_off_while_subscriber_stays(self):
        stream = self.make_stream()
        stream._lan = object()
        stream._proc = object()
        stream._subs.add(camera._Sink())
        stream._stop_encoder = AsyncMock()
        stream._teardown = AsyncMock()
        await stream.release()
        self.assertIsNotNone(stream._linger)
        stream._linger.cancel()
        await stream._linger_teardown()
        stream._stop_encoder.assert_awaited_once()
        stream._teardown.assert_not_awaited()


class PassthroughTests(unittest.IsolatedAsyncioTestCase):
    def make_camera(self, native=frozenset({"h264"})):
        cam = SimpleNamespace(serial="S", channel=1, slot=1, name="Door", local_ip="192.0.2.1")
        hass = SimpleNamespace(
            config=SimpleNamespace(api=SimpleNamespace(use_ssl=False, port=8123))
        )
        entity = camera.HikLocalCamera(hass, None, cam, asyncio.Semaphore(1), {}, None, native)
        entity.async_write_ha_state = Mock()
        return entity

    async def test_h264_streams_natively(self):
        entity = self.make_camera()
        entity._source._decoder = SimpleNamespace(codec="h264")
        entity._codec_seen()
        self.assertEqual(entity._attr_supported_features, camera.CameraEntityFeature.STREAM)
        self.assertIn("/api/hikconnect_intercom/stream/", await entity.stream_source())

    async def test_hevc_falls_back_to_mjpeg(self):
        entity = self.make_camera()
        entity.entity_id = "camera.door"
        entity._source._decoder = SimpleNamespace(codec="hevc")
        entity._codec_seen()
        self.assertEqual(entity._attr_supported_features, camera.CameraEntityFeature(0))
        self.assertIsNone(await entity.stream_source())
        entity.async_write_ha_state.assert_called_once()

    async def test_disabled_option_never_streams(self):
        entity = self.make_camera(native=frozenset())
        entity._source._decoder = SimpleNamespace(codec="h264")
        self.assertEqual(entity._features(), camera.CameraEntityFeature(0))
        self.assertIsNone(await entity.stream_source())

    async def test_hevc_streams_natively_when_included(self):
        entity = self.make_camera(native=frozenset({"h264", "hevc"}))
        entity._source._decoder = SimpleNamespace(codec="hevc")
        entity._codec_seen()
        self.assertEqual(entity._attr_supported_features, camera.CameraEntityFeature.STREAM)
        self.assertIsNotNone(await entity.stream_source())

    def test_legacy_boolean_option_maps_to_mode(self):
        const = load_module("const")
        self.assertEqual(const.native_stream_mode({"native_stream": True}), "h264")
        self.assertEqual(const.native_stream_mode({"native_stream": False}), "off")
        self.assertEqual(const.native_stream_mode({}), "h264")


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def setup(self, components, options=None):
        cam = SimpleNamespace(serial="S", channel=1, slot=1, name="Door", local_ip="192.0.2.1")
        hass = SimpleNamespace(
            data={"hikconnect_intercom": {"e": {
                "client": None, "quality": {}, "status_coordinator": None, "cameras": [cam],
                "door_presence": {},
            }}},
            config=SimpleNamespace(components=set(components)),
            http=SimpleNamespace(register_view=Mock()),
        )
        entry = SimpleNamespace(entry_id="e", options=options or {})
        added = []
        await camera.async_setup_entry(hass, entry, added.extend)
        return hass, added[0]

    async def test_native_without_stream_or_go2rtc_stays_mjpeg(self):
        hass, entity = await self.setup({"camera", "ffmpeg"})
        self.assertEqual(entity._native, frozenset())
        hass.http.register_view.assert_not_called()

    async def test_native_with_go2rtc(self):
        hass, entity = await self.setup({"go2rtc"})
        self.assertEqual(entity._native, frozenset({"h264"}))
        hass.http.register_view.assert_called_once()


if __name__ == "__main__":
    unittest.main()
