import json
import sys
import unittest
from types import ModuleType
from unittest.mock import Mock, patch

from tests._modules import load_module

api = load_module("hikconnect_api")


def _load_diagnostics():
    interfaces = {
        "homeassistant.components.diagnostics": {"async_redact_data": lambda data, keys: data},
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.helpers": {"entity_registry": None},
    }
    modules = {}
    for name, attributes in interfaces.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        modules[name] = module
    with patch.dict(sys.modules, modules):
        return load_module("diagnostics")


diagnostics = _load_diagnostics()
WATCH_POINT = {"WatchPoint": {"deviveList": [{"channelNo": 1, "ipV4Address": "192.0.2.11"}]}}


class DiagnosticsTests(unittest.TestCase):
    def test_collects_cloud_and_isapi_data_per_station(self):
        client = Mock()
        client._get.side_effect = lambda path: (
            {"statusInfos": {"Q1": {"optionals": {"OSD": "[]"}}}, "connectionInfos": {}}
            if "pagelist" in path
            else {"cameraInfos": [{"channelNo": 1, "deviceSerial": "Q1"}]}
        )
        client.isapi.side_effect = lambda serial, method, path: (
            {"data": json.dumps(WATCH_POINT)} if "WatchPoint" in path else {"data": "<xml/>"}
        )
        dev = api.HikDevice("Q1", "Indoor", "192.0.2.2", "DS-KH6320")

        out = diagnostics.collect(client, [dev])["Q1"]

        self.assertEqual(out["status_info"], {"optionals": {"OSD": "[]"}})
        self.assertEqual(out["cameras_info"]["cameraInfos"][0]["channelNo"], 1)
        self.assertEqual(out["isapi"][diagnostics.ISAPI_PATHS[0]], WATCH_POINT)
        self.assertEqual(out["isapi"][diagnostics.ISAPI_PATHS[1]], "<xml/>")

    def test_failed_query_is_reported_not_raised(self):
        client = Mock()
        client._get.side_effect = api.HikConnectError("boom")
        client.isapi.side_effect = api.HikConnectError("offline")
        dev = api.HikDevice("Q1", "Indoor", "192.0.2.2", "DS-KH6320")

        out = diagnostics.collect(client, [dev])["Q1"]

        self.assertEqual(out["cameras_info"], {"error": "HikConnectError: boom"})
        self.assertEqual(out["status_info"], {"error": "HikConnectError: boom"})
        self.assertIn("offline", out["isapi"][diagnostics.ISAPI_PATHS[0]]["error"])

    def test_anonymize_replaces_serials_and_urls(self):
        data = {"Q1": {"unique_id": "hikconnect_intercom_Q1_ch2", "cover": "https://x/y?t=1"}}
        self.assertEqual(diagnostics.anonymize(data, ["Q1"]), {
            "STATION_1": {"unique_id": "hikconnect_intercom_STATION_1_ch2", "cover": "**REDACTED**"},
        })


if __name__ == "__main__":
    unittest.main()
