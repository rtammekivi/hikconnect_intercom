"""Regression tests for the restricted Russian CAS TLS compatibility path."""

import hashlib
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests._modules import load_module

cas = load_module("lib.cas")


def _verify_error(message, code=None):
    err = ssl.SSLCertVerificationError(message)
    if code is not None:
        err.verify_code = code
    return err


class RussiaCasCertificateTests(unittest.TestCase):
    def test_only_known_host_and_port(self):
        self.assertTrue(cas._is_russia_cas_endpoint("cas.ezvizru.com", 6500))
        self.assertTrue(cas._is_russia_cas_endpoint("CAS.EZVIZRU.COM", 6500))
        for host, port in (
            ("cas.ezvizru.com", 443),
            ("cas.ezvizru.com.attacker.invalid", 6500),
            ("cas.ezvizlife.com", 6500),
            ("uscas.ezvizlife.com", 6500),
        ):
            with self.subTest(host=host, port=port):
                self.assertFalse(cas._is_russia_cas_endpoint(host, port))

    def test_only_hostname_mismatch_is_recognized(self):
        self.assertTrue(cas._is_certificate_hostname_mismatch_error(_verify_error(
            "Hostname mismatch, certificate is not valid for 'cas.ezvizru.com'."
        )))
        self.assertTrue(cas._is_certificate_hostname_mismatch_error(_verify_error(
            "hostname verification failed", 62
        )))
        self.assertFalse(cas._is_certificate_hostname_mismatch_error(_verify_error(
            "certificate has expired", 10
        )))
        self.assertFalse(cas._is_certificate_hostname_mismatch_error(_verify_error(
            "self-signed certificate", 18
        )))

    def test_pin_accepts_only_exact_der_bytes(self):
        peer = b"synthetic-test-certificate"
        sock = SimpleNamespace(getpeercert=Mock(return_value=peer))
        with patch.object(cas, "RUSSIA_CAS_CERT_SHA256", hashlib.sha256(peer).hexdigest()):
            cas._verify_russia_cas_certificate(sock)
            sock.getpeercert.assert_called_once_with(binary_form=True)

    def test_pin_rejects_wrong_or_missing_certificate(self):
        for peer in (b"different-certificate", None, b""):
            with self.subTest(peer=peer):
                sock = SimpleNamespace(getpeercert=Mock(return_value=peer))
                with self.assertRaises(cas.PyEzvizError):
                    cas._verify_russia_cas_certificate(sock)


class RussiaCasHandshakeTests(unittest.TestCase):
    def _run(self, host, port, reason, *, strict=False, pin_ok=True):
        # The first TLS handshake rejects the legacy certificate. The fallback
        # exposes certificate bytes and must authenticate them before sending.
        peer = b"synthetic-test-certificate"
        sockets = [Mock(), Mock()]
        sockets[1].getpeercert.return_value = peer
        verified_ctx = Mock()
        verified_ctx.wrap_socket.side_effect = reason
        fallback_ctx = Mock()
        fallback_ctx.wrap_socket.return_value = sockets[1]
        token = {"service_urls": {"sysConf": {15: host, 16: port}}}
        client = cas.EzvizCAS(token, verify_tls_certificate=strict)
        pin = hashlib.sha256(peer).hexdigest() if pin_ok else "0" * 64
        with (
            patch.object(cas.socket, "create_connection", side_effect=sockets) as conn,
            patch.object(cas, "_cas_tls_context", side_effect=[verified_ctx, fallback_ctx]) as contexts,
            patch.object(cas, "RUSSIA_CAS_CERT_SHA256", pin),
            patch.object(cas, "_send_all") as send,
        ):
            if strict or host != "cas.ezvizru.com" or port != 6500 or not pin_ok or (
                not cas._is_certificate_hostname_mismatch_error(reason)
                and not cas._is_certificate_expired_error(reason)
            ):
                with self.assertRaises((cas.PyEzvizError, ssl.SSLError)):
                    client._send_cas_payload(b"request", host=host, port=port,
                                             use_tls=True, expect_frame=False)
                send.assert_not_called()
            else:
                client._send_cas_payload(b"request", host=host, port=port,
                                         use_tls=True, expect_frame=False)
                send.assert_called_once_with(sockets[1], b"request")
                sockets[1].getpeercert.assert_called_once_with(binary_form=True)
            return contexts.call_count, conn.call_count

    def test_matched_russian_host_and_pin_uses_fallback(self):
        self.assertEqual(self._run("cas.ezvizru.com", 6500, _verify_error(
            "Hostname mismatch, certificate is not valid for 'cas.ezvizru.com'."
        )), (2, 2))

    def test_wrong_pin_fails_closed_before_sending(self):
        self.assertEqual(self._run("cas.ezvizru.com", 6500, _verify_error(
            "Hostname mismatch"
        ), pin_ok=False), (2, 2))

    def test_wrong_host_or_port_does_not_fallback(self):
        for host, port in (("cas.ezvizru.com", 443), ("uscas.ezvizlife.com", 6500)):
            with self.subTest(host=host, port=port):
                self.assertEqual(self._run(host, port, _verify_error("Hostname mismatch")), (1, 1))

    def test_unrelated_verification_error_does_not_fallback(self):
        self.assertEqual(self._run("cas.ezvizru.com", 6500, _verify_error(
            "self signed certificate", 18
        )), (1, 1))

    def test_strict_verification_disables_fallback(self):
        self.assertEqual(self._run("cas.ezvizru.com", 6500, _verify_error(
            "Hostname mismatch"
        ), strict=True), (1, 1))


if __name__ == "__main__":
    unittest.main()
