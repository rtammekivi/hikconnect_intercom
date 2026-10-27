"""Call commands, ring history and station status parsing."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from tests._modules import load_event, load_module

api = load_module("hikconnect_api")


def _client(isapi_serials=()):
    client = api.HikConnectClient("test-account", "test-password")
    client._username = "user name"
    client._isapi_call_serials = set(isapi_serials)
    return client


class CallCommandTests(unittest.TestCase):
    OK = {"meta": {"code": 200}, "data": '{"statusCode": 1}'}
    IDLE = {
        "meta": {"code": 200},
        "data": '{"statusCode": "4", "subStatusCode": "invalidOperation"}',
    }

    @staticmethod
    def _signals(call):
        sent = []
        for c in call.call_args_list:
            method, path, factory = c.args
            form = factory()
            head, body = form["apiData"].split("\r\n", 1)
            sent.append((method, path, form["apiKey"], form["channelNo"], head, json.loads(body)))
        return sent

    def test_isapi_station_matches_app_request(self):
        client = _client({"S1"})
        client._call = Mock(return_value=self.OK)
        client.answer_call("S1")
        self.assertEqual(
            self._signals(client._call),
            [(
                "POST", "/v3/userdevices/v1/isapi", "100113", 1,
                "PUT /ISAPI/VideoIntercom/callSignal?format=json",
                {"CallSignal": {"channelID": 0, "cmdType": "answer", "sessionId": "user name"}},
            )],
        )

    def test_end_call_picks_command_from_status(self):
        for status, first in (("ringing", "reject"), ("call in progress", "hangUp"), (None, "hangUp")):
            with self.subTest(status=status):
                client = _client({"S1"})
                client._call = Mock(return_value=self.OK)
                client.end_call("S1", status)
                cmds = [s[5]["CallSignal"]["cmdType"] for s in self._signals(client._call)]
                self.assertEqual(cmds, [first])

    def test_end_call_falls_back_to_other_command(self):
        client = _client({"S1"})
        client._call = Mock(side_effect=[self.IDLE, self.OK])
        client.end_call("S1", "call in progress")
        cmds = [s[5]["CallSignal"]["cmdType"] for s in self._signals(client._call)]
        self.assertEqual(cmds, ["hangUp", "reject"])

    def test_end_call_reports_first_rejection(self):
        client = _client({"S1"})
        client._call = Mock(return_value=self.IDLE)
        with self.assertRaisesRegex(api.HikConnectError, "hangUp rejected.*invalidOperation"):
            client.end_call("S1")

    def test_legacy_station_sends_handler(self):
        client = _client()
        client._put = Mock(return_value={"meta": {"code": 200}, "data": '{"rc": 1}'})
        client.end_call("S2", "ringing")
        client._put.assert_called_once_with(
            "/v3/devconfig/v1/call/S2/operation?cmdId=3&handler=user%20name"
        )

    def test_support_isapi_parsing(self):
        parse = api.HikConnectClient._parse_support_isapi
        self.assertTrue(parse('{"145": "1"}'))
        self.assertTrue(parse({"145": 1}))
        self.assertFalse(parse('{"145": "0"}'))
        self.assertFalse(parse("not json"))
        self.assertFalse(parse(None))


class StationReadTests(unittest.TestCase):
    def test_last_ring(self):
        client = _client()
        client._get = Mock(
            return_value={
                "message": [
                    {
                        "subType": 2701,
                        "msgId": "m1",
                        "time": 1791812071000,
                        "ext": {"callingStatus": 1},
                    }
                ]
            }
        )
        wall = datetime.now(timezone.utc) + timedelta(hours=2)
        client.isapi = Mock(
            return_value={"data": f"<localTime>{wall:%Y-%m-%dT%H:%M:%S}+02:00</localTime>"}
        )
        ring = client.get_last_ring("S1")
        self.assertEqual((ring["msg_id"], ring["calling_status"]), ("m1", 1))
        self.assertAlmostEqual(ring["time"], 1791812071000 - 7_200_000, delta=2000)
        self.assertEqual(client.get_last_ring("S1"), ring)
        client._get.return_value["message"][0]["ext"]["callingStatus"] = 2
        self.assertEqual(client.get_last_ring("S1")["calling_status"], 2)
        client.isapi.assert_called_once()
        client._get.return_value = {"message": []}
        self.assertIsNone(client.get_last_ring("S1"))

    def test_last_ring_retries_when_device_time_unavailable(self):
        client = _client()
        client._get = Mock(return_value={"message": [{"subType": 2701, "msgId": "m1", "time": 5000}]})
        client.isapi = Mock(return_value={"data": ""})
        self.assertIsNone(client.get_last_ring("S1"))
        now = datetime.now(timezone.utc)
        client.isapi.return_value = {"data": f"<localTime>{now:%Y-%m-%dT%H:%M:%S}+00:00</localTime>"}
        self.assertAlmostEqual(client.get_last_ring("S1")["time"], 5000, delta=2000)

    def test_operation_time(self):
        client = _client()
        client.isapi = Mock(
            return_value={
                "data": '<OperationTime xmlns="http://www.isapi.org/ver20/XMLSchema">'
                "<maxMonitoringTime>30</maxMonitoringTime><maxRingTime>45</maxRingTime>"
                "</OperationTime>"
            }
        )
        self.assertEqual(
            client.get_operation_time("S1"),
            {"maxMonitoringTime": 30, "maxRingTime": 45},
        )
        client.isapi.return_value = {"data": '{"statusCode": "4"}'}
        self.assertEqual(client.get_operation_time("S1"), {})

    def test_work_status(self):
        client = _client()
        client.isapi = Mock(
            return_value={
                "data": '{"AcsWorkStatus": {"privateSipStatus": "connect", "TFCardStatus": "mounted"}}'
            }
        )
        self.assertEqual(
            client.get_work_status("S1"),
            {"privateSipStatus": "connect", "TFCardStatus": "mounted"},
        )
        client.isapi.return_value = {"data": '{"statusCode": "4"}'}
        self.assertEqual(client.get_work_status("S1"), {})


if __name__ == "__main__":
    unittest.main()


class CallingDoorTests(unittest.TestCase):
    @staticmethod
    def _status(call_status, dev_no=0, dev_type=1):
        info = {"devNo": dev_no, "devType": dev_type, "lockNum": 1}
        return {"meta": {"code": 200}, "data": json.dumps({"callStatus": call_status, "callerInfo": info})}

    def test_maps_dev_no_to_channel(self):
        client = _client()
        client._get = Mock(return_value=self._status(2, dev_no=1))
        self.assertEqual(client.calling_door("S1"), 2)
        client._get.return_value = self._status(3, dev_no=0)
        self.assertEqual(client.calling_door("S1"), 1)

    def test_prefers_channel_learned_from_push(self):
        client = _client()
        client.remember_caller_channel("S1", {"devNo": 1, "channelno": 5})
        client.remember_caller_channel("S1", {"devNo": 2, "channelno": "x"})
        client._get = Mock(return_value=self._status(2, dev_no=1))
        self.assertEqual(client.calling_door("S1"), 5)
        client._get.return_value = self._status(2, dev_no=2)
        self.assertEqual(client.calling_door("S1"), 3)
        client._get.return_value = self._status(2, dev_no=1)
        self.assertEqual(client.calling_door("S2"), 2)

    def test_remembers_caller_after_call_ends(self):
        client = _client()
        client._get = Mock(return_value=self._status(2, dev_no=1))
        client.get_call_status("S1")
        seen_at = client._last_callers["S1"][0]
        client.get_last_ring = Mock(return_value={"time": (seen_at - 5) * 1000})
        client._get.return_value = self._status(1, dev_no=0, dev_type=-1)
        self.assertEqual(client.calling_door("S1"), 2)
        first_seen, _, info = client._last_callers["S1"]
        client._last_callers["S1"] = (first_seen - 600, seen_at - 200, info)
        client.get_last_ring.return_value = {"time": (first_seen - 605) * 1000}
        self.assertEqual(client.calling_door("S1"), 2)
        client._last_callers["S1"] = (first_seen - 600, seen_at - 301, info)
        self.assertIsNone(client.calling_door("S1"))

    def test_newer_ring_hides_previous_caller(self):
        client = _client()
        client._get = Mock(return_value=self._status(2, dev_no=1))
        client.get_call_status("S1")
        seen_at = client._last_callers["S1"][0]
        client.get_last_ring = Mock(return_value={"time": (seen_at + 30) * 1000})
        client._get.return_value = self._status(1, dev_no=0, dev_type=-1)
        self.assertIsNone(client.calling_door("S1"))

    def test_ring_after_first_sighting_is_refused(self):
        client = _client()
        client._get = Mock(return_value=self._status(2, dev_no=1))
        client.get_call_status("S1")
        first_seen = client._last_callers["S1"][0]
        client.get_call_status("S1")
        self.assertEqual(client._last_callers["S1"][0], first_seen)
        client.get_last_ring = Mock(return_value={"time": (first_seen + 1) * 1000})
        client._get.return_value = self._status(1, dev_no=0, dev_type=-1)
        self.assertIsNone(client.calling_door("S1"))

    def test_new_caller_without_idle_poll_resets_first_sighting(self):
        client = _client()
        client._get = Mock(return_value=self._status(2, dev_no=0))
        client.get_call_status("S1")
        client._last_callers["S1"] = (0.0, 0.0, client._last_callers["S1"][2])
        client._get.return_value = self._status(2, dev_no=1)
        client.get_call_status("S1")
        self.assertGreater(client._last_callers["S1"][0], 0.0)

    def test_unidentified_call_clears_previous_caller(self):
        client = _client()
        client._get = Mock(return_value=self._status(2, dev_no=1))
        client.get_call_status("S1")
        client._get.return_value = self._status(2, dev_no=0, dev_type=-1)
        client.get_call_status("S1")
        client.get_last_ring = Mock(return_value=None)
        client._get.return_value = self._status(1, dev_no=0, dev_type=-1)
        self.assertIsNone(client.calling_door("S1"))

    def test_unclear_status_does_not_use_previous_caller(self):
        client = _client()
        client._get = Mock(return_value=self._status(2, dev_no=0))
        client.get_call_status("S1")
        client._get.return_value = {"meta": {"code": 500}}
        self.assertIsNone(client.calling_door("S1"))
        client._get.return_value = self._status(2, dev_no=0, dev_type=-1)
        self.assertIsNone(client.calling_door("S1"))

    def test_no_call_seen(self):
        client = _client()
        client._get = Mock(return_value=self._status(1, dev_no=0, dev_type=-1))
        self.assertIsNone(client.calling_door("S1"))


class DoorbellEventTests(unittest.TestCase):
    def _fire(self, updates):
        coordinator = SimpleNamespace(data={"S1": {"status": "idle"}})
        entity = load_event().HikDoorbellEvent(coordinator, "S1")
        entity._trigger_event = Mock()
        for data in updates:
            coordinator.data = {"S1": data}
            entity._handle_coordinator_update()
        return entity._trigger_event.call_count

    def test_rings_once_per_call(self):
        self.assertEqual(self._fire([{"status": "ringing"}, {"status": "ringing"}]), 1)

    def test_stale_poll_after_button_does_not_ring_again(self):
        updates = [
            {"status": "ringing", "polled_at": 1.0},
            {"status": "idle", "local_at": 3.0},
            {"status": "ringing", "polled_at": 2.0},
            {"status": "idle", "polled_at": 4.0},
            {"status": "ringing", "polled_at": 5.0},
        ]
        self.assertEqual(self._fire(updates), 2)

    def test_new_ring_right_after_local_hang_up_fires(self):
        updates = [
            {"status": "ringing", "polled_at": 1.0},
            {"status": "idle", "local_at": 3.0},
            {"status": "ringing", "polled_at": 4.0},
        ]
        self.assertEqual(self._fire(updates), 2)

    def test_ring_carries_caller_info(self):
        coordinator = SimpleNamespace(data={"S1": {"status": "idle"}})
        entity = load_event().HikDoorbellEvent(coordinator, "S1")
        entity._trigger_event = Mock()
        caller = {"buildingNo": 8, "unitNo": 1, "devNo": 1, "devType": 1}
        coordinator.data = {"S1": {"status": "ringing", "info": caller}}
        entity._handle_coordinator_update()
        entity._trigger_event.assert_called_once_with("ring", caller)

    def test_push_ring_fires_once_with_the_door(self):
        coordinator = SimpleNamespace(data={"S1": {"status": "idle"}})
        entity = load_event().HikDoorbellEvent(coordinator, "S1")
        entity._trigger_event = Mock()
        caller = {"devNo": 1, "channelno": 2, "devType": 1}
        for data in (
            {"status": "ringing", "info": caller, "polled_at": 5.0, "pushed_at": 5.0},
            {"status": "ringing", "info": {"devNo": 1}, "polled_at": 6.0},
            {"status": "idle", "info": caller, "polled_at": 5.0, "pushed_at": 5.0, "local_at": 7.0},
            {"status": "idle", "info": {}, "polled_at": 8.0},
        ):
            coordinator.data = {"S1": data}
            entity._handle_coordinator_update()
        entity._trigger_event.assert_called_once_with("ring", caller)

    def test_failed_poll_does_not_ring_again(self):
        updates = [{"status": "ringing"}, {"status": "unknown"}, {"status": "ringing"}]
        self.assertEqual(self._fire(updates), 1)
