"""Regression coverage for scarce stream slots and cancellation cleanup."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests._modules import load_camera

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


if __name__ == "__main__":
    unittest.main()
