# Hik-Connect reverse-engineering notes

Research log for making a DS-KH6320-WTE1 indoor station usable in Home Assistant
when the device is **managed by a third party (no admin password)** and the
Hik-Connect cloud call-status endpoint has been retired.

Device under test: `DS-KH6320-WTE1`, serial `<SERIAL>`, firmware `V2.2.108`,
LAN IP `<DEVICE-LAN-IP>`. Account is a plain Hik-Connect personal account.

---

## 1. Call status (SOLVED — shipped in this fork, v2.5.0)

The legacy integration polled `GET /v3/devconfig/v1/call/{serial}/status`. That
endpoint is dead server-side since ~Dec 2025 (returns `meta.code` 2003/2009,
"device not online / network abnormal") even though the app works — see upstream
issue #61. No payload-parsing patch can fix it.

**Real mechanism:** the app receives calls over the **EZVIZ MQTT push channel**
(Hik-Connect shares the EZVIZ backend). Reproduced end-to-end:

1. `POST {base}/v3/users/login/v2` (md5 password) → `sessionId`, `username`.
2. `GET {base}/v3/configurations/system/info` → `systemConfigInfo.pushAddr`
   (`pusheu.hik-connect.com`).
3. `POST https://{pushAddr}/v1/getClientId` — HTTP Basic `base64(APP_KEY:APP_SECRET)`,
   form `appKey,clientType=5,mac=<featureCode>,token=123456,version=v1.3.0`
   → `clientId` + broker `tcp://pusheu.ezvizlife.com:1882`.
4. `POST https://{pushAddr}/api/push/start` — form
   `appKey,clientId,clientType=5,sessionId,username,token=123456` → ticket.
5. MQTT connect broker:1882, `username_pw_set(APP_KEY, APP_SECRET)`,
   `clientId=<clientId>`, subscribe topic `{APP_KEY}/#` qos2.

App key/secret (EZVIZ app identity, public, shipped in pyEzvizApi):
`APP_KEY=4c6b3cc2-b5eb-4813-a592-612c1374c1fe`,
`APP_SECRET=17454517-cc1c-42b3-a845-99b4a15dd3e6`.

Each push message has an `ext` CSV whose fields are
`channel_type,time,device_serial,channel_no,alert_type_code,...` (see
`custom_components/hikconnect/push.py`). This fork subscribes and flips the call
sensor to `ringing` on an event. **Remaining:** capture one real doorbell call to
learn the exact `alert_type_code` for this model and tighten `CALL_ALERT_CODES`.

---

## 2. Live video

### 2a. What does NOT work

- **Standard HA Hikvision integrations** (`hikvision`, `hikvision_next`, doorbell
  add-on): all use ISAPI (HTTP 80/443), RTSP (554) or the SDK (8000) with the
  **device admin password**. On this unit 80/443/554 are closed/refused, and
  8000 speaks the NET_DVR SDK handshake (needs the password we don't have).
- **Hik-Connect web portal** (`ieu.hik-connect.com`): the free/Personal tier is a
  micro-frontend with only `HCBLogin`+`HCBPortal` provisioned. Live view is a
  separate `HCBVideo` child app that is **not deployed** to Personal accounts.
- **Cloud VTM relay** (`/v3/streaming/vtm/{serial}/{channel}`): returns a relay
  server + EC public key, but the transport is EZVIZ's proprietary encrypted P2P
  (the relay `vtmcdsfra.ezvizlife.com:8554` silently drops a plain RTSP OPTIONS).

### 2b. Frida extraction (WORKS — proven with real 720p footage)

On a rooted Android phone with the app (`com.connect.enduser`), tap the decrypted
stream where the app hands it to Hikvision's player:

- Hook `libPlayCtrl.so!PlayM4_InputData(port, buf, size)`. This receives the
  already-decrypted stream (~1400-byte RTP packets). Confirmed: 5000+ calls
  during live view; other decode entry points (AMediaCodec, NET_DVR) are unused.
- Write each buffer with a 4-byte LE length prefix (frida 17 `File.write` is
  broken — use libc `open/write/close` via `NativeFunction`; write to the app's
  own cache dir `/data/data/com.connect.enduser/cache/`, SELinux blocks
  `/data/local/tmp` for the app uid).
- De-packetize RTP→Annex-B H.264 (single-NAL / STAP-A / FU-A). Result decodes as
  **H.264 Baseline 1280×720 25fps** — clean picture.

Android-16/frida gotchas learned the hard way:
- frida's **Java bridge crashes** on this ART build (`art::JNI::FindClass`
  SIGSEGV). Use **native hooks only**.
- Hooking `dlopen`/`android_dlopen_ext` **breaks RenderScript** (`BlurKitStartup`
  → `rsContextCreate` null-deref crash). Don't hook dlopen; poll
  `Process.findModuleByName` instead.
