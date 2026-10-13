"""Call-operation buttons (answer / hang up) for a Hik-Connect station."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN



async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client = data["client"]
    coordinator = data["coordinator"]
    registry = er.async_get(hass)
    entities = []
    for dev in data["devices"]:
        stale = registry.async_get_entity_id("button", DOMAIN, f"{DOMAIN}_{dev.serial}_cancel")
        if stale:
            registry.async_remove(stale)
        entities.append(HikAnswerButton(hass, client, coordinator, dev.serial))
        entities.append(HikHangUpButton(hass, client, coordinator, dev.serial))
        if dev.locks:
            entities.append(HikUnlockAllButton(hass, client, dev))
            entities.append(HikSyncTimeButton(hass, client, dev.serial))
    async_add_entities(entities)


def _set_call_status(coordinator, serial, status):
    data = dict(coordinator.data or {})
    data[serial] = {**(data.get(serial) or {}), "status": status}
    coordinator.async_set_updated_data(data)


class HikAnswerButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Answer call"
    _attr_icon = "mdi:phone"

    def __init__(self, hass, client, coordinator, serial):
        self.hass = hass
        self._client = client
        self._coordinator = coordinator
        self._serial = serial
        self._attr_unique_id = f"{DOMAIN}_{serial}_answer"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(self._client.answer_call, self._serial)
        _set_call_status(self._coordinator, self._serial, "call in progress")


class HikHangUpButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Hang up call"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, hass, client, coordinator, serial):
        self.hass = hass
        self._client = client
        self._coordinator = coordinator
        self._serial = serial
        self._attr_unique_id = f"{DOMAIN}_{serial}_hangup"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    async def async_press(self) -> None:
        status = ((self._coordinator.data or {}).get(self._serial) or {}).get("status")
        await self.hass.async_add_executor_job(
            self._client.end_call, self._serial, status
        )
        _set_call_status(self._coordinator, self._serial, "idle")


class HikUnlockAllButton(ButtonEntity):
    """Unlock every lock-capable channel on the station in one press."""

    _attr_has_entity_name = True
    _attr_name = "Unlock all doors"
    _attr_icon = "mdi:lock-open-variant"

    def __init__(self, hass, client, dev):
        self.hass = hass
        self._client = client
        self._dev = dev
        self._attr_unique_id = f"{DOMAIN}_{dev.serial}_unlock_all"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._dev.serial)})

    async def async_press(self) -> None:
        def work():
            for channel, count in sorted(self._dev.locks.items()):
                for idx in range(count):
                    self._client.unlock(self._dev.serial, channel, idx)

        await self.hass.async_add_executor_job(work)


class HikSyncTimeButton(ButtonEntity):
    """Set the station clock to Home Assistant's current time."""

    _attr_has_entity_name = True
    _attr_name = "Sync time"
    _attr_icon = "mdi:clock-check"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass, client, serial):
        self.hass = hass
        self._client = client
        self._serial = serial
        self._attr_unique_id = f"{DOMAIN}_{serial}_sync_time"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(
            self._client.set_time_now, self._serial, dt_util.now()
        )
