"""Call Hik-Connect cloud / ISAPI-passthrough endpoints with the integration's client.

usage: cloud_probe.py get /v3/...           cloud GET
       cloud_probe.py isapi SERIAL /ISAPI/...  ISAPI GET via the cloud relay
       cloud_probe.py devices                device list with support_isapi
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.append(str(REPO / "custom_components/hikconnect_intercom"))

from hikconnect_api import HikConnectClient  # noqa: E402

creds = Path(os.environ.get("HIK_CREDS", REPO.parent / ".hik"))
account, password = [x.strip() for x in creds.read_text().splitlines()[:2]]
client = HikConnectClient(account, password)
client.login()

cmd, *args = sys.argv[1:] or ["devices"]
if cmd == "get":
    print(json.dumps(client._get(args[0]), indent=1, ensure_ascii=False))
elif cmd == "isapi":
    print(client.isapi(args[0], "GET", args[1]).get("data"))
elif cmd == "devices":
    for dev in client.get_devices():
        print(dev)
