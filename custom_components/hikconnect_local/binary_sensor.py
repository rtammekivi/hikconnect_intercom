"""Device telemetry binary sensors (connectivity, update, disk health)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["status_coordinator"]
    entities = []
    for dev in data["devices"]:
        entities.append(
            HikBinary(
                coordinator, dev.serial, "connectivity", "Connectivity",
                lambda s: s.get("online"),
                BinarySensorDeviceClass.CONNECTIVITY, None,
            )
        )
        entities.append(
            HikBinary(
                coordinator, dev.serial, "update", "Firmware update",
                lambda s: s.get("upgrade_available"),
                BinarySensorDeviceClass.UPDATE, EntityCategory.DIAGNOSTIC,
            )
        )
        status = (coordinator.data or {}).get(dev.serial) or {}
        if status.get("disk_present"):
            entities.append(
                HikBinary(
                    coordinator, dev.serial, "disk", "Storage health",
                    # PROBLEM: on == problem, so invert disk_ok
                    lambda s: (not s["disk_ok"]) if s.get("disk_ok") is not None else None,
                    BinarySensorDeviceClass.PROBLEM, EntityCategory.DIAGNOSTIC,
                )
            )
    async_add_entities(entities)


class HikBinary(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, serial, key, name, value_fn, device_class, category):
        super().__init__(coordinator)
        self._serial = serial
        self._value_fn = value_fn
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_entity_category = category
        self._attr_unique_id = f"{DOMAIN}_{serial}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    @property
    def is_on(self) -> bool | None:
        return self._value_fn((self.coordinator.data or {}).get(self._serial) or {})
