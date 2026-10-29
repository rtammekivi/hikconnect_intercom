"""Hik-Connect Intercom: native LAN video for Hik-Connect indoor stations (CPD7)."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

import requests
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CALL_POLL_INTERVAL,
    CONF_ACCOUNT,
    CONF_BASE_URL,
    CONF_CALL_POLL_INTERVAL,
    CONF_PASSWORD,
    DEFAULT_BASE_URL,
    DOMAIN,
    STATUS_POLL_INTERVAL,
    STATUS_POLL_TOLERANCE,
    call_signal,
    native_stream_mode,
)
from .hikconnect_api import HikConnectAuthError, HikConnectClient, HikConnectError
from .longlink import HikConnectLongLink
from .push import HikConnectPush

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [
    Platform.CAMERA,
    Platform.BUTTON,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
]


def _call_poll_interval(entry: ConfigEntry) -> timedelta:
    return timedelta(seconds=entry.options.get(CONF_CALL_POLL_INTERVAL, CALL_POLL_INTERVAL))


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
        "Hik-Connect Intercom: %d device(s), %d camera channel(s)",
        len(devices), len(cameras),
    )

    async def _poll_call_status() -> dict[str, dict]:
        def work():
            polled_at = time.monotonic()
            out: dict[str, dict] = {}
            for dev in devices:
                try:
                    out[dev.serial] = client.get_call_status(dev.serial)
                except HikConnectError as err:
                    raise UpdateFailed(f"call status poll failed for {dev.serial}: {err}") from err
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("call status poll failed for %s: %s", dev.serial, err)
                    out[dev.serial] = {"status": "unknown", "info": {}}
                out[dev.serial]["polled_at"] = polled_at
            return out

        result = await hass.async_add_executor_job(work)
        for serial, polled in result.items():
            current = (coordinator.data or {}).get(serial) or {}
            if max(current.get("local_at", 0.0), current.get("pushed_at", 0.0)) > polled["polled_at"]:
                result[serial] = current
        return result

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_call_status",
        update_method=_poll_call_status,
        update_interval=_call_poll_interval(entry),
    )
    await coordinator.async_config_entry_first_refresh()

    native_stream = native_stream_mode(entry.options)

    async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        coordinator.update_interval = _call_poll_interval(entry)
        if native_stream_mode(entry.options) != native_stream:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_options_updated))

    status_misses = 0

    def _keep_last_status(reason: str) -> dict[str, dict]:
        nonlocal status_misses
        status_misses += 1
        if status_coordinator.data is None or status_misses > STATUS_POLL_TOLERANCE:
            raise UpdateFailed(reason)
        _LOGGER.debug(
            "%s; keeping last status (%d/%d)", reason, status_misses, STATUS_POLL_TOLERANCE
        )
        return status_coordinator.data

    def _status_work() -> dict[str, dict]:
        nonlocal status_misses
        try:
            status = client.get_device_status_map()
        except (HikConnectError, requests.RequestException) as err:
            return _keep_last_status(f"status poll failed: {err}")
        missing = [d.serial for d in devices if d.serial not in status]
        if missing:
            # The cloud answers an expired session with a well-formed 401 and an
            # empty device list, so silence here would read as "no data" forever.
            return _keep_last_status(f"cloud returned no data for {', '.join(missing)}")
        status_misses = 0
        for dev in devices:
            if not dev.locks:
                continue
            previous = (status_coordinator.data or {}).get(dev.serial) or {}
            for key, fetch, what in (
                ("volumes", client.get_audio_volumes, "volume"),
                ("dnd", client.get_dnd, "dnd"),
                ("last_ring", client.get_last_ring, "ring history"),
                ("work_status", client.get_work_status, "work status"),
                ("operation_time", client.get_operation_time, "operation time"),
            ):
                try:
                    status[dev.serial][key] = fetch(dev.serial)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("%s fetch failed for %s: %s", what, dev.serial, err)
                    if key in previous:
                        status[dev.serial][key] = previous[key]
            if status[dev.serial].get("dnd") is None and previous.get("dnd") is not None:
                status[dev.serial]["dnd"] = previous["dnd"]
        return status

    async def _poll_status() -> dict[str, dict]:
        return await hass.async_add_executor_job(_status_work)

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
    async def _on_call_push(*_) -> None:
        await coordinator.async_request_refresh()

    unsubs = [
        async_dispatcher_connect(hass, call_signal(dev.serial), _on_call_push)
        for dev in devices
    ]

    push = HikConnectPush(
        hass, client._base, entry.data[CONF_ACCOUNT], entry.data[CONF_PASSWORD]
    )
    try:
        await push.async_start()
    except Exception as err:  # noqa: BLE001 - never fail setup on push
        _LOGGER.warning("Hik-Connect push listener failed to start: %s", err)
        push = None
    else:
        async def _stop_push(_event) -> None:
            await push.async_stop()

        entry.async_on_unload(
            client.add_login_listener(lambda: hass.add_job(push.async_refresh_session))
        )
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_push))

    longlink = None
    if push is None or push.used_fallback_host:
        longlink = await _async_start_longlink(hass, entry, client, coordinator)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "devices": devices,
        "cameras": cameras,
        "coordinator": coordinator,
        "status_coordinator": status_coordinator,
        "push": push,
        "longlink": longlink,
        "unsubs": unsubs,
        "quality": {},  # (serial_chN -> MAIN|SUB), shared: select writes, camera reads
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_start_longlink(
    hass: HomeAssistant, entry: ConfigEntry, client: HikConnectClient, coordinator
) -> HikConnectLongLink | None:
    @callback
    def _rang(fields: list[str], caller: dict) -> None:
        data = dict(coordinator.data or {})
        serials = [serial for serial in data if serial in fields]
        if not serials:
            hass.async_create_task(coordinator.async_request_refresh())
            return
        now = time.monotonic()
        for serial in serials:
            client.remember_caller_channel(serial, caller)
            data[serial] = {"status": "ringing", "info": caller, "polled_at": now, "pushed_at": now}
        coordinator.async_set_updated_data(data)

    @callback
    def _hung_up(fields: list[str]) -> None:
        data = dict(coordinator.data or {})
        serials = [serial for serial in data if serial in fields]
        for serial in serials:
            data[serial] = {**data[serial], "status": "idle", "local_at": time.monotonic()}
        if serials:
            coordinator.async_set_updated_data(data)

    longlink = HikConnectLongLink(
        hass,
        Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.longlink", private=True),
        entry.data[CONF_ACCOUNT],
        entry.data[CONF_PASSWORD],
        client._base,
        lambda fields, caller: hass.add_job(_rang, fields, caller),
        lambda fields: hass.add_job(_hung_up, fields),
    )
    try:
        await longlink.async_start()
    except Exception as err:  # noqa: BLE001 - never fail setup on push
        _LOGGER.warning("Hik-Connect long-link push failed to start: %s", err)
        return None

    async def _stop_longlink(_event) -> None:
        await longlink.async_stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_longlink))
    return longlink


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
            longlink = data.get("longlink")
            if longlink is not None:
                await longlink.async_stop()
    return unload_ok
