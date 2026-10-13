"""Call commands, ring history and station status parsing."""

import json
import unittest
from unittest.mock import Mock

from tests._modules import load_module

api = load_module("hikconnect_api")


def _client(isapi_serials=()):
    client = api.HikConnectClient("test-account", "test-password")
    client._username = "user name"
    client._isapi_call_serials = set(isapi_serials)
    return client


class CallCommandTests(unittest.TestCase):
    def test_isapi_station_uses_call_signal(self):
        client = _client({"S1"})
        client.isapi = Mock(return_value={"resultCode": "0", "data": '{"statusCode": 1}'})
        client._put = Mock()
        for method, cmd_type in (
            ("answer_call", "answer"),
            ("reject_call", "reject"),
            ("hangup_call", "hangUp"),
        ):
            with self.subTest(method=method):
                getattr(client, method)("S1")
                serial, verb, path, body = client.isapi.call_args.args
                self.assertEqual(
                    (serial, verb, path),
                    ("S1", "PUT", "/ISAPI/VideoIntercom/callSignal?format=json"),
                )
                self.assertEqual(
                    json.loads(body),
                    {"CallSignal": {"cmdType": cmd_type, "sessionId": "user name"}},
                )
        client._put.assert_not_called()

    def test_call_signal_rejection_raises(self):
        client = _client({"S1"})
        client.isapi = Mock(
            return_value={
                "resultCode": "0",
                "data": '{"statusCode": "4", "subStatusCode": "invalidOperation"}',
            }
        )
        with self.assertRaisesRegex(api.HikConnectError, "invalidOperation"):
            client.hangup_call("S1")

    def test_legacy_station_sends_handler(self):
        client = _client()
        client._put = Mock(return_value={"meta": {"code": 200}, "data": '{"rc": 1}'})
        client.hangup_call("S2")
        client._put.assert_called_once_with(
            "/v3/devconfig/v1/call/S2/operation?cmdId=5&handler=user%20name"
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
        self.assertEqual(
            client.get_last_ring("S1"),
            {"time": 1791812071000, "msg_id": "m1", "calling_status": 1},
        )
        client._get.return_value = {"message": []}
        self.assertIsNone(client.get_last_ring("S1"))

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
