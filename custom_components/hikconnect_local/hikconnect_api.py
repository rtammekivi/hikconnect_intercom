"""Hik-Connect account client for the CPD7 local-stream path.

Hik-Connect shares the EZVIZ CAS backend, so once we log in with a Hik-Connect
account we can reuse the vendored `lib.cas.EzvizCAS` to fetch each device's CPD7
control key (``@Key``) and drive the local `Cpd7LanClient`.  This module provides
the Hik-Connect-specific pieces: the login endpoint, the device list (with LAN
IP), and a token shaped the way `EzvizCAS` expects.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import requests

try:
    from .lib.cas import EzvizCAS
except ImportError:  # standalone / CLI use
    from lib.cas import EzvizCAS

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.hik-connect.com"
FEATURE_CODE = "deadbeefdeadbeef"
_HEADERS = {"clientType": "55", "lang": "en-US", "featureCode": FEATURE_CODE}


@dataclass
class HikDevice:
    serial: str
    name: str
    local_ip: str | None
    device_type: str


@dataclass
class HikCamera:
    """A real (configured) channel on a device — one HA camera entity each."""

    serial: str
    channel: int
    name: str
    local_ip: str


class HikConnectAuthError(Exception):
    """Login failed."""


class HikConnectClient:
    """Minimal Hik-Connect cloud client feeding the CPD7 local stream."""

    def __init__(self, account: str, password: str, base_url: str = DEFAULT_BASE) -> None:
        self._account = account
        self._password = password
        self._base = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._session_id: str | None = None
        self._username: str | None = None
        self._sysconf: list[str] = []

    # -- auth -------------------------------------------------------------
    def login(self) -> None:
        data = {
            "account": self._account,
            "password": hashlib.md5(self._password.encode("utf-8")).hexdigest(),
        }
        r = self._post("/v3/users/login/v2", data)
        if r["meta"]["code"] == 1100:  # region redirect
            self._base = "https://" + r["loginArea"]["apiDomain"]
            r = self._post("/v3/users/login/v2", data)
        code = r["meta"]["code"]
        if code in (1013, 1014):
            raise HikConnectAuthError("bad username/password")
        if code == 1015:
            raise HikConnectAuthError("CAPTCHA required; log in via the app once, then retry")
        if "loginSession" not in r:
            raise HikConnectAuthError(f"login failed (meta code {code})")
        self._session_id = r["loginSession"]["sessionId"]
        self._username = r["loginUser"]["username"]
        self._session.headers["sessionId"] = self._session_id
        sci = self._get("/v3/configurations/system/info")["systemConfigInfo"]
        self._sysconf = str(sci.get("sysConf", "")).split("|")
        _LOGGER.debug("Hik-Connect login ok user=%s base=%s", self._username, self._base)

    # -- devices ----------------------------------------------------------
    def get_devices(self) -> list[HikDevice]:
        devices: list[HikDevice] = []
        limit, offset, has_next = 50, 0, True
        while has_next:
            path = (
                "/v3/userdevices/v1/devices/pagelist"
                f"?groupId=-1&limit={limit}&offset={offset}"
                "&filter=CONNECTION,STATUS,STATUS_EXT,WIFI,P2P"
            )
            j = self._get(path)
            conns = j.get("connectionInfos") or {}
            for d in j.get("deviceInfos", []):
                serial = d["deviceSerial"]
                conn = conns.get(serial) or {}
                ip = conn.get("localIp")
                if ip in ("0.0.0.0", "", None):
                    ip = None
                devices.append(
                    HikDevice(serial, d.get("name") or serial, ip, d.get("deviceType", ""))
                )
            offset += limit
            has_next = (j.get("page") or {}).get("hasNext", False)
        return devices

    def get_cameras(self, device: HikDevice) -> list[HikCamera]:
        """Return the real (configured) channels for a device.

        The cloud reports many phantom channels (name defaults to the serial,
        signalStatus 0); a real door-station channel is either named or has
        signal.  Falls back to channel 1 if the list can't be read.
        """
        cams: list[HikCamera] = []
        try:
            j = self._get(
                f"/v3/userdevices/v1/cameras/info?deviceSerial={device.serial}"
            )
            for c in j.get("cameraInfos", []):
                ch = c.get("channelNo")
                if ch is None:
                    continue
                name = c.get("cameraName") or device.serial
                sig = (c.get("deviceChannelInfo") or {}).get("signalStatus", 0)
                if name != device.serial or sig == 1:
                    cams.append(HikCamera(device.serial, int(ch), name, device.local_ip))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("cameras/info failed for %s: %s", device.serial, err)
        if not cams:
            cams.append(HikCamera(device.serial, 1, device.name, device.local_ip))
        cams.sort(key=lambda c: c.channel)
        return cams

    # -- CPD7 control key -------------------------------------------------
    @property
    def cas_token(self) -> dict[str, Any]:
        if not self._session_id:
            raise RuntimeError("call login() first")
        return {
            "session_id": self._session_id,
            "rf_session_id": None,
            "username": self._username,
            "api_url": self._base.replace("https://", ""),
            "feature_code": FEATURE_CODE,
            "service_urls": {"sysConf": self._sysconf},
        }

    def get_control_key(self, serial: str) -> tuple[str, str]:
        """Return (aes_key, operation_code) for the device from the shared CAS."""
        doc = EzvizCAS(self.cas_token).cas_get_encryption(serial)
        session = doc["Response"]["Session"]
        return session["@Key"], session["@OperationCode"]

    # -- http helpers -----------------------------------------------------
    def _post(self, path: str, data: dict) -> dict:
        return self._session.post(f"{self._base}{path}", data=data, timeout=25).json()

    def _get(self, path: str) -> dict:
        return self._session.get(f"{self._base}{path}", timeout=25).json()
