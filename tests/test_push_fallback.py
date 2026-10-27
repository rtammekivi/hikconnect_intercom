"""The legacy push reports when it had to leave the cloud-assigned push host."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp

from tests._modules import load_push

push = load_push()


def make_push(register_side_effect):
    async def executor(fn, *args):
        return fn(*args)

    listener = push.HikConnectPush(
        SimpleNamespace(session=None, async_add_executor_job=executor),
        "https://api.example", "account", "password",
    )
    listener._login = AsyncMock(return_value=("https://api.example", "session", "user"))
    listener._get_push_addr = AsyncMock(return_value="pusheu.hik-connect.com")
    listener._register = AsyncMock(side_effect=register_side_effect)
    listener._start_push = AsyncMock()
    listener._connect = lambda broker, client_id: None
    return listener


class FallbackHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_assigned_host_is_not_a_fallback(self):
        listener = make_push([("client", "tcp://pusheu.ezvizlife.com:1882")])
        await listener.async_start()
        self.assertFalse(listener.used_fallback_host)

    async def test_unreachable_assigned_host_is_a_fallback(self):
        listener = make_push([
            aiohttp.ClientError("Server disconnected"),
            ("client", "tcp://pusheu.ezvizlife.com:1882"),
        ])
        await listener.async_start()
        self.assertTrue(listener.used_fallback_host)
        self.assertEqual(listener._register.await_args.args[1], "pusheu.ezvizlife.com")


if __name__ == "__main__":
    unittest.main()
