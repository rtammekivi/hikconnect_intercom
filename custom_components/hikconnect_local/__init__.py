"""Hik-Connect Local: native LAN video for Hik-Connect indoor stations (CPD7)."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CALL_POLL_INTERVAL,
    CONF_ACCOUNT,
    CONF_BASE_URL,
    CONF_PASSWORD,
    DEFAULT_BASE_URL,
    DOMAIN,
    STATUS_POLL_INTERVAL,
    call_signal,
)
from .hikconnect_api import HikConnectAuthError, HikConnectClient
from .push import HikConnectPush

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [
    Platform.CAMERA,
    Platform.BUTTON,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = HikConnectClient(
        entry.data[CONF_ACCOUNT],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
    )

    def _login_and_enumerate():
        client.login()
        devices = client.get_devices()
        cams = []
        for dev in devices:
            if not dev.local_ip:
                continue
            cams.extend(client.get_cameras(dev))
        return devices, cams

    try:
        devices, cameras = await hass.async_add_executor_job(_login_and_enumerate)
    except HikConnectAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"Hik-Connect setup failed: {err}") from err

    _LOGGER.info(
        "Hik-Connect Local: %d device(s), %d camera channel(s)",
        len(devices), len(cameras),
    )

    async def _poll_call_status() -> dict[str, dict]:
        def work():
            out: dict[str, dict] = {}
            for dev in devices:
                try:
                    out[dev.serial] = client.get_call_status(dev.serial)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("call status poll failed for %s: %s", dev.serial, err)
                    out[dev.serial] = {"status": "unknown", "info": {}}
            return out

        return await hass.async_add_executor_job(work)

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_call_status",
        update_method=_poll_call_status,
        update_interval=timedelta(seconds=CALL_POLL_INTERVAL),
    )
    await coordinator.async_config_entry_first_refresh()

    async def _poll_status() -> dict[str, dict]:
        return await hass.async_add_executor_job(client.get_device_status_map)

    status_coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_status",
        update_method=_poll_status,
        update_interval=timedelta(seconds=STATUS_POLL_INTERVAL),
    )
    await status_coordinator.async_config_entry_first_refresh()

    registry = dr.async_get(hass)
    for device in devices:
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device.serial)},
            manufacturer="Hikvision",
            name=device.name,
            model=device.device_type,
            sw_version=device.version or None,
        )

    # Realtime push: a call event just triggers an immediate authoritative poll.
    unsubs = []
    for dev in devices:
        unsubs.append(
            async_dispatcher_connect(
                hass,
                call_signal(dev.serial),
                lambda *_: hass.async_create_task(coordinator.async_request_refresh()),
            )
        )

    push = HikConnectPush(
        hass, client._base, entry.data[CONF_ACCOUNT], entry.data[CONF_PASSWORD]
    )
    try:
        await push.async_start()
    except Exception as err:  # noqa: BLE001 - never fail setup on push
        _LOGGER.warning("Hik-Connect push listener failed to start: %s", err)
        push = None

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "devices": devices,
        "cameras": cameras,
        "coordinator": coordinator,
        "status_coordinator": status_coordinator,
        "push": push,
        "unsubs": unsubs,
        "quality": {},  # (serial_chN -> MAIN|SUB), shared: select writes, camera reads
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data:
            for unsub in data.get("unsubs", []):
                unsub()
            push = data.get("push")
            if push is not None:
                await push.async_stop()
    return unload_ok
