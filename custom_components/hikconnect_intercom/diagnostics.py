"""Diagnostics: live cloud and ISAPI data behind each station's door/channel mapping."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_ACCOUNT, CONF_PASSWORD, DOMAIN

REDACTED = "**REDACTED**"
TO_REDACT = {
    CONF_ACCOUNT,
    CONF_PASSWORD,
    "netIp",
    "wanIp",
    "mac",
    "macAddress",
    "password",
    "userName",
    "verificationCode",
    "deviceCode",
    "deviceKey",
    "token",
}
ISAPI_PATHS = (
    "/ISAPI/VideoIntercom/WatchPoint?format=json",
    "/ISAPI/VideoIntercom/deviceCommunication?format=json",
)


def _attempt(fetch) -> Any:
    try:
        return fetch()
    except Exception as err:  # noqa: BLE001
        return {"error": f"{type(err).__name__}: {err}"}


def _isapi_json(client, serial: str, path: str) -> Any:
    data = client.isapi(serial, "GET", path).get("data") or ""
    try:
        return json.loads(data)
    except ValueError:
        return data


def collect(client, devices) -> dict[str, dict]:
    """Query the cloud and each station for the raw door/channel data."""
    pagelist = _attempt(lambda: client._get(
        "/v3/userdevices/v1/devices/pagelist"
        "?groupId=-1&limit=50&offset=0&filter=CONNECTION,STATUS,STATUS_EXT"
    ))
    out: dict[str, dict] = {}
    for dev in devices:
        serial = dev.serial
        out[serial] = {
            "device": asdict(dev),
            "status_info": (pagelist.get("statusInfos") or {}).get(serial),
            "connection_info": (pagelist.get("connectionInfos") or {}).get(serial),
            "cameras_info": _attempt(lambda: client._get(
                f"/v3/userdevices/v1/cameras/info?deviceSerial={serial}"
            )),
            "isapi": {
                path: _attempt(lambda: _isapi_json(client, serial, path))
                for path in ISAPI_PATHS
            },
        }
        if "error" in pagelist:
            out[serial]["status_info"] = pagelist
    return out


def anonymize(data: Any, serials: list[str]) -> Any:
    """Replace device serials, in keys and values, with stable placeholders."""
    names = {serial: f"STATION_{i}" for i, serial in enumerate(serials, 1)}

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {scrub(k): scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            if value.startswith(("http://", "https://")):
                return REDACTED
            for serial, name in names.items():
                value = value.replace(serial, name)
        return value

    return scrub(data)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    data = hass.data[DOMAIN][entry.entry_id]
    client, devices = data["client"], data["devices"]
    live = await hass.async_add_executor_job(collect, client, devices)
    registry = er.async_get(hass)
    entities = [
        {
            "entity_id": ent.entity_id,
            "unique_id": ent.unique_id,
            "original_name": ent.original_name,
            "disabled_by": ent.disabled_by,
        }
        for ent in er.async_entries_for_config_entry(registry, entry.entry_id)
    ]
    report = {
        "options": dict(entry.options),
        "cameras": [asdict(cam) for cam in data["cameras"]],
        "entities": entities,
        "call_status": data["coordinator"].data,
        "stations": live,
    }
    return anonymize(async_redact_data(report, TO_REDACT), [dev.serial for dev in devices])
