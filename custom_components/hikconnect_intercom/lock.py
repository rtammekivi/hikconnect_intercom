"""Door-latch lock entities for Hik-Connect outdoor-station channels.

There is no lock-state feedback from the device, so this is an assumed-state,
momentary control: unlocking opens the latch and it auto-relocks after a few
seconds (mirroring the physical latch).
"""

from __future__ import annotations

import logging

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, DOOR_LATCH_UNLOCKED_FOR

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client = data["client"]
    names = {
        (c.serial, c.channel): c.name for c in data["cameras"]
    }
    entities = []
    for dev in data["devices"]:
        for channel, count in sorted(dev.locks.items()):
            base = names.get((dev.serial, channel), f"Channel {channel}")
            for lock_index in range(count):
                entities.append(
                    HikLock(hass, client, dev.serial, channel, lock_index, base)
                )
    async_add_entities(entities)


class HikLock(LockEntity):
    _attr_has_entity_name = True
    _attr_assumed_state = True

    def __init__(self, hass, client, serial, channel, lock_index, base_name):
        self.hass = hass
        self._client = client
        self._serial = serial
        self._channel = channel
        self._lock_index = lock_index
        name = f"{base_name} lock"
        if lock_index:
            name += f" {lock_index + 1}"
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{serial}_ch{channel}_lock{lock_index}"
        self._attr_is_locked = True

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    async def async_lock(self, **kwargs) -> None:
        self._attr_is_locked = True
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(
            self._client.unlock, self._serial, self._channel, self._lock_index
        )
        self._attr_is_locked = False
        self.async_write_ha_state()

        async def _relock(_now):
            await self.async_lock()

        async_call_later(self.hass, DOOR_LATCH_UNLOCKED_FOR, _relock)
