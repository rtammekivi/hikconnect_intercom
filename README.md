<p align="center">
  <img src="custom_components/hikconnect_local/icon.png" width="128" alt="Hik-Connect Local"/>
</p>

# Hik-Connect Local

Native **local** video for Hik-Connect indoor stations / video intercoms
(e.g. `DS-KH6320-WTE1`) in Home Assistant — no cloud relay, no phone, no
port-8000 admin password.

It logs into your **Hik-Connect account** only to (a) list your devices and their
LAN IP and (b) fetch each device's per-device stream key from the shared CAS
cloud. The video itself is pulled **directly from the station over your LAN** using
the CPD7 protocol (ports 9010/9020), decrypted/de-framed in pure Python, and
served to HA as an MJPEG camera.

This started as a fork of
[Bobsilvio/ezviz_hp7](https://github.com/Bobsilvio/ezviz_hp7) (the EZVIZ HP7
CPD7 work — thank you). The CPD7 `lan_client`, ECDH/ChaCha20 `crypto`, and the
`pylocalapi` CAS client are vendored under `custom_components/hikconnect_local/lib/`.
Hik-Connect indoor stations send the local media **unencrypted** (Hikvision RTP),
so a dedicated decoder (`lib/hik_decoder.py`) replaces the HP7 ChaCha20 path.

## How it works

```
Hik-Connect login ──► device list + LAN IP        (cloud, once)
                └────► CAS getDevOperationCode ──► 16-byte AES control key
Cpd7LanClient (9010 INIT/INVITE/PLAY, AES-128-CBC control) ──► 9020 stream
   └─► HikStreamDecoder: strip $01 framing + 12B RTP + 13B Hik header ──► H.264
        └─► ffmpeg H.264 ─► MJPEG ─► Home Assistant camera
```

## Install (HACS)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/rtammekivi/hikconnect_local`, type **Integration**.
2. Install **Hik-Connect Local**, restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Hik-Connect Local**,
   enter your Hik-Connect account + password.

A camera entity is created for each LAN-reachable device. Live view uses MJPEG
(codec-agnostic, low-latency, no go2rtc required); snapshots work too.

## Status / limits

- Verified on `DS-KH6320-WTE1` (H.264 Baseline 720p25).
- Requires HA to be on the **same LAN** as the station (routed subnets are fine
  as long as ports 9010/9020 are reachable).
- Video only for now — call answer/hangup and door unlock are not yet wired.
- The device allows a limited number of concurrent local streams; close the
  phone app's live view if a stream won't start.

See `RESEARCH.md` for the full protocol reverse-engineering notes.
