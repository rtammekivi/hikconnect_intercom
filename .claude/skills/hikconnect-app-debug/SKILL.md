---
name: hikconnect-app-debug
description: Reverse-engineer the Hik-Connect Android app (com.connect.enduser) to find or verify the cloud/ISAPI calls behind a feature. Use when porting an app feature to this integration, debugging a control that works in the app but not in Home Assistant, or capturing what the app sends on a button tap.
---

# Debugging the Hik-Connect mobile app

Three tools, cheapest first: read the decompiled app, call the API directly,
then capture the live app with frida. All scratch output goes in `/tmp`, never
the repo.

## Prerequisites

- Phone on USB with `adb devices` listing it; `adb root` works on this phone
  (no `su`).
- Account credentials in `~/code/hikconnect/.hik` (line 1 account, line 2
  password), outside the repo. Override with `HIK_CREDS=/path`.
- Python with the integration's deps:
  ```sh
  PYENV='with import <nixpkgs> {}; python3.withPackages (p: [p.requests p.cryptography p.pycryptodome p.xmltodict p.aiohttp p.paho-mqtt])'
  nix shell --impure --expr "$PYENV" -c python3 ...
  ```
  Append the component dir to `sys.path` (never insert at index 0); its
  `select.py` otherwise shadows the stdlib `select` module.

## 1. Decompile the app

```sh
mkdir -p /tmp/hc_apk && cd /tmp/hc_apk
for p in $(adb shell pm path com.connect.enduser | sed 's/package://'); do adb pull "$p"; done
nix shell nixpkgs#jadx -c jadx --no-res --no-imports -j 8 -d /tmp/hc_jadx /tmp/hc_apk/base.apk
```

Takes about 5 minutes and ends with "finished with errors"; that's normal.
Where to look:

- Retrofit interfaces: `sources/com/hikvision/hikconnect/sdk/pre/http/api/*Api.java`
  (`VideoIntercomApi`, `UserDeviceApi`, `EntranceGuardApi`, ...).
- Callers live in obfuscated `sources/defpackage/*.java`. `rg -l <ApiName>`
  and follow it. Intercom call ops are `sdl`/`tdl`/`vdl`/`wdl`/`xdl` in
  6.13.x; names change between versions.
- ISAPI paths the app uses:
  `grep -rhoE '/ISAPI/(VideoIntercom|AccessControl)[A-Za-z0-9/_]*' --include=*.java . | sort -u`.
  Use `grep -r`, not `rg -o` over the whole tree, which times out.
- Device capability flags: `getDeviceSupport().X()` resolves to
  `getInt(<n>)` or `getInt("<name>")` in `defpackage/g27.java` / `r27.java`.
  `<n>` is the key in the device list's `supportExt` JSON (e.g. `145` =
  `support_isapi`).

## 2. Call the API directly

`scripts/cloud_probe.py` reuses `HikConnectClient`:

```sh
python3 scripts/cloud_probe.py devices
python3 scripts/cloud_probe.py get '/v3/unifiedmsg/list?stype=27&serials=SERIAL&limit=5'
python3 scripts/cloud_probe.py isapi SERIAL '/ISAPI/VideoIntercom/callStatus?format=json'
```

- Stick to GETs unless the user agreed to a write. For a write, send it while
  idle and compare against a deliberately invalid body: the station answers
  `invalidOperation` for a valid command it can't run right now and
  `methodNotAllowed` for an unknown one.
- ISAPI errors come back as HTTP 200 with `statusCode` 4 in `data`.
- Keep request rates low; the cloud rate-limits per account.

## 3. Capture the live app with frida

The phone's server is frida **17.15.3**; nixpkgs' frida-tools is newer and
can't attach. Use a matching client in a venv:

```sh
nix shell nixpkgs#python3 -c python3 -m venv /tmp/hc_frida_venv
/tmp/hc_frida_venv/bin/pip install 'frida==17.15.3' frida-tools
adb root
adb shell 'pidof frida-server || (setsid /data/local/tmp/frida-server -D >/dev/null 2>&1 </dev/null &)'
```

Attach only to the app **while it's in the foreground**. Spawn (`-f`) and
attaching to a backgrounded (frozen) app both time out:

```sh
adb shell monkey -p com.connect.enduser -c android.intent.category.LAUNCHER 1
P=$(adb shell pidof com.connect.enduser | tr -d '\r')
/tmp/hc_frida_venv/bin/frida -U -p $P -l scripts/ssl_plaintext.js -q -t inf > /tmp/hc_frida/ssl.log 2>&1 &
```

`scripts/ssl_plaintext.js` hooks native `SSL_write`/`SSL_read` (the Java
bridge crashes on this ROM) and logs requests matching `/v3/`, `ISAPI`, etc.,
with `sessionId` headers redacted. Form bodies keep a `sessionId=` field, so
filter before sharing:

```sh
grep -E '^(GET|POST|PUT) |apiData=|transmissionData=' /tmp/hc_frida/ssl.log | sed -E 's/sessionId=[^&]+/sessionId=<r>/g'
```

Tap the feature in the app (or `adb shell input tap X Y`) and read the log.

## 4. Watch push and call state during a real ring

```sh
python3 scripts/push_monitor.py SERIAL
```

This registers its own push client on `pusheu.ezvizlife.com`;
`pusheu.hik-connect.com`, the host the cloud hands out, drops requests. After
each push it polls `callStatus`/`callerInfo` for 40 s. `callerInfo` only
answers while a call is active.

## Cleanup

Stop the frida and monitor processes by PID. Don't use `pkill -f` with a
pattern that also appears in your own shell's command line; it kills that
shell too.