- The app has **no anti-debug**; frida spawn/attach both work.

### 2c. Local direct protocol (the native goal — BLOCKED on a cipher)

When on-LAN, the app does **not** use the cloud: it connects straight to the
station. Captured with `tcpdump` on the rooted phone:

- `<DEVICE-LAN-IP>:9010` — command channel (login, stream-login, stop).
- `<DEVICE-LAN-IP>:9020` — stream channel (the video).
- Ports 9010/9020/8000 are **open and reachable from the HA LAN**
  (192.168.2.x → 192.168.4.x is routed).

**Message framing** (`9e ba ac e9` magic; big picture below), 32-byte header +
payload + 32-char ASCII trailer:

| off | field |
|-----|-------|
| 0   | magic `9e ba ac e9` |
| 4   | version `01 00 00 00` |
| 8   | seq (global counter, increments per request across the session) |
| 16  | command code (see below) |
| 20  | session (`ff ff ff ff` before/where unused) |
| 24  | payload length (BE32) |
| 32  | payload = `cred48` (static credential) + command params |
| 32+len | trailer = **`md5(payload)`** as 32 lowercase hex chars (no secret — cracked) |

Command codes seen: `0x3003` device-info, `0x2011` stream-login (→ `Session` +
`StreamHeader` whose base64 decodes to `IMKH`, the Hikvision stream magic),
`0x3105` stream-start (on 9020), `0x2013` stop.

**Replay results (from a dev box on the LAN, different IP than the phone):**
- `0x3003` device-info → **works**, returns plaintext XML (DevName/Serial/FW).
- `0x2011` stream-login → **works**, returns `Result 0` + a fresh incrementing
  `Session` + the `IMKH` StreamHeader.
- `0x3105` stream-start → **fails, `Result 129`.**

The `cred48` credential is **static and replayable** (identical across
connections and ports — an account-derived local token, NOT the admin password).
But the rest of the `0x3105` payload (an 80-byte blob) is **100% different every
session** (diffed two sessions). A replayed login *works* while a replayed
stream-start *fails*, so the stream-start is bound to a **per-session key the
login negotiates** (the `CHIKEncrypt` class exposes `GenerateRSAKey` /
`DecryptByPrivateKey`, i.e. RSA key exchange → AES session key).

### 2d. The cipher wall

To forge `0x3105` natively we must reproduce that session key + the blob
encryption. Findings:

- The app libs are **not stripped** — `libHCCore.so` exports a full
  `NetSDK::CHIKEncrypt` API (`SetAesCbcKey`, `SetAesCbcIv`, `AesCbcEncrypt`,
  `AesEcbEncrypt`, `DecryptByPrivateKey`, `GenerateRSAKey`), plus
  `CoreBase_EncryptByAes{Cbc,Ecb}`, `Interim_EncryptByAes*`, `Core_SimpleEncrypt`,
  `Core_ENCRYPT_LevelFiveEncrypt`, `SSLTrans_Aes*`; `libezLongLink.so` has
  `bscomptls_aes_*`. `libHCCore` statically links OpenSSL AES
  (`AES_cbc_encrypt`, `AES_set_encrypt_key` strings present).
- **Hooked all ~20 of these** (cold-start spawn and running-attach, app streaming)
  → **zero hits.** A simultaneous `tcpdump`+frida run proved the instrumented app
  connected **entirely locally** (9010/9020, no cloud) yet still called none of
  them.
- Therefore the 9010/9020 request crypto is **statically inlined in
  `libhcnetsdk`'s request-builder** (or a non-exported internal), using its own
  OpenSSL AES copy. There is no exported function to hook.
- The protocol magic `9e ba ac e9` is not stored as a literal, and standard AES
  Rijndael / SM4 S-box tables are not present as byte tables — consistent with a
  bitsliced/no-table OpenSSL AES and a runtime-built header.

The streaming is NOT in `libhcnetsdk` — a send-backtrace (`HPR_Send`) showed the
whole path is in **`libezstreamclient.so`** (the EZVIZ `ez_stream_sdk` CAS
client): `ezplayer_start → EZMediaPreview::start → DirectClient::startPreview →
CASClient_Start → CCtrlClient::SendInviteStream → SendRequest → SendDataToDev →
CMbedtlsClient::TCPSendSSLMsg → HPR_Send`. That lib has named crypto
(`ECDHCryption_*`, its OWN statically-linked `mbedtls_aes_*` and
`mbedtls_chacha20_*`). Hooking `libezstreamclient.so!mbedtls_aes_setkey_enc` +
`mbedtls_aes_crypt_cbc` (while driving the app via `adb input tap`) captured the
control key and plaintext.

### 2e. SOLVED — this is the CPD7 protocol (ezviz_hp7)

