"""Print Hik-Connect MQTT push messages; poll call status/caller info after each one.

usage: push_monitor.py SERIAL
"""

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import requests

REPO = Path(__file__).resolve().parents[4]
sys.path.append(str(REPO / "custom_components/hikconnect_intercom"))

from const import MQTT_APP_KEY as KEY, MQTT_APP_SECRET as SECRET  # noqa: E402
from hikconnect_api import HikConnectClient  # noqa: E402

serial = sys.argv[1]
creds = Path(os.environ.get("HIK_CREDS", REPO.parent / ".hik"))
account, password = [x.strip() for x in creds.read_text().splitlines()[:2]]
client = HikConnectClient(account, password)
client.login()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


auth = "Basic " + base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()


def register(host):
    return requests.post(
        f"https://{host}/v1/getClientId",
        data=dict(appKey=KEY, clientType="5", mac=hashlib.md5(b"hc-debug").hexdigest(),
                  token="123456", version="v1.3.0"),
        headers={"Authorization": auth}, timeout=20,
    ).json()["data"]


push_host = client._get("/v3/configurations/system/info")["systemConfigInfo"]["pushAddr"]
try:
    reg = register(push_host)
except (requests.RequestException, ValueError, KeyError):
    push_host = push_host.replace(".hik-connect.com", ".ezvizlife.com")
    reg = register(push_host)
log("push host", push_host)
client_id = reg["clientId"]
broker_host, broker_port = reg["mqtts"].split("://")[-1].split(":")
requests.post(
    f"https://{push_host}/api/push/start",
    data=dict(appKey=KEY, clientId=client_id, clientType=5, sessionId=client._session_id,
              username=client._username, token="123456"),
    timeout=20,
)

burst_until = [0.0]


def on_message(_c, _u, msg):
    log("PUSH", msg.payload.decode("utf-8", "replace"))
    burst_until[0] = time.time() + 40


m = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=False)
m.username_pw_set(KEY, SECRET)
m.on_connect = lambda c, *_: (c.subscribe(f"{KEY}/#", qos=2), log("mqtt connected"))
m.on_message = on_message
m.connect(broker_host, int(broker_port), 30)
m.loop_start()

while True:
    if time.time() < burst_until[0]:
        data = client.isapi(serial, "GET", "/ISAPI/VideoIntercom/callStatus?format=json").get("data")
        status = json.loads(data or "{}").get("CallStatus", {}).get("status")
        log("callStatus", status)
        if status and status != "idle":
            log("callerInfo", client.isapi(serial, "GET", "/ISAPI/VideoIntercom/callerInfo?format=json").get("data"))
    time.sleep(2)
