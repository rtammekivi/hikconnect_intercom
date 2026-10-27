"""Channel-99 long-link login, registration and event wiring."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests._modules import load_longlink

longlink = load_longlink()

LOGIN_OK = {
    "meta": {"code": 200},
    "loginSession": {"sessionId": "session-1"},
    "loginUser": {"userId": "user-1"},
}
SYSTEM_INFO = {"systemConfigInfo": {"pushDasDomain": "das.example", "pushDasPort": 8777}}
REGISTERED = {"meta": {"code": 200}}


def make_listener(replies, on_ring=None, on_hangup=None):
    listener = longlink.HikConnectLongLink(
        SimpleNamespace(), None, "account", "password", "https://api.example",
        on_ring or Mock(), on_hangup or Mock(),
    )
    listener._http.request = Mock(side_effect=[SimpleNamespace(json=lambda r=r: r, status_code=200) for r in replies])
    return listener


class LoginTests(unittest.TestCase):
    def test_login_registers_channel_99_with_the_app_identity(self):
        listener = make_listener([LOGIN_OK, SYSTEM_INFO, REGISTERED])
        listener._login()
        login = listener._http.request.call_args_list[0]
        self.assertEqual(login.args, ("POST", "https://api.example/v3/users/login/v5"))
        self.assertEqual(login.kwargs["data"]["pushRegisterJson"], '[{"channel":99}]')
        self.assertEqual(login.kwargs["data"]["featureCode"], longlink.long_link_feature_code("account"))
        self.assertEqual(len(login.kwargs["data"]["featureCode"]), 32)
        self.assertEqual(listener._http.headers["appId"], "HikConnect2022")
        self.assertEqual(listener._http.request.call_args_list[2].args[:2], ("PUT", "https://api.example/v3/push/token"))
        self.assertEqual((listener._session_id, listener._user_id), ("session-1", "user-1"))
        self.assertEqual(listener._http.headers["sessionId"], "session-1")
        self.assertEqual(listener._endpoint, ("das.example", 8777))

    def test_login_follows_region_redirect(self):
        redirect = {"meta": {"code": 1100}, "loginArea": {"apiDomain": "apiieu.example"}}
        listener = make_listener([redirect, LOGIN_OK, SYSTEM_INFO, REGISTERED])
        listener._login()
        self.assertEqual(listener._http.request.call_args_list[1].args[1], "https://apiieu.example/v3/users/login/v5")

    def test_rejected_registration_fails(self):
        listener = make_listener([LOGIN_OK, SYSTEM_INFO, {"meta": {"code": 404}}])
        with self.assertRaises(longlink.LongLinkError):
            listener._login()

    def test_failed_login_fails(self):
        listener = make_listener([{"meta": {"code": 1013}}])
        with self.assertRaises(longlink.LongLinkError):
            listener._login()

    def test_prepare_only_relogs_after_max_age(self):
        listener = make_listener([LOGIN_OK, SYSTEM_INFO, REGISTERED])
        listener._login()
        listener._prepare()
        self.assertEqual(listener._http.request.call_count, 3)
        listener._logged_in_at -= longlink._SESSION_MAX_AGE + 1
        listener._http.request.side_effect = [SimpleNamespace(json=lambda r=r: r, status_code=200) for r in (LOGIN_OK, SYSTEM_INFO, REGISTERED)]
        listener._prepare()
        self.assertEqual(listener._http.request.call_count, 6)


class StartTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_uses_hik_connect_serial_and_saved_state(self):
        on_ring = Mock()
        listener = make_listener([LOGIN_OK, SYSTEM_INFO, REGISTERED], on_ring)

        async def executor(fn, *args):
            return fn(*args)

        listener._hass = SimpleNamespace(async_add_executor_job=executor)
        listener._store = SimpleNamespace(async_load=AsyncMock(return_value={"device_id": "aa"}))
        with patch.object(longlink, "PushWorker") as worker:
            await listener.async_start()
        session = worker.call_args.args[0]()
        self.assertEqual(session.serial, f"MOBILE:HikConnect2022:user-1:{longlink.long_link_feature_code('account')}".encode())
        self.assertEqual(session.endpoint, ("das.example", 8777))
        self.assertEqual(session.state, {"device_id": "aa"})
        self.assertEqual(session.credentials_input(), "session-1")
        worker.return_value.start.assert_called_once_with()
        session.on_message(b'{"pushType":2,"soundType":2701,"alert":"Your doorbell is ringing"}')
        on_ring.assert_called_once_with([""], {})


class MessageTests(unittest.TestCase):
    def test_ring_and_hang_up_have_their_own_callbacks(self):
        on_ring, on_hangup = Mock(), Mock()
        listener = make_listener([], on_ring, on_hangup)
        for payload in (
            b'{"pushType":99}',
            b"{}",
            b"[]",
            b"\x00\x01binary",
        ):
            with self.subTest(payload=payload):
                listener._on_message(payload)
        on_ring.assert_not_called()
        on_hangup.assert_not_called()
        listener._on_message(b'{"pushType":2,"soundType":2701}')
        on_ring.assert_called_once_with([""], {})
        on_hangup.assert_not_called()
        listener._on_message(b'{"pushType":3,"alert":"hang up","ext":"3,20,2026-10-27 16:34:22,Q1,0,,2701,4"}')
        on_hangup.assert_called_once_with(["3", "20", "2026-10-27 16:34:22", "Q1", "0", "", "2701", "4"])
        on_ring.assert_called_once_with([""], {})

    def test_ring_carries_the_calling_door(self):
        on_ring = Mock()
        listener = make_listener([], on_ring)
        caller = (
            "eyJkZXZUeXBlIjoxLCJoYW5kbGVyIjoiIiwiY2FsbGVyIjoiMS04LTEtMCIsInpvbmVObyI6MSwidW5pdE5vIjox"
            "LCJjaGFubmVsbm8iOjEsImxvY2tOdW0iOjEsImJ1aWxkaW5nTm8iOjgsImRldk5vIjowLCJ2b2lwTm8iOiIwIiwi"
            "Zmxvb3JObyI6MH0="
        )
        ext = f"14,2026-10-27 16:34:01,Q1,0,4,20261027163401-Q1-1-00000-1,1,,0,,0,,,DS-KH6320-WTE1,1-8-1-0,{caller}"
        listener._on_message(('{"pushType":2,"soundType":2701,"ext":"%s"}' % ext).encode())
        fields, info = on_ring.call_args.args
        self.assertIn("Q1", fields)
        self.assertEqual(info, {
            "devType": 1, "caller": "1-8-1-0", "zoneNo": 1, "unitNo": 1, "channelno": 1,
            "lockNum": 1, "buildingNo": 8, "devNo": 0, "voipNo": "0", "floorNo": 0,
        })


if __name__ == "__main__":
    unittest.main()