The protocol is **CPD7**, already implemented in Python by the `ezviz_hp7` HA
integration (`custom_components/ezviz_hp7/cpd7/`). What we misread as a "custom
cipher" is just **AES-128-CBC with a FIXED IV** (`b"01234567" + \x00*8`) and a
**fixed 16-byte control key**. The "static cred48" is simply the CBC ciphertext of
the identical XML prefix (fixed IV → identical output). Replay failed only because
the PLAY body encodes the session number.

Confirmed on our device:
- Control key (extracted via frida): **`<DEVICE-AES-KEY-16B>`** (16 ASCII bytes).
- Flow: INIT(0x2013) / INVITE(0x2011, carries our ECDH pubkey) → `<Session>` +
  `IMKH` StreamHeader → PLAY(0x3105) on 9020; stream flows on the play socket.
- Ran `ezviz_hp7`'s `Cpd7LanClient` with our key from a dev box on the LAN →
  authenticated and pulled ~500 KB of live H.264. **Native, no phone/cloud/frida.**

**Media format differs from HP7.** HP7 wraps media in ChaCha20 `$\x02` packets;
our indoor station sends **UNENCRYPTED** media. Wire framing per media unit:

```
$ <ch:1> <len:2 BE>         # RTSP-interleaved; ch == CPD7 channel (ch1 -> \x01, ch2 -> \x02)
  <12-byte RTP header>       # 80 60 <seq> <ts> <ssrc=session>
  <13-byte Hikvision header> # payload[0]=0x0d, contains a per-packet counter
  <RFC 6184 H.264 payload>   # 0x67 SPS / 0x68 PPS / single NAL / 0x7c FU-A
```

The RTSP-interleave channel byte **equals the CPD7 channel** (Main Door Station
ch1 → `$\x01`, Sub Door Station ch2 → `$\x02`). The decoder MUST sync on
`$<ch>`, not a hard-coded `$\x01`; otherwise a non-ch1 stream is missed entirely
and stray `0x24` bytes inside the payload get parsed as garbage NALs.

Decode = strip `$<ch>` framing → drop 12-byte RTP + 13-byte Hik header →
standard RFC 6184 depacketize (single-NAL / STAP-A / FU-A) → Annex-B H.264.
Verified: `ffprobe` → **H.264 Baseline 1280×720 25 fps**, decodes to a clean live
frame of the door. See scratchpad `decode_native.py`.

**FULLY SELF-CONTAINED — confirmed.** The control key is fetchable from the cloud
with a plain Hik-Connect account, no frida: log in → `GET
/v3/configurations/system/info` gives `sysConf` whose CAS server is
`eucas.ezvizlife.com:6500` (shared EZVIZ/Hik-Connect backend) → feed the
Hik-Connect `session_id` + `sysConf` to `pylocalapi.cas.EzvizCAS` →
`cas_get_encryption(serial)` returns:

```
Result=0  DevSerial=<SERIAL>  Algorithm=AES128
OperationCode=ABCDEFG  Key=<DEVICE-AES-KEY-16B>   (== the frida-extracted key)
```

End-to-end proven from a LAN box: `HikConnectClient.login()` → `get_devices()`
(LAN IP <DEVICE-LAN-IP>) → `get_control_key()` (CAS) → `Cpd7LanClient` →
`HikStreamDecoder` → **328 KB H.264, ffprobe = Baseline 1280×720 25fps, decodes to
a live door frame.** No phone, no frida, no hard-coded key. Built in
`rtammekivi/hikconnect_local`: `hikconnect_api.py`, `lib/hik_decoder.py`.

---

## 3. Native live video in HA — the plan (PROVEN)

Fork `ezviz_hp7` and adapt for Hik-Connect:

1. **CPD7 `lan_client`** — works as-is with our device + control key.
2. **Media decoder** — add a Hikvision branch: no ChaCha20; strip 12-byte RTP +
   13-byte Hik header → RFC 6184 → H.264 (see `decode_native.py`).
3. **Auth** — replace EZVIZ cloud login with the Hik-Connect API (login → device
   list → device LAN IP + the CPD7 control key). Confirm the key is fetchable from
   the account (else a one-time on-device grab).
4. **Output** — feed H.264 to `go2rtc` → RTSP/WebRTC camera entity in HA.

Everything up to (3)'s key-fetch is proven end-to-end from a LAN dev box.

---

## Repro toolbox (all in the research scratchpad, not shipped)

- `mqtt_listen.py` — call-status MQTT listener.
- `extract4.js` (frida) — dump `PlayM4_InputData` with length framing.
- `parse_rtp.py` — RTP → Annex-B H.264.
- `local_proto.js` / `tcpdump` — capture the 9010/9020 handshake.
- `analyze.py` — framing + `md5(payload)` trailer proof.
- `replay_stream.py` — native login/stream replay from the LAN.
