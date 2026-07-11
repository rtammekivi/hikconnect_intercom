"""Per-camera stream-quality select (HD = main stream, SD = sub stream).

The chosen value is stored in a dict shared with the camera platform; the next
stream/snapshot open uses it. An already-running live view must be reopened to
pick up a change.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN

_OPTION_TO_STREAM = {"HD": "MAIN", "SD": "SUB"}
_STREAM_TO_OPTION = {v: k for k, v in _OPTION_TO_STREAM.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    quality = data["quality"]
    async_add_entities(
        HikStreamQualitySelect(cam, quality) for cam in data["cameras"]
    )


class HikStreamQualitySelect(SelectEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:high-definition"
    _attr_options = list(_OPTION_TO_STREAM)
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, cam, quality: dict[str, str]):
        self._cam = cam
        self._quality = quality
        self._qkey = f"{cam.serial}_ch{cam.channel}"
        self._attr_name = f"{cam.name} stream quality"
        self._attr_unique_id = f"{DOMAIN}_{self._qkey}_quality"
        self._attr_current_option = "HD"
        self._quality.setdefault(self._qkey, "MAIN")

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._cam.serial)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in _OPTION_TO_STREAM:
            self._attr_current_option = last.state
            self._quality[self._qkey] = _OPTION_TO_STREAM[last.state]

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self._quality[self._qkey] = _OPTION_TO_STREAM[option]
        self.async_write_ha_state()
