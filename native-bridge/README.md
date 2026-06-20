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

## Phase 1 — Capture (run once)

You can instrument either the **redroid** container from the main stack (rooted,
headless, already on your box) or a physical Android phone.

```bash
# 1. Start mitmproxy (web UI on :8081, proxy on :8080)
cd native-bridge/mitm
docker compose up -d
#   open it once so it generates the CA, then:

# 2. Point redroid at the proxy + install the CA + start frida-server
./setup-redroid-mitm.sh <redroid-ip>:5555 <unraid-ip> 8080

# 3a. Capture the CLOUD AUTH: spawn the app with cert-unpinning, then log in
./frida/run-frida.sh <redroid-ip>:5555 unpin
#   -> watch the mitmproxy console / captures/ for UID + AuthKey candidates

# 3b. Capture the STREAM PROTOCOL: with the camera live-viewing, attach the
#     IOCTL hook and note avClientStart2 args + the avSendIOCtrl start command
./frida/run-frida.sh <redroid-ip>:5555 ioctl
```

Optional sanity check it's really Kalay on your LAN:
```bash
python3 native-bridge/probe/kalay-probe.py <camera-ip>
```

The captures land in `mitm/captures/` with credential candidates highlighted.

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
