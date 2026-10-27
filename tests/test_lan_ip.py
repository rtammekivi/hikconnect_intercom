"""Regression coverage for garbled station LAN addresses from the cloud."""

import unittest
from types import SimpleNamespace

from tests._modules import load_camera, load_module

api = load_module("hikconnect_api")
camera = load_camera()


class CleanIpTests(unittest.TestCase):
    def test_keeps_valid_addresses(self):
        self.assertEqual(api.HikConnectClient._clean_ip("192.168.100.234"), "192.168.100.234")
        self.assertEqual(api.HikConnectClient._clean_ip(" 10.0.0.5 "), "10.0.0.5")

    def test_rejects_garbled_and_placeholder_addresses(self):
        for value in (
            "1192.168.12.114", "11255.255.224.0", "255.255.224.0",
            "0.0.0.0", "", None, "fe80::1", "127.0.0.1",
        ):
            with self.subTest(value=value):
                self.assertIsNone(api.HikConnectClient._clean_ip(value))


class CurrentIpTests(unittest.TestCase):
    def make_stream(self, status):
        cam = SimpleNamespace(
            serial="TEST_STATION", channel=1, name="Test door", local_ip="192.0.2.1"
        )
        return camera._ChannelStream(
            None, None, cam, None, {}, "test", SimpleNamespace(data=status)
        )

    def test_keeps_last_seen_address_when_cloud_reports_none(self):
        status = {"TEST_STATION": {"local_ip": "192.0.2.50"}}
        stream = self.make_stream(status)
        self.assertEqual(stream._current_ip(), "192.0.2.50")
        status["TEST_STATION"]["local_ip"] = None
        self.assertEqual(stream._current_ip(), "192.0.2.50")


if __name__ == "__main__":
    unittest.main()
