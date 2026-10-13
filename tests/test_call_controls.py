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
            {"status": "ringing"},
            {"status": "idle", "local": True},
            {"status": "ringing"},
            {"status": "idle"},
            {"status": "ringing"},
        ]
        self.assertEqual(self._fire(updates), 2)

    def test_failed_poll_does_not_ring_again(self):
        updates = [{"status": "ringing"}, {"status": "unknown"}, {"status": "ringing"}]
        self.assertEqual(self._fire(updates), 1)
