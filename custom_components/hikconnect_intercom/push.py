import base64
import hashlib
import json
import logging

import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    MQTT_APP_KEY,
    MQTT_APP_SECRET,
    PUSH_FEATURE_CODE,
    call_signal,
)

_LOGGER = logging.getLogger(__name__)

_MQTT_PORT = 1882

# CSV field order of the message `ext` string (from EZVIZ push protocol).
_EXT_FIELDS = (
    "channel_type", "time", "device_serial", "channel_no", "alert_type_code",
    "default_pic_url", "media_url_alt1", "media_url_alt2", "resource_type",
    "status_flag", "file_id", "is_encrypted", "pic_checksum", "is_dev_video",
    "metadata", "msg_id", "image", "device_name", "reserved", "sequence_number",
)


class HikConnectPush:
    """Realtime call-event listener over the Hik-Connect/EZVIZ MQTT push channel."""

    def __init__(self, hass: HomeAssistant, base_url: str, account: str, password: str):
        self._hass = hass
        self._base_url = base_url
        self._account = account
        self._password = password
        self._client = None

    async def async_start(self) -> None:
        session = async_get_clientsession(self._hass)
        base, session_id, username = await self._login(session)
        push_addr = await self._get_push_addr(session, base, session_id)
        client_id, broker = await self._register(session, push_addr)
        await self._start_push(session, push_addr, client_id, session_id, username)
        await self._hass.async_add_executor_job(self._connect, broker, client_id)
        _LOGGER.info("Hik-Connect push listener started (broker=%s)", broker)

    async def async_stop(self) -> None:
        if self._client is not None:
            await self._hass.async_add_executor_job(self._disconnect)

    async def _login(self, session):
        data = {
            "account": self._account,
            "password": hashlib.md5(self._password.encode("utf-8")).hexdigest(),
        }
        headers = {"clientType": "55", "lang": "en-US", "featureCode": PUSH_FEATURE_CODE}
        base = self._base_url
        async with session.post(f"{base}/v3/users/login/v2", data=data, headers=headers) as r:
            j = await r.json()
        if j["meta"]["code"] == 1100:
            base = f"https://{j['loginArea']['apiDomain']}"
            async with session.post(f"{base}/v3/users/login/v2", data=data, headers=headers) as r:
                j = await r.json()
        return base, j["loginSession"]["sessionId"], j["loginUser"]["username"]

    async def _get_push_addr(self, session, base, session_id):
        headers = {
            "clientType": "55", "lang": "en-US",
            "featureCode": PUSH_FEATURE_CODE, "sessionId": session_id,
        }
        async with session.get(f"{base}/v3/configurations/system/info", headers=headers) as r:
            j = await r.json()
        return j["systemConfigInfo"]["pushAddr"]

    async def _register(self, session, push_addr):
        auth = "Basic " + base64.b64encode(
            f"{MQTT_APP_KEY}:{MQTT_APP_SECRET}".encode("ascii")
        ).decode()
        data = {
            "appKey": MQTT_APP_KEY, "clientType": "5", "mac": PUSH_FEATURE_CODE,
            "token": "123456", "version": "v1.3.0",
        }
        async with session.post(
            f"https://{push_addr}/v1/getClientId", data=data,
            headers={"Authorization": auth}, allow_redirects=False,
        ) as r:
            j = await r.json()
        d = j["data"]
        return d["clientId"], d.get("mqtts", f"tcp://{push_addr}:{_MQTT_PORT}")

    async def _start_push(self, session, push_addr, client_id, session_id, username):
        data = {
            "appKey": MQTT_APP_KEY, "clientId": client_id, "clientType": 5,
            "sessionId": session_id, "username": username, "token": "123456",
        }
        async with session.post(
            f"https://{push_addr}/api/push/start", data=data, allow_redirects=False,
        ) as r:
            j = await r.json()
        if j.get("status") != 200:
            _LOGGER.warning("Hik-Connect push/start returned %s", j)

    def _connect(self, broker, client_id):
        tail = broker.split("://")[-1]
        host = tail.split(":")[0]
        port = int(tail.split(":")[1]) if ":" in tail else _MQTT_PORT

        kwargs = dict(
            client_id=client_id, clean_session=False,
            protocol=mqtt.MQTTv311, transport="tcp",
        )
        cbver = getattr(mqtt, "CallbackAPIVersion", None)
        if cbver is not None:
            kwargs["callback_api_version"] = cbver.VERSION1

        c = mqtt.Client(**kwargs)
        c.username_pw_set(MQTT_APP_KEY, MQTT_APP_SECRET)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.reconnect_delay_set(min_delay=5, max_delay=60)
        c.connect(host, port, keepalive=60)
        self._client = c
        c.loop_start()

    def _disconnect(self):
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._client = None

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(f"{MQTT_APP_KEY}/#", qos=2)
            _LOGGER.debug("Hik-Connect push MQTT connected and subscribed")
        else:
            _LOGGER.warning("Hik-Connect push MQTT connect failed rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            _LOGGER.debug("Undecodable push payload: %r", msg.payload[:200])
            return

        ext = data.get("ext")
        if isinstance(ext, str):
            parts = ext.split(",")
            ext = {n: (parts[i] if i < len(parts) else None) for i, n in enumerate(_EXT_FIELDS)}
            data["ext"] = ext
        if not isinstance(ext, dict):
            ext = {}

        serial = ext.get("device_serial")
        alert_code = ext.get("alert_type_code")
        _LOGGER.info("Hik-Connect push: serial=%s alert_code=%s", serial, alert_code)
        if not serial:
            return
        try:
            alert_code = int(alert_code)
        except (TypeError, ValueError):
            pass

        self._hass.loop.call_soon_threadsafe(
            async_dispatcher_send, self._hass, call_signal(serial), alert_code, data
        )
