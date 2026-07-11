"""Call-operation buttons (answer / cancel / hangup) for a Hik-Connect station."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN

# (key, label, icon, api-method name)
_BUTTONS = (
    ("answer", "Answer call", "mdi:phone", "answer_call"),
    ("hangup", "Hang up call", "mdi:phone-hangup", "hangup_call"),
    ("cancel", "Cancel call", "mdi:phone-cancel", "cancel_call"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client = data["client"]
    entities = [
        HikCallButton(hass, client, dev.serial, *spec)
        for dev in data["devices"]
        for spec in _BUTTONS
    ]
    for dev in data["devices"]:
        if dev.locks:
            entities.append(HikUnlockAllButton(hass, client, dev))
            entities.append(HikSyncTimeButton(hass, client, dev.serial))
    async_add_entities(entities)


class HikCallButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, hass, client, serial, key, label, icon, method):
        self.hass = hass
        self._client = client
        self._serial = serial
        self._method = method
        self._attr_name = label
        self._attr_icon = icon
        self._attr_unique_id = f"{DOMAIN}_{serial}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(
            getattr(self._client, self._method), self._serial
        )


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
