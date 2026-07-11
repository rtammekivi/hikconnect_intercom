"""Minimal constants + exceptions vendored for the self-contained CAS client."""

from __future__ import annotations

import uuid
from hashlib import md5


def _generate_unique_code() -> str:
    mac_int = uuid.getnode()
    mac_str = ":".join(f"{(mac_int >> i) & 0xFF:02x}" for i in range(40, -1, -8))
    return md5(mac_str.encode("utf-8")).hexdigest()


FEATURE_CODE = _generate_unique_code()
XOR_KEY = b"\x0c\x0eJ^X\x15@Rr"


class PyEzvizError(Exception):
    """CAS/pylocalapi error."""


class InvalidHost(PyEzvizError):
    """Invalid IP or hostname."""
