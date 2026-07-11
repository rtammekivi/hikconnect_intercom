"""Volume controls (ringtone / two-way / microphone) via the cloud ISAPI tunnel.

Values are read with the device telemetry poll and written through
``/api/device/isapi`` (0-10). Read-modify-write is handled in the API layer.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

# (kind, name, icon)
_VOLUMES = (
    ("ringtone", "Ringtone volume", "mdi:bell-ring"),
    ("two_way", "Two-way audio volume", "mdi:account-voice"),
    ("microphone", "Microphone volume", "mdi:microphone"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client = data["client"]
    coordinator = data["status_coordinator"]
    entities = [
        HikVolumeNumber(coordinator, client, hass, dev.serial, *spec)
        for dev in data["devices"]
        if dev.locks  # intercoms only
        for spec in _VOLUMES
    ]
    async_add_entities(entities)


class HikVolumeNumber(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, client, hass, serial, kind, name, icon):
        super().__init__(coordinator)
        self.hass = hass
        self._client = client
        self._serial = serial
        self._kind = kind
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{DOMAIN}_{serial}_vol_{kind}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    @property
    def native_value(self) -> float | None:
        vols = ((self.coordinator.data or {}).get(self._serial) or {}).get("volumes") or {}
        v = vols.get(self._kind)
        return None if v is None else float(v)

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.async_add_executor_job(
            self._client.set_audio_volume, self._serial, self._kind, int(value)
        )
        await self.coordinator.async_request_refresh()
