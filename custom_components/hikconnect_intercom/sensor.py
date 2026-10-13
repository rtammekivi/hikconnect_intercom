"""Sensors: call status (fast poll + push) and device telemetry (slow poll)."""

from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CALL_STATES, DOMAIN

_CALL_ICONS = {
    "idle": "mdi:phone-hangup",
    "ringing": "mdi:phone-ring",
    "call in progress": "mdi:phone-in-talk",
}


def _connection_type(status: dict):
    w = status.get("wireless")
    return None if w is None else ("wireless" if w else "wired")


def _last_offline(status: dict):
    ts = status.get("offline_timestamp")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)


# key, name, icon, unit, device_class, state_class, options, value_fn
_METRICS = (
    ("wifi_signal", "WiFi signal", "mdi:wifi", "%", None,
     SensorStateClass.MEASUREMENT, None, lambda s: s.get("wifi_signal")),
    ("local_ip", "LAN IP", "mdi:ip-network-outline", None, None, None, None,
     lambda s: s.get("local_ip")),
    ("wan_ip", "WAN IP", "mdi:ip-network", None, None, None, None,
     lambda s: s.get("wan_ip")),
    ("connection_type", "Connection type", "mdi:lan", None,
     SensorDeviceClass.ENUM, None, ["wired", "wireless"], _connection_type),
    ("storage_capacity", "Storage capacity", "mdi:sd",
     UnitOfInformation.GIGABYTES, SensorDeviceClass.DATA_SIZE,
     SensorStateClass.MEASUREMENT, None, lambda s: s.get("disk_capacity_gb")),
    ("last_offline", "Last offline", None, None,
     SensorDeviceClass.TIMESTAMP, None, None, _last_offline),
)


def _operation_time(key: str):
    return lambda s: (s.get("operation_time") or {}).get(key)


_STATION_METRICS = (
    ("sd_card", "SD card", "mdi:micro-sd", None, None, None, None,
     lambda s: (s.get("work_status") or {}).get("TFCardStatus")),
    ("max_ring_time", "Max ring time", "mdi:bell-ring-outline",
     UnitOfTime.SECONDS, SensorDeviceClass.DURATION, None, None,
     _operation_time("maxRingTime")),
    ("ring_duration", "Ring duration", "mdi:bell-outline",
     UnitOfTime.SECONDS, SensorDeviceClass.DURATION, None, None,
     _operation_time("ringDurationTime")),
    ("max_monitoring_time", "Max monitoring time", "mdi:cctv",
     UnitOfTime.SECONDS, SensorDeviceClass.DURATION, None, None,
     _operation_time("maxMonitoringTime")),
    ("call_forwarding_time", "Call forwarding time", "mdi:phone-forward",
     UnitOfTime.SECONDS, SensorDeviceClass.DURATION, None, None,
     _operation_time("callForwardingTime")),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    status = data["status_coordinator"]
    entities: list[SensorEntity] = []
    for dev in data["devices"]:
        entities.append(HikCallStatusSensor(coordinator, dev.serial))
        st = (status.data or {}).get(dev.serial) or {}
        for spec in _METRICS:
            if spec[0] == "storage_capacity" and not st.get("disk_present"):
                continue
            entities.append(HikMetricSensor(status, dev.serial, *spec))
        if dev.locks:
            entities.append(HikLastRingSensor(status, dev.serial))
            for spec in _STATION_METRICS:
                entities.append(HikMetricSensor(status, dev.serial, *spec))
    async_add_entities(entities)


class HikCallStatusSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Call status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CALL_STATES

    def __init__(self, coordinator, serial):
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{DOMAIN}_{serial}_call_status"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    def _entry(self) -> dict:
        return (self.coordinator.data or {}).get(self._serial) or {}

    @property
    def native_value(self) -> str | None:
        status = self._entry().get("status")
        return status if status in CALL_STATES else None  # reserved states -> unknown

    @property
    def icon(self) -> str:
        return _CALL_ICONS.get(self.native_value, "mdi:phone-alert")

    @property
    def extra_state_attributes(self) -> dict:
        return self._entry().get("info") or {}


class HikMetricSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, serial, key, name, icon, unit,
                 device_class, state_class, options, value_fn):
        super().__init__(coordinator)
        self._serial = serial
        self._value_fn = value_fn
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_options = options
        self._attr_unique_id = f"{DOMAIN}_{serial}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    @property
    def native_value(self):
        return self._value_fn((self.coordinator.data or {}).get(self._serial) or {})


class HikLastRingSensor(CoordinatorEntity, SensorEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = "Last ring"
    _attr_icon = "mdi:doorbell"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, serial):
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{DOMAIN}_{serial}_last_ring"
        self._restored: tuple[str, datetime] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state is None:
            return
        msg_id = state.attributes.get("msg_id")
        value = dt_util.parse_datetime(state.state)
        if msg_id and value:
            self._restored = (msg_id, value)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    def _ring(self) -> dict:
        return ((self.coordinator.data or {}).get(self._serial) or {}).get("last_ring") or {}

    @property
    def native_value(self) -> datetime | None:
        ring = self._ring()
        if self._restored and ring.get("msg_id") == self._restored[0]:
            return self._restored[1]
        ts = ring.get("time")
        if not isinstance(ts, (int, float)):
            return None
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

    @property
    def extra_state_attributes(self) -> dict:
        ring = self._ring()
        return {"calling_status": ring.get("calling_status"), "msg_id": ring.get("msg_id")}
