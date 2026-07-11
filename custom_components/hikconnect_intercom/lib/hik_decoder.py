"""Incremental decoder for Hik-Connect indoor stations (unencrypted CPD7 media).

Unlike the EZVIZ HP7 (ChaCha20 ``$\\x02`` packets), Hik-Connect indoor stations
such as the DS-KH6320-WTE1 send the local 9020 media UNENCRYPTED.  Each media
unit on the wire is::

    $ \\x01 <len:2 BE>            # RTSP-interleaved framing
      <12-byte RTP header>       # 80 60 <seq> <ts> <ssrc=session>
      <13-byte Hikvision header> # payload[0] == 0x0d, per-packet counter
      <RFC 6184 H.264 payload>   # 0x67 SPS / 0x68 PPS / single NAL / 0x7c FU-A

Decode = strip framing -> drop 12+13 header -> standard RFC 6184 depacketize ->
Annex-B H.264.  Verified against a DS-KH6320-WTE1: H.264 Baseline 1280x720 25fps.
"""

from __future__ import annotations

import logging
import struct

_LOGGER = logging.getLogger(__name__)

_SC = b"\x00\x00\x00\x01"
_RTP_HEADER = 12
_HIK_HEADER = 13


class HikStreamDecoder:
    """Incremental Hik-Connect (unencrypted) CPD7 stream decoder.

    Usage mirrors ``cpd7.decoder.StreamDecoder`` so it drops into the same loop::

        d = HikStreamDecoder()
        d.feed(raw_bytes_from_play_socket)
        annexb = d.take()
    """

    def __init__(self, interleave: int = 1) -> None:
        # RTSP-interleaved channel byte after '$'; equals the CPD7 channel
        # (ch1 -> $\x01, ch2 -> $\x02, ...).  Syncing on the wrong channel
        # locks onto stray 0x24 bytes in the payload and emits garbage.
        self._interleave = interleave & 0xFF
        self._marker = bytes([0x24, self._interleave])
        self._buf = bytearray()
        self._out = bytearray()
        self._synced = False

    @property
    def keys_derived(self) -> bool:  # parity with StreamDecoder API
        return True

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self._buf.extend(data)
        while self._consume_one_chunk():
            pass

    def take(self) -> bytes:
        out = bytes(self._out)
        self._out.clear()
        return out

    # -- internals --------------------------------------------------------
    def _consume_one_chunk(self) -> bool:
        # Resync to the first RTSP-interleaved marker ($ + this channel).
        if not self._synced:
            i = self._buf.find(self._marker)
            if i < 0:
                # keep only the last byte in case '$' straddles a read boundary
                if len(self._buf) > 1:
                    del self._buf[:-1]
                return False
            del self._buf[:i]
            self._synced = True

        if len(self._buf) < 4:
            return False
        if self._buf[0] != 0x24 or self._buf[1] != self._interleave:
            self._synced = False
            return True
        plen = struct.unpack(">H", bytes(self._buf[2:4]))[0]
        total = 4 + plen
        if len(self._buf) < total:
            return False
        packet = bytes(self._buf[4:total])
        del self._buf[:total]
        self._handle_packet(packet)
        return True

    def _handle_packet(self, packet: bytes) -> None:
        if len(packet) < _RTP_HEADER + _HIK_HEADER + 1:
            return
        pay = packet[_RTP_HEADER + _HIK_HEADER:]  # RFC 6184 payload
        t = pay[0] & 0x1F
        if 1 <= t <= 23:
            self._out += _SC + pay
        elif t == 28:  # FU-A
            fu_ind, fu_hdr = pay[0], pay[1]
            if fu_hdr & 0x80:  # start fragment
                self._out += _SC + bytes([(fu_ind & 0xE0) | (fu_hdr & 0x1F)]) + pay[2:]
            else:
                self._out += pay[2:]
        elif t == 24:  # STAP-A
            i = 1
            while i + 2 <= len(pay):
                sz = struct.unpack(">H", pay[i:i + 2])[0]
                i += 2
                if i + sz > len(pay):
                    break
                self._out += _SC + pay[i:i + sz]
                i += sz
