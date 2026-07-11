"""Hik-Connect account client for the CPD7 local-stream path.

Hik-Connect shares the EZVIZ CAS backend, so once we log in with a Hik-Connect
account we can reuse the vendored `lib.cas.EzvizCAS` to fetch each device's CPD7
control key (``@Key``) and drive the local `Cpd7LanClient`.  This module provides
the Hik-Connect-specific pieces: the login endpoint, the device list (with LAN
IP), and a token shaped the way `EzvizCAS` expects.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from dataclasses import dataclass, field
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
_CALL_STATUS = {1: "idle", 2: "ringing", 3: "call in progress"}


def _csv_first_int(value: str | None) -> int | None:
    """First field of a "14,-1,-1,..." CSV string as int (negatives -> None)."""
    try:
        v = int(str(value).split(",")[0])
    except (ValueError, TypeError, AttributeError):
        return None
    return v if v >= 0 else None


@dataclass
class HikDevice:
    serial: str
    name: str
    local_ip: str | None
    device_type: str
    locks: dict[int, int] = field(default_factory=dict)  # channel -> lock count
    version: str = ""


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
            stats = j.get("statusInfos") or {}
            for d in j.get("deviceInfos", []):
                serial = d["deviceSerial"]
                conn = conns.get(serial) or {}
                ip = conn.get("localIp")
                if ip in ("0.0.0.0", "", None):
                    ip = None
                devices.append(
                    HikDevice(
                        serial,
                        d.get("name") or serial,
                        ip,
                        d.get("deviceType", ""),
                        self._parse_locks(stats.get(serial) or {}),
                        d.get("version", ""),
                    )
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

    # -- call / door controls (cloud) -------------------------------------
    def unlock(self, serial: str, channel: int, lock_index: int = 0) -> dict:
        """Open a door latch connected to an outdoor station channel."""
        return self._put(
            f"/v3/devconfig/v1/call/{serial}/{channel}/remote/unlock"
            f"?srcId=1&lockId={lock_index}&userType=0"
        )

    def answer_call(self, serial: str) -> dict:
        return self._put(f"/v3/devconfig/v1/call/{serial}/operation?cmdId=2")

    def cancel_call(self, serial: str) -> dict:
        return self._put(f"/v3/devconfig/v1/call/{serial}/operation?cmdId=3")

    def hangup_call(self, serial: str) -> dict:
        return self._put(f"/v3/devconfig/v1/call/{serial}/operation?cmdId=5")

    def get_call_status(self, serial: str) -> dict:
        """Return {'status': idle|ringing|call in progress|unknown, 'info': {...}}."""
        r = self._get(f"/v3/devconfig/v1/call/{serial}/status")
        if (r.get("meta") or {}).get("code") != 200:
            return {"status": "unknown", "info": {}}
        data = json.loads(r["data"])
        status = _CALL_STATUS.get(data.get("callStatus"), "unknown")
        return {"status": status, "info": data.get("callerInfo") or {}}

    # -- ISAPI over the cloud passthrough ---------------------------------
    def isapi(self, serial: str, method: str, path: str, body: str = "") -> dict:
        """Relay an ISAPI request to the device via the cloud (no admin pw)."""
        data = {
            "subSerial": serial,
            "cmdId": "19713",
            "transmissionData": f"{method} {path}\r\n{body}",
            "clientType": "55",
            "sessionId": self._session_id or "",
            "lang": "2",
        }
        return self._session.post(
            f"{self._base}/api/device/isapi", data=data, timeout=25
        ).json()

    def get_audio_volumes(self, serial: str) -> dict[str, int | None]:
        """Ringtone / two-way / microphone volume (0-10) via ISAPI."""

        def grab(xml: str, tag: str) -> int | None:
            m = re.search(rf"<{tag}>(\d+)</{tag}>", xml or "")
            return int(m.group(1)) if m else None

        ao = self.isapi(serial, "GET", "/ISAPI/System/Audio/AudioOut/channels/1")
        ai = self.isapi(serial, "GET", "/ISAPI/System/Audio/AudioIn/channels/1")
        return {
            "two_way": grab(ao.get("data"), "talkVolume"),
            "ringtone": grab(ao.get("data"), "volume"),
            "microphone": grab(ai.get("data"), "volume"),
        }

    def set_audio_volume(self, serial: str, kind: str, value: int) -> None:
        """Set one volume (kind: two_way|ringtone|microphone), 0-10."""
        value = max(0, min(10, int(value)))
        if kind == "microphone":
            body = (
                "<AudioIn><AudioInVolumelist><AudioInVlome><type>audioInput</type>"
                f"<volume>{value}</volume></AudioInVlome></AudioInVolumelist>"
                "<id>1</id></AudioIn>"
            )
            self.isapi(serial, "PUT", "/ISAPI/System/Audio/AudioIn/channels/1", body)
            return
        # AudioOut carries both talkVolume (two-way) and volume (ringtone);
        # ISAPI PUT replaces the resource, so preserve the other field.
        cur = self.get_audio_volumes(serial)
        talk = value if kind == "two_way" else (cur.get("two_way") or 5)
        vol = value if kind == "ringtone" else (cur.get("ringtone") or 5)
        body = (
            "<AudioOut><AudioOutVolumelist><AudioOutVlome>"
            f"<talkVolume>{talk}</talkVolume><type>audioOutput</type>"
            f"<volume>{vol}</volume>"
            "</AudioOutVlome></AudioOutVolumelist><id>1</id></AudioOut>"
        )
        self.isapi(serial, "PUT", "/ISAPI/System/Audio/AudioOut/channels/1", body)

    def set_time_now(self, serial: str, wall_now: datetime) -> None:
        """Set the device clock to `wall_now` (the caller's real local wall time).

        `wall_now` must be the correct local time (DST-aware, e.g. HA's
        ``dt_util.now()``); its wall-clock components are written verbatim and
        stamped with the device's own offset so the device *displays* the right
        time even if its configured timezone doesn't track DST.
        """
        cur = self.isapi(serial, "GET", "/ISAPI/System/time").get("data") or ""
        tzm = re.search(r"<timeZone>([^<]*)</timeZone>", cur)
        ltm = re.search(r"<localTime>([^<]+)</localTime>", cur)
        tz = tzm.group(1) if tzm else "CST0:00:00"
        lt = ltm.group(1) if ltm else ""
        off = lt[-6:] if len(lt) >= 6 and lt[-6] in "+-" else "+00:00"
        local = wall_now.strftime("%Y-%m-%dT%H:%M:%S") + off
        body = (
            '<Time version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<timeMode>manual</timeMode><localTime>{local}</localTime>"
            f"<timeZone>{tz}</timeZone></Time>"
        )
        self.isapi(serial, "PUT", "/ISAPI/System/time", body)

    # -- Do Not Disturb + time config -------------------------------------
    def get_dnd(self, serial: str) -> bool | None:
        """True if DND is on (account won't receive calls from the device)."""
        j = self._get(f"/v3/unifiedmsg/notify/nodisturb?devices={serial}&type=27")
        for entry in j.get("deviceData") or []:
            if serial in entry:
                return entry[serial] is False  # inverted: false == disturbed/off
        return None

    def set_dnd(self, serial: str, on: bool) -> None:
        self._session.post(
            f"{self._base}/v3/unifiedmsg/notify/nodisturb",
            data={"devices": serial, "type": "27",
                  "enableNoDisturb": "true" if on else "false"},
            timeout=25,
        )

    def set_time_config(
        self, serial: str, *, daylight_saving, time_zone, time_zone_no, time_format
    ) -> None:
        """Time zone / DST / date format (all via one endpoint)."""
        self._session.post(
            f"{self._base}/api/device/configTimeZone",
            data={
                "deviceSerialNo": serial,
                "daylightSaving": str(daylight_saving),
                "timeZone": time_zone or "UTC+00:00",
                "timeZoneNo": str(time_zone_no or 0),
                "timeFormat": str(time_format or 2),
                "time": "",
                "areaId": "105",
                "clientType": "55",
                "sessionId": self._session_id or "",
            },
            timeout=25,
        )

    # -- device status / metrics ------------------------------------------
    def get_device_status_map(self) -> dict[str, dict]:
        """Per-serial telemetry parsed from the cloud device list."""
        out: dict[str, dict] = {}
        limit, offset, has_next = 50, 0, True
        while has_next:
            path = (
                "/v3/userdevices/v1/devices/pagelist"
                f"?groupId=-1&limit={limit}&offset={offset}"
                "&filter=CONNECTION,STATUS,STATUS_EXT,WIFI,P2P"
            )
            j = self._get(path)
            conns = j.get("connectionInfos") or {}
            stats = j.get("statusInfos") or {}
            wifis = j.get("wifiInfos") or {}
            for d in j.get("deviceInfos", []):
                serial = d["deviceSerial"]
                st = stats.get(serial) or {}
                conn = conns.get(serial) or {}
                wifi = wifis.get(serial) or {}
                opt = st.get("optionals") or {}
                gs = st.get("globalStatus")
                disk_num = st.get("diskNum") or 0
                out[serial] = {
                    "online": (gs == 1) if gs is not None else (d.get("status") == 1),
                    "version": d.get("version"),
                    "model": d.get("deviceType"),
                    "local_ip": self._clean_ip(conn.get("localIp")),
                    "wan_ip": self._clean_ip(conn.get("netIp") or opt.get("wanIp")),
                    "wifi_signal": wifi.get("signal")
                    if isinstance(wifi.get("signal"), int)
                    else None,
                    "wireless": (wifi.get("netType") == "wireless")
                    if wifi.get("netType")
                    else None,
                    "upgrade_available": bool(st.get("upgradeAvailable")),
                    "disk_present": disk_num > 0,
                    "disk_capacity_gb": _csv_first_int(opt.get("diskCapacity"))
                    if disk_num
                    else None,
                    "disk_ok": (_csv_first_int(opt.get("diskHealth")) == 0)
                    if disk_num
                    else None,
                    "offline_timestamp": d.get("offlineTimestamp"),
                    "dst": opt.get("daylightSavingTime") == "1",
                    "time_zone": opt.get("timeZone"),
                    "time_zone_no": opt.get("tzCode"),
                    "time_format": opt.get("timeFormat"),
                }
            offset += limit
            has_next = (j.get("page") or {}).get("hasNext", False)
        return out

    @staticmethod
    def _clean_ip(value: str | None) -> str | None:
        if not value or value in ("0.0.0.0", ""):
            return None
        return value

    @staticmethod
    def _parse_locks(status_info: dict) -> dict[int, int]:
        """channel -> lock count, from statusInfos[serial].optionals.lockNum."""
        try:
            raw = json.loads((status_info.get("optionals") or {})["lockNum"])
        except (KeyError, TypeError, ValueError):
            return {}
        out: dict[int, int] = {}
        for k, v in raw.items():
            try:
                out[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    # -- http helpers -----------------------------------------------------
    def _post(self, path: str, data: dict) -> dict:
        return self._session.post(f"{self._base}{path}", data=data, timeout=25).json()

    def _get(self, path: str) -> dict:
        return self._session.get(f"{self._base}{path}", timeout=25).json()

    def _put(self, path: str) -> dict:
        return self._session.put(f"{self._base}{path}", timeout=25).json()
