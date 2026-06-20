# Native bridge — the wyze-bridge pattern for the Owlet Cam

Owlet Cam v1 and v2 run on **ThroughTek / Kalay (TUTK)** — the same P2P platform
as the Wyze cams that [docker-wyze-bridge](https://github.com/mrlt8/docker-wyze-bridge)
talks to (confirmed by Bitdefender's audit). So architecturally this is
**wyze-bridge pointed at an Owlet camera**: talk Kalay directly, pull the native
H.264, serve RTSP/WebRTC/HLS with a web UI. No emulator, no screen-capture, real
resolution and latency.

```
Owlet Cam ──Kalay/TUTK P2P──▶ owlet_tutk_client.py ──H264──▶ go2rtc ──▶ RTSP/WebRTC/HLS + Web UI
   ▲  (your account)              (ctypes over the                http://host:1984
   └─ owlet_auth.py (your login → UID + AuthKey)   app's own .so libs)
```

## The honest part: why there's a one-time setup

wyze-bridge is turnkey because someone did the per-camera reverse-engineering
**once** and bakes it in: how Wyze's cloud hands back the camera credentials, and
the exact `avSendIOCtrl` command that starts the video. **No such project exists
for Owlet yet** — so that one-time work has to happen a single time. After it's
done, this container is just as turnkey as wyze-bridge: set `OWLET_EMAIL` /
`OWLET_PASSWORD`, open the web UI, done.

You do **not** operate the capture forever. It's a ~30-minute, run-once step that
extracts **three Owlet-specific values**, which then get baked in:

| # | Value | Where it comes from |
|---|---|---|
| 1 | Cloud auth → **UID + AuthKey** | `mitm/` capture of your login + device-list calls |
| 2 | **AV account/password** for `avClientStart2` | `frida/hook-tutk-ioctl.js` dump of `avClientStart2` |
| 3 | **Stream-start IOCTL** (type + payload) | `frida/hook-tutk-ioctl.js` dump of `avSendIOCtrl` |

Everything else in here is generic TUTK and already written.

---

## Phase 1 — Capture (run once, on Unraid)

The whole capture toolchain is packaged as **one container, `owlet-capture`** —
mitmproxy (web UI) + adb + frida-tools + the scripts. Deploy it next to your
`owlet-redroid` container, run three commands in its console, done. You can
delete it afterwards.

### Deploy it

**Easiest — pull the prebuilt image (no PC, no build):**
```bash
docker network create owlet-net 2>/dev/null || true
docker run -d --name owlet-capture --network owlet-net \
  -p 8080:8080 -p 8081:8081 \
  -e MITM_IP=<unraid-ip> -e REDROID=owlet-redroid:5555 \
  -v /mnt/user/appdata/owlet/captures:/captures \
  -v /mnt/user/appdata/owlet/mitm-ca:/root/.mitmproxy \
  ghcr.io/btoth525/owlet-capture:latest
```

**Or via the Unraid template:** Docker → Add Container → paste the
`unraid/owlet-capture.xml` raw URL, set **MITM_IP** to your server IP, apply.

**Or build locally:**
```bash
docker build -f native-bridge/capture/Dockerfile -t owlet-capture native-bridge/
```

### Run the capture (open the container Console / `>_`)

```bash
# 1. Point redroid at the proxy, install the CA, start frida-server
/app/mitm/setup-redroid-mitm.sh owlet-redroid:5555 <unraid-ip> 8080

# 2a. CLOUD AUTH — spawn the app unpinned, then LOG IN in the Owlet app.
#     Watch the console + web UI (http://<unraid-ip>:8081) for UID/AuthKey.
/app/mitm/frida/run-frida.sh owlet-redroid:5555 unpin

# 2b. STREAM PROTOCOL — open the camera live view, then attach the hook and
#     note the avClientStart2 args + the avSendIOCtrl start command.
/app/mitm/frida/run-frida.sh owlet-redroid:5555 ioctl

# optional: confirm Kalay on the LAN
python3 /app/probe/kalay-probe.py <camera-ip>
```

Captures land in the mapped `captures/` folder with credential candidates
highlighted. **Send me that output and I'll wire in the three values.**

> Driving the Owlet app's UI: use `scrcpy -s <unraid-ip>:5555` from any PC to
> tap through login + open the live view while the Frida scripts are attached.

---

## Phase 2 — Bake in + run (turnkey from here)

```bash
# Pull the TUTK SDK libs out of the APK you already have
cd native-bridge/tutk
./extract-libs.sh /path/to/owlet.apk x86_64      # or arm64-v8a

# Put the 3 captured values into .env (or owlet_auth.py / owlet_tutk_client.py)
cat > .env <<'EOF'
OWLET_EMAIL=you@example.com
OWLET_PASSWORD=...
OWLET_AV_ACCOUNT=admin
OWLET_AV_PASSWORD=<from capture>
OWLET_IOTYPE_START=0x<from capture>
OWLET_START_PAYLOAD_HEX=<from capture>
EOF

docker network create owlet-net 2>/dev/null || true
docker compose up -d --build
```

Then it behaves exactly like wyze-bridge:

- **Web UI:** `http://<host>:1984` — stream tile + copy-paste RTSP/WebRTC/HLS links
- **RTSP:** `rtsp://<host>:8554/owlet` → drop straight into your existing Frigate

---

## Files

| Path | Role |
|---|---|
| `mitm/owlet_addon.py` | mitmproxy addon — extracts UID/AuthKey from your auth flow |
| `mitm/frida/ssl-unpinning.js` | makes the app accept the mitm CA (your traffic) |
| `mitm/frida/hook-tutk-ioctl.js` | dumps the live TUTK stream-start protocol |
| `mitm/setup-redroid-mitm.sh` | proxy + CA + frida-server onto the redroid container |
| `mitm/frida/run-frida.sh` | launch the app under either Frida script |
| `mitm/docker-compose.yml` | standalone mitmproxy + the addon |
| `probe/kalay-probe.py` | confirm Kalay on UDP 63616 |
| `tutk/extract-libs.sh` | pull `libIOTCAPIs.so` / `libAVAPIs.so` from the APK |
| `tutk/owlet_auth.py` | Owlet login → UID + AuthKey (the "just enter creds" layer) |
| `tutk/owlet_tutk_client.py` | TUTK IOTC/AV client → raw H.264 to stdout |
| `tutk/go2rtc.yaml` | serves RTSP/WebRTC/HLS + web UI |
| `tutk/Dockerfile` / `docker-compose.yml` | the turnkey container |

## Legal / ethical

This is interoperability with **your own** camera, account, and app traffic —
the same basis the Wyze/Owlet bridges and security researchers operate on. It
may run against Owlet's Terms of Service (a civil matter); don't redistribute
their proprietary `.so` libraries — each user extracts them from the app they
already installed. Don't point any of this at devices or accounts that aren't
yours.

## Reality check vs. the screen-capture bridge

- **If the capture yields a clean UID + AuthKey + start-IOCTL:** this is the
  better path by far — native quality, low latency, no emulator. Worth it.
- **If Owlet's newer firmware gates the AuthKey behind device-bound DTLS or
  rotates it server-side:** the native path gets much harder, and the
  screen-capture bridge in the repo root remains the reliable fallback.

Phase 1 is the cheap experiment that tells you which world you're in.
