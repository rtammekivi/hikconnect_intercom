"""Doorbell event entity fired when a station starts ringing."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        HikDoorbellEvent(data["coordinator"], dev.serial)
        for dev in data["devices"]
        if dev.locks
    )


class HikDoorbellEvent(CoordinatorEntity, EventEntity):
    _attr_has_entity_name = True
    _attr_name = "Doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]

    def __init__(self, coordinator, serial):
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{DOMAIN}_{serial}_doorbell"
        self._last_status = self._status()

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    def _status(self) -> str | None:
        return ((self.coordinator.data or {}).get(self._serial) or {}).get("status")

    @callback
    def _handle_coordinator_update(self) -> None:
        status = self._status()
        if status == "ringing" and self._last_status != "ringing":
            self._trigger_event("ring")
        self._last_status = status
        super()._handle_coordinator_update()
