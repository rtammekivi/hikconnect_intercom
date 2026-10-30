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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, DOOR_LATCH_UNLOCKED_FOR
from .doors import slot_for_channel

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client = data["client"]
    status = data["status_coordinator"]
    names = {
        (c.serial, c.channel): c.name for c in data["cameras"]
    }
    entities = []
    for dev in data["devices"]:
        pins = data["door_pins"].get(dev.serial, {})
        slots = data["door_slots"].get(dev.serial)
        presence = data["door_presence"].get(dev.serial)
        for channel, count in sorted(dev.locks.items()):
            slot = channel if slots is None else slot_for_channel(channel, pins, slots)
            if slot is None:
                continue
            base = names.get((dev.serial, channel), f"Channel {channel}")
            for lock_index in range(count):
                entities.append(
                    HikLock(
                        hass, client, dev.serial, channel, lock_index, base,
                        slot, pins.get(slot), status, presence,
                    )
                )
    async_add_entities(entities)


class HikLock(LockEntity):
    _attr_has_entity_name = True
    _attr_assumed_state = True

    def __init__(
        self, hass, client, serial, channel, lock_index, base_name, slot, door_ip,
        status=None, presence=None,
    ):
        self.hass = hass
        self._client = client
        self._serial = serial
        self._channel = channel
        self._lock_index = lock_index
        self._door_ip = door_ip
        self._slot = slot
        self._status = status
        self._presence = presence
        name = f"{base_name} lock"
        if lock_index:
            name += f" {lock_index + 1}"
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{serial}_ch{slot}_lock{lock_index}"
        self._attr_is_locked = True

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    @property
    def available(self) -> bool:
        return self._presence is None or self._presence.online(self._slot)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._presence is not None:
            self.async_on_remove(self._status.async_add_listener(self.async_write_ha_state))

    async def async_lock(self, **kwargs) -> None:
        self._attr_is_locked = True
        self.async_write_ha_state()

    def _current_channel(self) -> int:
        if self._door_ip is None:
            return self._channel
        try:
            doors = self._client.get_door_stations(self._serial)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Cannot confirm which door {self.name} is: {err}") from err
        channels = [ch for ch, door in doors.items() if door["ip"] == self._door_ip]
        if len(channels) != 1:
            raise HomeAssistantError(f"Door station for {self.name} is not connected")
        return channels[0]

    async def async_unlock(self, **kwargs) -> None:
        def work():
            self._client.unlock(self._serial, self._current_channel(), self._lock_index)

        await self.hass.async_add_executor_job(work)
        self._attr_is_locked = False
        self.async_write_ha_state()

        async def _relock(_now):
            await self.async_lock()

        async_call_later(self.hass, DOOR_LATCH_UNLOCKED_FOR, _relock)
