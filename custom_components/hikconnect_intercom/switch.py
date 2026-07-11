"""Switches: Do Not Disturb (account cloud) and DST (device time config).

Both cloud writes are eventually consistent (the state reflects a few seconds
later), so the switches hold an optimistic value until the poll confirms it —
otherwise the immediate re-poll would read the old state and bounce the toggle.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client = data["client"]
    coordinator = data["status_coordinator"]
    entities = []
    for dev in data["devices"]:
        if not dev.locks:  # intercoms only
            continue
        entities.append(HikDndSwitch(coordinator, client, hass, dev.serial))
        entities.append(HikDstSwitch(coordinator, client, hass, dev.serial))
    async_add_entities(entities)


class _OptimisticSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _key = ""  # key in the coordinator status dict

    def __init__(self, coordinator, client, hass, serial):
        super().__init__(coordinator)
        self.hass = hass
        self._client = client
        self._serial = serial
        self._pending: bool | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    def _status(self) -> dict:
        return (self.coordinator.data or {}).get(self._serial) or {}

    @property
    def is_on(self) -> bool | None:
        if self._pending is not None:
            return self._pending
        return self._status().get(self._key)

    def _write(self, on: bool) -> None:
        raise NotImplementedError

    async def _apply(self, on: bool) -> None:
        await self.hass.async_add_executor_job(self._write, on)
        self._pending = on
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._pending is not None and self._status().get(self._key) == self._pending:
            self._pending = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs) -> None:
        await self._apply(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._apply(False)


class HikDndSwitch(_OptimisticSwitch):
    _attr_name = "Do not disturb"
    _attr_icon = "mdi:bell-off"
    _key = "dnd"

    def __init__(self, coordinator, client, hass, serial):
        super().__init__(coordinator, client, hass, serial)
        self._attr_unique_id = f"{DOMAIN}_{serial}_dnd"

    def _write(self, on: bool) -> None:
        self._client.set_dnd(self._serial, on)


class HikDstSwitch(_OptimisticSwitch):
    _attr_name = "Daylight saving time"
    _attr_icon = "mdi:sun-clock"
    _attr_entity_category = EntityCategory.CONFIG
    _key = "dst"

    def __init__(self, coordinator, client, hass, serial):
        super().__init__(coordinator, client, hass, serial)
        self._attr_unique_id = f"{DOMAIN}_{serial}_dst"

    def _write(self, on: bool) -> None:
        st = self._status()
        self._client.set_time_config(
            self._serial,
            daylight_saving=1 if on else 0,
            time_zone=st.get("time_zone"),
            time_zone_no=st.get("time_zone_no"),
            time_format=st.get("time_format"),
        )
