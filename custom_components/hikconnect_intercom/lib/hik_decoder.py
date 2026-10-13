"""CPD7 nested RTP decoder for H.264 intercoms and encrypted H.264/H.265 NVRs.

Recorder encryption: AES-128-ECB with the verification code zero padded to 16
bytes; only the first 4096 complete bytes of a NAL body are encrypted. Encryption
is signalled by RTP extension 0x4000. Scheme independently documented at
https://github.com/dsameendra/sentinel-eye/blob/main/tools/NOTES.md .
"""

from __future__ import annotations

from Crypto.Cipher import AES

_SC = b"\x00\x00\x00\x01"


class HikStreamDecoder:
    """Incrementally depacketize and decrypt CPD7 H.264/H.265 video.

    Keys remain in memory. An encrypted stream without a key is detected but
    emits no video, allowing the caller to retrieve its verification code and
    replay the initial packets.
    """

    def __init__(self, interleave: int = 1, video_key: str | None = None) -> None:
        self._marker = bytes([0x24, interleave & 255])
        self._buf = bytearray()
        self._out = bytearray()
        self.codec: str | None = None
        self.requires_key = False
        self._fragment: bytearray | None = None
        self._fragment_enc = False
        self._last_seq: int | None = None
        self._aes = None
        if video_key:
            key = video_key.encode("utf-8")
            if len(key) > 16:
                raise ValueError("Unsupported video encryption key length")
            self._aes = AES.new(key.ljust(16, b"\0"), AES.MODE_ECB)

    @property
    def keys_derived(self) -> bool:
        return not self.requires_key or self._aes is not None

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)
        while True:
            i = self._buf.find(self._marker)
            if i < 0:
                self._buf[:] = self._buf[-1:]
                return
            if i:
                del self._buf[:i]
            if len(self._buf) < 4:
                return
            n = int.from_bytes(self._buf[2:4], "big")
            if len(self._buf) < n + 4:
                return
            packet = bytes(self._buf[4 : n + 4])
            del self._buf[: n + 4]
            self._handle_packet(packet)

    def take(self) -> bytes:
        out = bytes(self._out)
        self._out.clear()
        return out

    def _emit(self, nal: bytes, encrypted: bool) -> None:
        if encrypted:
            self.requires_key = True
            if self._aes is None:
                return
            # H.265 preserves its two-byte NAL header; H.264's clear
            # header is duplicated inside the encrypted payload (even when
            # a short PPS contains no complete AES block).
            header_size = 2 if self.codec == "hevc" else 1
            n = min((len(nal) - header_size) // 16 * 16, 4096)
            body = (
                self._aes.decrypt(nal[header_size : header_size + n])
                + nal[header_size + n :]
            )
            nal = nal[:header_size] + body if header_size == 2 else body
        self._out.extend(_SC + nal)

    def _handle_packet(self, packet: bytes) -> None:
        if len(packet) < 26 or packet[12] != 13:
            return
        # The outer RTP header is followed by 0x0d and a nested RTP packet.
        rtp = packet[13:]
        if rtp[0] >> 6 != 2:
            return
        offset = 12 + 4 * (rtp[0] & 15)
        encrypted = False
        if rtp[0] & 16:
            if len(rtp) < offset + 4:
                return
            profile = rtp[offset : offset + 2]
            size = int.from_bytes(rtp[offset + 2 : offset + 4], "big") * 4
            offset += 4 + size
            # Other profiles are recorder metadata, not compressed video.
            if profile != b"\x40\0" or offset > len(rtp):
                return
            encrypted = True
            self.requires_key = True
        end = len(rtp)
        if rtp[0] & 32:
            padding = rtp[-1]
            if padding == 0 or padding > end - offset:
                return
            end -= padding
        payload = rtp[offset:end]
        if not payload:
            return
        if self.codec is None:
            if (
                len(payload) > 1
                and payload[0] in (0x40, 0x42, 0x44)
                and payload[1] == 1
            ):
                self.codec = "hevc"
            elif payload[0] & 31 in (7, 8):
                self.codec = "h264"
            else:
                return
        seq = int.from_bytes(rtp[2:4], "big")
        if self._last_seq is not None and seq != (self._last_seq + 1) & 65535:
            self._fragment = None
        self._last_seq = seq
        hevc = self.codec == "hevc"
        nal_type = (payload[0] >> 1) & 63 if hevc else payload[0] & 31
        fragment_type = 49 if hevc else 28
        aggregation_type = 48 if hevc else 24
        if nal_type == fragment_type:
            header_size = 2 if hevc else 1
            if len(payload) <= header_size:
                return
            flags = payload[header_size]
            if flags & 128:
                header = (
                    bytes([(payload[0] & 129) | ((flags & 63) << 1), payload[1]])
                    if hevc
                    else bytes([(payload[0] & 224) | (flags & 31)])
                )
                self._fragment = bytearray(header + payload[header_size + 1 :])
                self._fragment_enc = encrypted
            elif self._fragment is not None:
                self._fragment.extend(payload[header_size + 1 :])
            if self._fragment is not None and len(self._fragment) > 8 * 1024 * 1024:
                self._fragment = None
            if flags & 64 and self._fragment is not None:
                self._emit(bytes(self._fragment), self._fragment_enc)
                self._fragment = None
        elif nal_type == aggregation_type:
            i = 2 if hevc else 1
            while i + 2 <= len(payload):
                n = int.from_bytes(payload[i : i + 2], "big")
                i += 2
                if not n or i + n > len(payload):
                    break
                self._emit(payload[i : i + n], encrypted)
                i += n
        elif (hevc and nal_type < 48) or (not hevc and 0 < nal_type < 24):
            self._emit(payload, encrypted)
