"""Realtime call events over the Hik-Connect channel-99 long link."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import requests
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .lib.longlink._longlink_session import Channel99Session
from .lib.longlink._longlink_worker import PushWorker

_LOGGER = logging.getLogger(__name__)

_APP_TYPE = "HikConnect2022"
_HEADERS = {
    "clientType": "55",
    "lang": "en-US",
    "appId": _APP_TYPE,
    "clientVersion": "7.0.200.1010",
    "osVersion": "16",
    "customno": "1000002",
    "netType": "WIFI",
    "clientNo": "",
}
_REGISTRATION = {
    "pushRegisterJson": '[{"channel":99}]',
    "pushExtJson": '{"language":"","protoVer":"2"}',
}
_PUSH_RING = 2
_PUSH_HANGUP = 3
_SESSION_MAX_AGE = 6 * 3600
_SAVE_TIMEOUT = 30


class LongLinkError(Exception):
    """The long-link login or registration failed."""


def long_link_feature_code(account: str) -> str:
    return hashlib.md5(f"hikconnect_intercom:{account}".encode("utf-8")).hexdigest()


class HikConnectLongLink:
    """Channel-99 push listener; any message triggers an authoritative poll."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: Store,
        account: str,
        password: str,
        base_url: str,
        on_ring: Callable[[], None],
        on_hangup: Callable[[list[str]], None],
    ) -> None:
        self._hass = hass
        self._store = store
        self._account = account
        self._password = password
        self._base = base_url.rstrip("/")
        self._on_ring = on_ring
        self._on_hangup = on_hangup
        self._feature_code = long_link_feature_code(account)
        self._http = requests.Session()
        self._http.headers.update({**_HEADERS, "featureCode": self._feature_code})
        self._session_id: str | None = None
        self._user_id: str | None = None
        self._logged_in_at = float("-inf")
        self._endpoint: tuple[str, int] | None = None
        self._worker: PushWorker | None = None

    async def async_start(self) -> None:
        state = await self._store.async_load() or {}
        await self._hass.async_add_executor_job(self._login)
        serial = f"MOBILE:{_APP_TYPE}:{self._user_id}:{self._feature_code}".encode("ascii")
        self._worker = PushWorker(
            lambda: Channel99Session(
                self._endpoint,
                serial,
                lambda: self._session_id,
                state,
                self._save,
                self._on_message,
                prepare=self._prepare,
            )
        )
        self._worker.start()
        _LOGGER.info("Hik-Connect long-link push started (%s:%s)", *self._endpoint)

    async def async_stop(self) -> None:
        if self._worker is not None:
            worker, self._worker = self._worker, None
            await self._hass.async_add_executor_job(worker.stop)

    def _login(self) -> None:
        data = {
            "account": self._account,
            "password": hashlib.md5(self._password.encode("utf-8")).hexdigest(),
            "featureCode": self._feature_code,
            "msgType": "0",
            "bizType": "",
            "cuName": base64.b64encode(b"Hassio").decode(),
            **_REGISTRATION,
        }
        j = self._request("POST", "/v3/users/login/v5", data=data)
        if (j.get("meta") or {}).get("code") == 1100:
            self._base = f"https://{j['loginArea']['apiDomain']}"
            j = self._request("POST", "/v3/users/login/v5", data=data)
        if "loginSession" not in j:
            raise LongLinkError(f"long-link login failed (meta {j.get('meta')})")
        self._session_id = j["loginSession"]["sessionId"]
        self._user_id = str(j["loginUser"]["userId"])
        self._http.headers["sessionId"] = self._session_id
        self._logged_in_at = time.monotonic()
        info = self._request("GET", "/v3/configurations/system/info")["systemConfigInfo"]
        self._endpoint = (info["pushDasDomain"], int(info["pushDasPort"]))
        reg = self._request("PUT", "/v3/push/token", data={"pushRegisterJson": _REGISTRATION["pushRegisterJson"]})
        if (reg.get("meta") or {}).get("code") != 200:
            raise LongLinkError(f"channel-99 registration rejected (meta {reg.get('meta')})")

    def _prepare(self) -> None:
        if time.monotonic() - self._logged_in_at > _SESSION_MAX_AGE:
            self._login()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        r = self._http.request(method, f"{self._base}{path}", timeout=25, **kwargs)
        try:
            return r.json()
        except ValueError as err:
            raise LongLinkError(f"{method} {path}: non-JSON reply ({r.status_code})") from err

    def _save(self, snapshot: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(
            self._store.async_save(snapshot), self._hass.loop
        ).result(_SAVE_TIMEOUT)

    def _on_message(self, payload: bytes) -> None:
        _LOGGER.debug("Hik-Connect long-link message: %r", payload[:500])
        try:
            message = json.loads(payload)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        if message.get("pushType") == _PUSH_RING:
            self._on_ring()
        elif message.get("pushType") == _PUSH_HANGUP:
            self._on_hangup(str(message.get("ext") or "").split(","))
