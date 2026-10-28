"""Minimal MPEG-TS muxer for one H.264/H.265 elementary stream.

RTP video timestamps already run on the 90 kHz MPEG clock, so the station's own
frame timing becomes the PTS directly and no transcoder or remuxer is needed.
"""

from __future__ import annotations

_PMT_PID = 0x1000
_VIDEO_PID = 0x100
_PCR_DELAY = 63000
_PTS_START = 126000
_MAX_GAP = 10 * 90000
_AUD = {
    "h264": b"\x00\x00\x00\x01\x09\xf0",
    "hevc": b"\x00\x00\x00\x01\x46\x01\x50",
}
_STREAM_TYPE = {"h264": 0x1B, "hevc": 0x24}


def _crc32(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = (crc << 1) ^ 0x04C11DB7 if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def _section(body: bytes) -> bytes:
    return body + _crc32(body).to_bytes(4, "big")


class TsMuxer:
    """Turns timestamped access units into a continuous transport stream."""

    def __init__(self, codec: str) -> None:
        self._aud = _AUD[codec]
        self._cc: dict[int, int] = {}
        self._last_ts: int | None = None
        self._pts = _PTS_START
        self._pat = _section(
            bytes([0x00, 0xB0, 13, 0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01])
            + bytes([0xE0 | _PMT_PID >> 8, _PMT_PID & 0xFF])
        )
        self._pmt = _section(
            bytes([0x02, 0xB0, 18, 0x00, 0x01, 0xC1, 0x00, 0x00])
            + bytes([0xE0 | _VIDEO_PID >> 8, _VIDEO_PID & 0xFF, 0xF0, 0x00])
            + bytes([_STREAM_TYPE[codec], 0xE0 | _VIDEO_PID >> 8, _VIDEO_PID & 0xFF, 0xF0, 0x00])
        )

    def mux(self, ts: int, unit: bytes, key: bool) -> bytes:
        """One access unit; ``ts`` is its 32-bit 90 kHz RTP timestamp."""
        if self._last_ts is not None:
            delta = ((ts - self._last_ts + 2**31) & 0xFFFFFFFF) - 2**31
            self._pts += delta if 0 < delta <= _MAX_GAP else 1
        self._last_ts = ts
        pts = self._pts & (2**33 - 1)
        out = bytearray()
        if key or len(self._cc) == 0:
            out += self._psi(0, self._pat) + self._psi(_PMT_PID, self._pmt)
        pes = (
            b"\x00\x00\x01\xe0\x00\x00\x84\x80\x05"
            + bytes([
                0x21 | (pts >> 29) & 0x0E,
                (pts >> 22) & 0xFF,
                0x01 | (pts >> 14) & 0xFE,
                (pts >> 7) & 0xFF,
                0x01 | (pts << 1) & 0xFE,
            ])
            + self._aud
            + unit
        )
        out += self._packets(pes, (self._pts - _PCR_DELAY) & (2**33 - 1), key)
        return bytes(out)

    def _next_cc(self, pid: int) -> int:
        cc = self._cc.get(pid, -1) + 1 & 0x0F
        self._cc[pid] = cc
        return cc

    def _psi(self, pid: int, section: bytes) -> bytes:
        payload = b"\x00" + section
        header = bytes([0x47, 0x40 | pid >> 8, pid & 0xFF, 0x10 | self._next_cc(pid)])
        return header + payload + b"\xff" * (184 - len(payload))

    def _packets(self, pes: bytes, pcr: int, key: bool) -> bytes:
        out = bytearray()
        pos = 0
        first = True
        while first or pos < len(pes):
            fields = b""
            if first:
                fields = bytes([
                    (0x40 if key else 0) | 0x10,
                    (pcr >> 25) & 0xFF,
                    (pcr >> 17) & 0xFF,
                    (pcr >> 9) & 0xFF,
                    (pcr >> 1) & 0xFF,
                    (pcr & 1) << 7 | 0x7E,
                    0x00,
                ])
            room = 184 - (1 + len(fields) if fields else 0)
            n = min(room, len(pes) - pos)
            if fields or n < 184:
                size = 184 - n
                if size == 1:
                    adaptation = b"\x00"
                else:
                    body = fields or b"\x00"
                    adaptation = bytes([size - 1]) + body + b"\xff" * (size - 1 - len(body))
                control = 0x30
            else:
                adaptation = b""
                control = 0x10
            out += bytes([
                0x47,
                (0x40 if first else 0) | _VIDEO_PID >> 8,
                _VIDEO_PID & 0xFF,
                control | self._next_cc(_VIDEO_PID),
            ])
            out += adaptation + pes[pos:pos + n]
            pos += n
            first = False
        return bytes(out)
