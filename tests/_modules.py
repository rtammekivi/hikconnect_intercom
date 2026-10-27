"""Load integration modules without running Home Assistant setup.

Only HA's import-time interfaces are stubbed. Tests exercise the actual decoder,
cloud client, and camera methods with mocked network/process boundaries.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components/hikconnect_intercom"


def load_module(name):
    package = "_hikconnect_regression"
    if package not in sys.modules:
        module = ModuleType(package)
        module.__path__ = [str(ROOT)]
        sys.modules[package] = module
    full_name = f"{package}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(
        full_name, ROOT / (name.replace(".", "/") + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def load_camera():
    interfaces = {
        "homeassistant.components.camera": {"Camera": type("Camera", (), {})},
        "homeassistant.components.ffmpeg": {"get_ffmpeg_manager": lambda hass: None},
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.helpers.entity": {"DeviceInfo": dict},
        "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
    }
    modules = {}
    for name, attributes in interfaces.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        modules[name] = module
    with patch.dict(sys.modules, modules):
        return load_module("camera")


def load_event():
    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

        def _handle_coordinator_update(self):
            pass

    interfaces = {
        "homeassistant.components.event": {
            "EventDeviceClass": type("EventDeviceClass", (), {"DOORBELL": "doorbell"}),
            "EventEntity": type("EventEntity", (), {}),
        },
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.core": {"HomeAssistant": object, "callback": lambda f: f},
        "homeassistant.helpers.entity": {"DeviceInfo": dict},
        "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
        "homeassistant.helpers.update_coordinator": {"CoordinatorEntity": CoordinatorEntity},
    }
    modules = {}
    for name, attributes in interfaces.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        modules[name] = module
    with patch.dict(sys.modules, modules):
        return load_module("event")


def load_longlink():
    interfaces = {
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.helpers.storage": {"Store": object},
    }
    modules = {}
    for name, attributes in interfaces.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        modules[name] = module
    with patch.dict(sys.modules, modules):
        return load_module("longlink")


def load_push():
    interfaces = {
        "paho": {},
        "paho.mqtt": {},
        "paho.mqtt.client": {"Client": object, "MQTTv311": 4},
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.helpers.aiohttp_client": {"async_get_clientsession": lambda hass: hass.session},
        "homeassistant.helpers.dispatcher": {"async_dispatcher_send": lambda *a: None},
    }
    modules = {}
    for name, attributes in interfaces.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        modules[name] = module
    modules["paho"].mqtt = modules["paho.mqtt"]
    modules["paho.mqtt"].client = modules["paho.mqtt.client"]
    with patch.dict(sys.modules, modules):
        return load_module("push")
