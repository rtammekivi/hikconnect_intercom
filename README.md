<p align="center">
  <img src="custom_components/hikconnect_intercom/brand/icon.png" width="128" alt="Hik-Connect Intercom"/>
</p>

# Hik-Connect Intercom

Home Assistant integration for **Hikvision video intercoms / indoor stations**
(e.g. `DS-KH6320-WTE1`) that you **can't administer locally** — the common case
in **apartments and managed buildings**, where an installer or property-management
/ security company keeps the device admin password and the local ISAPI/SDK port
(8000). You only have the **Hik-Connect app account**, not the admin credentials —
so the usual local Hikvision integrations (which need that password) don't work.

This one needs only that account — **no admin password, no phone, no port-8000
access**:

- **Video is local.** The live stream is pulled **directly from the station over
  your LAN** (CPD7 protocol, ports 9010/9020), decrypted/de-framed in pure Python
  and served to HA as MJPEG — no cloud relay for the video.
- **Everything else is cloud**, using the same calls the app makes (authorized by
  your account session, no admin password): device discovery + LAN IP, the
  per-device stream key (CAS), door unlock, answer/hang-up/cancel, call status,
  volumes, Do-Not-Disturb, time, and telemetry — via the Hik-Connect cloud and its
  ISAPI passthrough.

> ⚠️ **Hybrid, not local-only.** Only the *video* is local (pulled from the station
> over your LAN — the hard part, and the differentiator from cloud-relay
> integrations); device discovery and all controls go through the Hik-Connect cloud.

This started as a fork of
[Bobsilvio/ezviz_hp7](https://github.com/Bobsilvio/ezviz_hp7) (the EZVIZ HP7
CPD7 work — thank you). The CPD7 `lan_client`, ECDH/ChaCha20 `crypto`, and the
`pylocalapi` CAS client are vendored under `custom_components/hikconnect_intercom/lib/`.
Hik-Connect indoor stations send the local media **unencrypted** (Hikvision RTP),
so a dedicated decoder (`lib/hik_decoder.py`) replaces the HP7 ChaCha20 path.

## How it works

**Video (local):**

```
Hik-Connect login ──► device list + LAN IP        (cloud, once)
                └────► CAS getDevOperationCode ──► 16-byte AES control key
Cpd7LanClient (9010 INIT/INVITE/PLAY, AES-128-CBC control) ──► 9020 stream
   └─► HikStreamDecoder: strip $01 framing + 12B RTP + 13B Hik header ──► H.264
        └─► ffmpeg H.264 ─► MJPEG ─► Home Assistant camera
```

**Controls / config / status (cloud):** relayed through the Hik-Connect cloud —
`/api/device/isapi` tunnels **standard Hikvision ISAPI** to the device (volumes,
time), `/v3/devconfig/.../remote/unlock` and `/call/.../operation` handle door +
call ops, `configTimeZone` + `nodisturb` cover DST + Do-Not-Disturb — **all
authorized by your account session, no device admin password needed.** This is
what makes the integration usable on locked-down apartment installs.

## Install (HACS)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/rtammekivi/hikconnect_intercom`, type **Integration**.
2. Install **Hik-Connect Intercom**, restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Hik-Connect Intercom**,
   enter your Hik-Connect account + password.

A camera entity is created for each LAN-reachable device. Live view uses MJPEG
(codec-agnostic, low-latency, no go2rtc required); snapshots work too.

## Entities

- **Camera** — one per real door-station channel (native local CPD7 video).
- **Lock** — one per unlock-capable channel; opens the door latch (momentary,
  auto-relocks after a few seconds). Uses the cloud remote-unlock endpoint.
- **Buttons** — *Answer call*, *Hang up call*, *Cancel call* (cloud call ops),
  *Unlock all doors* (opens every lock-capable channel at once), and *Sync time*
  (config; sets the station clock to HA's DST-aware local time via ISAPI).
- **Call status sensor** — `idle` / `ringing` / `call in progress`. State comes
  from the authoritative cloud poll; a realtime MQTT push event triggers an
  immediate re-poll so ringing shows in near real time.
- **Volume numbers** — Ringtone / Two-way audio / Microphone (0-10), driven via
  the Hik-Connect cloud **ISAPI passthrough** (`/api/device/isapi`) — standard
  Hikvision ISAPI relayed by the account session, no device admin password.
- **Do Not Disturb switch** — account-level; when on, calls from the device are
  silenced (`/v3/unifiedmsg/notify/nodisturb`).
- **Daylight-saving switch** — device time config (`/api/device/configTimeZone`).
- **Diagnostics** — connectivity, firmware-update, storage health binary sensors;
  WiFi signal, LAN IP, WAN IP, connection type, storage capacity, last-offline.
- **Stream quality select** — HD (main) / SD (sub) per camera.

All controls, config, and status go through the Hik-Connect **cloud** (authorized
by your account, no admin password) — the same operations the app performs. Only
the **video** is pulled locally over the LAN.

## Status / limits

- Verified on `DS-KH6320-WTE1` (H.264 Baseline 720p25, 2 door-station channels).
- Video requires HA on the **same LAN** as the station (routed subnets fine as
  long as ports 9010/9020 are reachable). Controls/status use the cloud.
- The device allows a limited number of concurrent local streams; close the
  phone app's live view if a stream won't start.

See `RESEARCH.md` for the full protocol reverse-engineering notes.
