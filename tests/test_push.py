"""Push registration lifecycle: unregister on stop, re-register on re-login."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tests._modules import load_module, load_push

api = load_module("hikconnect_api")
push = load_push()


class _Response:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type="application/json"):
        return self._payload


class _Session:
    def __init__(self):
        self.posts = []

    def post(self, url, data=None, **kwargs):
        self.posts.append((url, data))
        return _Response({"status": 200})


def make_push():
    session = _Session()

    async def executor(fn, *args):
        return fn(*args)

    hass = SimpleNamespace(session=session, async_add_executor_job=executor)
    listener = push.HikConnectPush(hass, "https://api.example", "account", "password")
    listener._push_addr = "push.example"
    listener._client_id = "client-1"
    listener._session_id = "session-1"
    listener._username = "user"
    return listener, session


class PushLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_unregisters_once(self):
        listener, session = make_push()
        listener._client = SimpleNamespace(loop_stop=Mock(), disconnect=Mock())
        await listener.async_stop()
        await listener.async_stop()
        self.assertEqual(
            session.posts,
            [("https://push.example/api/push/stop", {
                "appKey": push.MQTT_APP_KEY, "clientId": "client-1", "clientType": 5,
                "sessionId": "session-1", "username": "user",
            })],
        )

    async def test_refresh_rebinds_same_client_to_new_session(self):
        listener, session = make_push()
        listener._login = AsyncMock(return_value=("https://api.example", "session-2", "user"))
        await listener.async_refresh_session()
        self.assertEqual(session.posts[0][0], "https://push.example/api/push/start")
        self.assertEqual(session.posts[0][1]["clientId"], "client-1")
        self.assertEqual(session.posts[0][1]["sessionId"], "session-2")
        self.assertEqual(listener._session_id, "session-2")

    async def test_refresh_after_stop_does_nothing(self):
        listener, session = make_push()
        await listener.async_stop()
        listener._login = AsyncMock()
        await listener.async_refresh_session()
        listener._login.assert_not_awaited()
        self.assertEqual([url for url, _ in session.posts], ["https://push.example/api/push/stop"])


class LoginListenerTests(unittest.TestCase):
    def test_login_notifies_listeners_until_removed(self):
        client = api.HikConnectClient("account", "password")
        client._raw = Mock(side_effect=lambda method, path, data=None: (
            {"meta": {"code": 200}, "loginSession": {"sessionId": "s"}, "loginUser": {"username": "u"}}
            if path.startswith("/v3/users/login")
            else {"systemConfigInfo": {"sysConf": ""}}
        ))
        listener = Mock()
        remove = client.add_login_listener(listener)
        client.login()
        remove()
        client.login()
        listener.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
