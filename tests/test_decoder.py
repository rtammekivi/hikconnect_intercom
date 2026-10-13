"""Synthetic RTP/AES fixtures; no captured household video or real keys."""

import struct
import unittest

from Crypto.Cipher import AES

from tests._modules import load_module

m = load_module("lib.hik_decoder")


def packet(nal, seq=0, enc=False):
    inner = bytes([0x90 if enc else 0x80, 0x60]) + struct.pack(">H", seq) + b"\0" * 8
    if enc:
        inner += bytes.fromhex("400000028006000111210201")
    p = b"\x80\x60" + b"\0" * 10 + b"\x0d" + inner + nal
    return b"$\x01" + struct.pack(">H", len(p)) + p


class DecoderTests(unittest.TestCase):
    def test_intercom_h264(self):
        d = m.HikStreamDecoder()
        raw = (
            packet(b"\x67sps")
            + packet(b"\x68pps", 1)
            + packet(b"\x7c\x85abc", 2)
            + packet(b"\x7c\x45def", 3)
        )
        for v in raw:
            d.feed(bytes([v]))
        self.assertEqual(d.codec, "h264")
        self.assertEqual(d.take(), b"\0\0\0\1\x67sps\0\0\0\1\x68pps\0\0\0\1\x65abcdef")

    def test_hevc_encryption(self):
        k = b"TESTKEY"
        body = b"0123456789abcdef" * 300 + b"tail"
        n = 4096
        wire = (
            b"\x40\x01"
            + AES.new(k.ljust(16, b"\0"), AES.MODE_ECB).encrypt(body[:n])
            + body[n:]
        )
        d = m.HikStreamDecoder(video_key=k.decode())
        d.feed(packet(wire, enc=True))
        self.assertEqual(d.take(), b"\0\0\0\1\x40\x01" + body)

    def test_encrypted_h264(self):
        k = b"TESTKEY"
        nal = b"\x67" + b"X" * 31 + b"tail"
        encrypted = (
            b"\x67"
            + AES.new(k.ljust(16, b"\0"), AES.MODE_ECB).encrypt(nal[:32])
            + nal[32:]
        )
        d = m.HikStreamDecoder(video_key=k.decode())
        d.feed(packet(encrypted, enc=True))
        self.assertEqual(d.take(), b"\0\0\0\1" + nal)

    def test_encrypted_short_h264_pps(self):
        d = m.HikStreamDecoder(video_key="TESTKEY")
        d.feed(packet(b"\x68\x68\xee\x3c\x80", enc=True))
        self.assertEqual(d.take(), b"\0\0\0\1\x68\xee\x3c\x80")

    def test_rtp_padding(self):
        raw = bytearray(packet(b"\x67sps" + b"\0\0\0\4"))
        raw[17] |= 32
        d = m.HikStreamDecoder()
        d.feed(raw)
        self.assertEqual(d.take(), b"\0\0\0\1\x67sps")

    def test_lost_fragment(self):
        d = m.HikStreamDecoder()
        d.feed(
            packet(b"\x67sps") + packet(b"\x7c\x85abc", 1) + packet(b"\x7c\x45def", 3)
        )
        self.assertEqual(d.take(), b"\0\0\0\1\x67sps")

    def test_missing_key(self):
        d = m.HikStreamDecoder()
        d.feed(packet(b"\x40\x01" + b"X" * 16, enc=True))
        self.assertTrue(d.requires_key)
        self.assertEqual(d.take(), b"")

    def test_hevc_fragments(self):
        decoder = m.HikStreamDecoder()
        decoder.feed(packet(b"\x40\x01vps"))
        decoder.feed(packet(b"\x62\x01\x93first", 1))
        decoder.feed(packet(b"\x62\x01\x53last", 2))
        self.assertEqual(
            decoder.take(), b"\0\0\0\1\x40\x01vps\0\0\0\1\x26\x01firstlast"
        )

    def test_aggregation_packet(self):
        decoder = m.HikStreamDecoder()
        decoder.feed(packet(b"\x67sps"))
        decoder.take()
        decoder.feed(packet(b"\x78\x00\x04\x68pps\x00\x04\x65idr", 1))
        self.assertEqual(decoder.take(), b"\0\0\0\1\x68pps\0\0\0\1\x65idr")

    def test_sequence_number_wrap(self):
        decoder = m.HikStreamDecoder()
        decoder.feed(packet(b"\x67sps", 65534))
        decoder.take()
        decoder.feed(packet(b"\x7c\x85first", 65535))
        decoder.feed(packet(b"\x7c\x45last", 0))
        self.assertEqual(decoder.take(), b"\0\0\0\1\x65firstlast")

    def test_other_interleave_is_ignored(self):
        decoder = m.HikStreamDecoder(interleave=4)
        decoder.feed(packet(b"\x67other"))
        self.assertEqual(decoder.take(), b"")
        raw = bytearray(packet(b"\x67sps"))
        raw[1] = 4
        decoder.feed(raw)
        self.assertEqual(decoder.take(), b"\0\0\0\1\x67sps")

    def test_metadata_extension_is_not_video(self):
        decoder = m.HikStreamDecoder(video_key="TESTKEY")
        raw = bytearray(packet(b"\x40\x01metadata", enc=True))
        raw[29:31] = b"\x10\x00"
        decoder.feed(raw)
        self.assertEqual(decoder.take(), b"")
        self.assertIsNone(decoder.codec)

    def test_invalid_padding_is_ignored(self):
        decoder = m.HikStreamDecoder()
        for padding in (0, 255):
            raw = bytearray(packet(b"\x67sps" + bytes([padding])))
            raw[17] |= 32
            decoder.feed(raw)
        self.assertEqual(decoder.take(), b"")


if __name__ == "__main__":
    unittest.main()
