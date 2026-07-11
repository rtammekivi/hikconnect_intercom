"""Constants for the hikconnect_intercom integration."""

DOMAIN = "hikconnect_intercom"

CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"
CONF_BASE_URL = "base_url"
CONF_SERVER = "server"

DEFAULT_BASE_URL = "https://api.hik-connect.com"

# Predefined Hik-Connect regional API servers (value -> label). The global
# entry auto-routes via the login region redirect; picking a region is just
# faster/deterministic. "custom" reveals the free-text override below.
SERVER_CUSTOM = "custom"
SERVERS = {
    DEFAULT_BASE_URL: "Global (auto-route)",
    "https://apiieu.hik-connect.com": "Europe (apiieu)",
    "https://apiius.hik-connect.com": "Americas (apiius)",
}

# Live MJPEG transcode defaults
MJPEG_FPS = 8
MJPEG_QUALITY = 5
MJPEG_WIDTH = 1280
MJPEG_HEIGHT = 720

# Door latch re-locks itself a few seconds after unlock; mirror that in HA.
DOOR_LATCH_UNLOCKED_FOR = 5

# Baseline poll interval (s) for the cloud call-status endpoint. MQTT push
# accelerates this to near real time, so the baseline can stay gentle.
CALL_POLL_INTERVAL = 10

# Device telemetry (online, wifi, firmware, disk) changes slowly — poll gently.
STATUS_POLL_INTERVAL = 60

CALL_STATUS_MAPPING = {1: "idle", 2: "ringing", 3: "call in progress"}
CALL_STATES = ["idle", "ringing", "call in progress"]  # "unknown" -> None (reserved state)

# Realtime push (EZVIZ MQTT backend, shared by Hik-Connect). A push event is
# only used as a trigger to immediately re-poll the authoritative call status,
# so we never have to guess which opaque alert code means "incoming call".
MQTT_APP_KEY = "4c6b3cc2-b5eb-4813-a592-612c1374c1fe"
MQTT_APP_SECRET = "17454517-cc1c-42b3-a845-99b4a15dd3e6"
PUSH_FEATURE_CODE = FEATURE_CODE = "deadbeefdeadbeef"


def call_signal(serial: str) -> str:
    return f"{DOMAIN}_call_push_{serial}"
