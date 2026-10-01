"""Read-only ISAPI verification-code lookup, including masked responses."""

import unittest
from unittest.mock import Mock

from tests._modules import load_module

api = load_module("hikconnect_api")


class VideoKeyTests(unittest.TestCase):
    def test_namespaced_verification_code(self):
        client = api.HikConnectClient("test-account", "test-password")
        client.isapi = Mock(
            return_value={
                "data": '<EZVIZ xmlns="urn:test"><verificationCode> TESTKEY </verificationCode></EZVIZ>'
            }
        )
        self.assertEqual(client.get_video_key("TEST_SERIAL"), "TESTKEY")
        client.isapi.assert_called_once_with(
            "TEST_SERIAL", "GET", "/ISAPI/System/Network/EZVIZ"
        )

    def test_missing_or_masked_verification_code(self):
        for xml in (
            "<EZVIZ/>",
            "<EZVIZ><verificationCode>******</verificationCode></EZVIZ>",
            "<EZVIZ><verificationCode/></EZVIZ>",
        ):
            with self.subTest(xml=xml):
                client = api.HikConnectClient("test-account", "test-password")
                client.isapi = Mock(return_value={"data": xml})
                with self.assertRaisesRegex(api.HikConnectError, "unavailable"):
                    client.get_video_key("TEST_SERIAL")


if __name__ == "__main__":
    unittest.main()
