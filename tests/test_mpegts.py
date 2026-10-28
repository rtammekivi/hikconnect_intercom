import unittest

from tests._modules import load_module

m = load_module("lib.mpegts")

IDR = b"\0\0\0\1\x67sps\0\0\0\1\x68pps\0\0\0\1\x65" + bytes(range(256)) * 3
P = b"\0\0\0\1\x41" + b"p" * 50


def packets(stream):
    return [stream[i:i + 188] for i in range(0, len(stream), 188)]


def pts(packet):
    afc = packet[3] >> 4 & 3
    pes = packet[4 + (1 + packet[4] if afc == 3 else 0):]
    b = pes[9:14]
    return (b[0] >> 1 & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


class TsMuxerTests(unittest.TestCase):
    def test_packets_are_aligned_and_continuous(self):
        mux = m.TsMuxer("h264")
        out = mux.mux(1000, IDR, True) + mux.mux(4600, P, False)
        self.assertEqual(len(out) % 188, 0)
        video = [p for p in packets(out) if p[0] == 0x47 and (p[1] & 0x1F) << 8 | p[2] == 0x100]
        ccs = [p[3] & 0x0F for p in video]
        self.assertEqual(ccs, list(range(len(video))))

    def test_psi_precedes_keyframes_only(self):
        mux = m.TsMuxer("hevc")
        key = packets(mux.mux(0, IDR, True))
        delta = packets(mux.mux(3600, P, False))
        self.assertEqual([p[2] for p in key[:2]], [0x00, 0x00])
        self.assertEqual(key[1][1] & 0x1F, 0x10)
        self.assertEqual(key[1][17], 0x24)
        self.assertEqual(delta[0][1:3], b"\x41\x00")

    def test_pts_follows_rtp_clock_across_wrap(self):
        mux = m.TsMuxer("h264")
        first = pts(packets(mux.mux(2**32 - 1800, IDR, True))[2])
        second = pts(packets(mux.mux(1800, P, False))[0])
        self.assertEqual(second - first, 3600)

    def test_backward_timestamp_keeps_pts_increasing(self):
        mux = m.TsMuxer("h264")
        first = pts(packets(mux.mux(90000, IDR, True))[2])
        second = pts(packets(mux.mux(0, P, False))[0])
        self.assertGreater(second, first)

    def test_psi_crc(self):
        self.assertEqual(m._crc32(b"123456789"), 0x0376E6E7)


if __name__ == "__main__":
    unittest.main()
