<div align="center">

<img src="assets/logo.png" width="140" alt="Owlet-To-RTSP logo">

# Owlet‑To‑RTSP

**Turn an Owlet Cam into a plain RTSP stream for Frigate, Home Assistant, or any RTSP client — running entirely on your own server.**

No phone. No Android emulator. No cloud relay. It talks to the camera directly over ThroughTek/Kalay (TUTK), the [docker‑wyze‑bridge](https://github.com/mrlt8/docker-wyze-bridge) way.

![image](https://img.shields.io/badge/ghcr.io-owlet--bridge--native-2496ED?logo=docker&logoColor=white)
![platform](https://img.shields.io/badge/runs%20on-Unraid%20%C2%B7%20x86__64-39d98a)
![output](https://img.shields.io/badge/output-RTSP%20%C2%B7%20WebRTC%20%C2%B7%20HLS-3fa7ff)
![status](https://img.shields.io/badge/status-working-39d98a)

</div>

---

> **Status: working & verified.** Tested end‑to‑end on an **Owlet Dream Duo 3.0** → Unraid (x86‑64) → **Frigate**: live H.264 pulled straight off the camera over Kalay, repackaged to RTSP locally, recording 24/7. As far as we know, this is the **first working local Owlet → RTSP bridge.**

## What you get

- 🦉 **A single container** — drop in your Owlet login, get `rtsp://<host>:8554/owlet`.
- 🌐 **A friendly web UI** — status dashboard, in‑browser APK upload, one‑click "Connect & Diagnose," live logs.
- 🏠 **100% local video** — once connected, frames go camera → bridge directly over your LAN; they never touch Owlet's or ThroughTek's servers.
- 🔁 **Self‑healing** — keeps the camera session alive 24/7 and reconnects automatically after a Frigate restart or a power cut.
- 🚫 **Nothing proprietary shipped** — you supply your own copy of the Owlet app; the bridge extracts the TUTK libraries from it for you.

---

## How it works

The Owlet Cam speaks **ThroughTek / Kalay (TUTK)** — the same P2P video platform the Wyze cameras use. This bridge talks that protocol directly:

```
 Owlet account (email / password / camera DSN)
        │
        ▼
  webapp.py ── control panel :8088
        │   • Owlet cloud login   (Firebase → Ayla SSO → Ayla)
        │   • camera‑key fetch     (camera‑kms.owletdata.com → UID / AuthKey / password)
        ▼  /config/owlet.env
  tutk_client.py ── Kalay/TUTK P2P, DTLS ──▶ Owlet Cam
        │   • licenses + inits the TUTK SDK   (region US, app license key)
        │   • IOTC_Connect_ByUIDEx            (authKey)
        │   • avClientStartEx                 (DTLS AV login)
        │   • avRecvFrameData2 loop ─ raw H.264 → stdout
        ▼
  ffmpeg + go2rtc ──▶  RTSP :8554  ·  WebRTC :8555  ·  HLS / preview UI :1984
```

Everything is configured in the browser. The **Connect & Diagnose** button runs the *real* Owlet login, fetches the camera's Kalay credentials, and streams every step to a live log.

---

## Quick start (Unraid)

> ⚠️ **Networking matters.** Use **Bridge** networking — *not* a `br0`/macvlan custom network. On macvlan the bridge gets its own LAN IP that the Unraid host (and host/bridge‑networked containers like Frigate) **cannot reach**, so Frigate can't pull the stream. Bridge networking with published ports works for everything (the camera dials *outbound*, so P2P is fine — same as docker‑wyze‑bridge).

### 1 · Add the container

Docker → **Add Container** → paste the template URL:

```
https://raw.githubusercontent.com/btoth525/Owlet-To-Rtsp/main/unraid/owlet-bridge-native.xml
```

It comes pre‑configured: **Bridge** networking, ports that **coexist with Frigate's go2rtc** (which owns `1984/8554/8555`):

| Service | Host port | Container port |
|---|---|---|
| Control panel | `8088` | `8088` |
| go2rtc video UI | `1985` | `1984` |
| **RTSP** | **`18554`** | `8554` |
| WebRTC | `18555` | `8555` |

and two volumes: **`/config`** (settings) and **`/app/libs`** (the TUTK libraries).

### 2 · Make `/config` writable

The container runs as a non‑root (Termux) user, so the Unraid appdata folder must be writable by it:

```bash
chmod -R 777 /mnt/user/appdata/owlet/config
```

### 3 · Provide the TUTK libraries (in the browser)

The proprietary ThroughTek `.so` libraries aren't shipped — you supply your own copy of the Owlet app. Open the control panel at **`http://<unraid-ip>:8088`**, go to the **🧩 TUTK libraries** card, click **Upload & extract**, and pick your Owlet **`.apkm`** (or `.apk`). The bridge pulls the five x86‑64 libraries out of it for you.

> These libraries are Android binaries linked against Bionic libc, so glibc can't load them. The image runs a **Termux (Bionic) userspace** so they load on a normal x86‑64 Linux kernel — no Android, no `binder`/`ashmem`, no privileged mode.

### 4 · Sign in

In the control panel:
1. Enter your Owlet **email / password** and **region**.
2. Enter your camera **DSN** — it looks like **`OCD…`** (that's a **capital letter O**, not a zero). Find it in the Owlet app under the camera, or on the camera base.
3. Click **Connect & Diagnose** → it logs in and auto‑fills the camera UID / AuthKey / password.
4. Click **Save & (re)start stream.**

### 5 · Use it

- Control panel: `http://<unraid-ip>:8088`
- Video UI / preview: `http://<unraid-ip>:1985`
- **RTSP: `rtsp://<unraid-ip>:18554/owlet`** ← point Frigate here.

---

## Feed it into Frigate

Add the bridge as a go2rtc stream, then a record‑only camera. **Disable birdseye** for it — Frigate's birdseye decodes the full 1440p feed continuously and will spike your CPU otherwise:

```yaml
go2rtc:
  streams:
    Owlet_Cam:
      - rtsp://<unraid-ip>:18554/owlet      # the bridge

cameras:
  Owlet_Cam:
    enabled: true
    birdseye:
      enabled: false                        # don't decode 1440p just for the mosaic
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/Owlet_Cam
          input_args: preset-rtsp-restream
          roles: [record]
    detect:
      enabled: false
    record:
      enabled: true
```

> The Owlet stream is **video‑only** (no audio), so don't add an `audio` role. The first connect takes ~10s (P2P + DTLS), so Frigate's ffmpeg may retry once — that's normal.

---

## Run it without Unraid

**docker run:**
```bash
docker run -d --name owlet-bridge --restart unless-stopped \
  -p 8088:8088 -p 1985:1984 -p 18554:8554 -p 18555:8555/tcp -p 18555:8555/udp \
  -e PUBLIC_HTTP_PORT=1985 -e PUBLIC_RTSP_PORT=18554 -e PUBLIC_WEBRTC_PORT=18555 \
  -v /path/to/owlet/config:/config \
  -v /path/to/owlet/libs:/app/libs \
  ghcr.io/btoth525/owlet-bridge-native:latest
```

**docker compose:** see [`native-bridge/bridge/docker-compose.yml`](native-bridge/bridge/docker-compose.yml).

The `PUBLIC_*` vars just tell the UI which host ports you mapped, so the copy‑URLs and the "Open video UI" button point to the right place.

---

## How we built this — the findings

Owlet ships no public API or RTSP. Getting here meant reverse‑engineering the Owlet Android app and the ThroughTek libraries it bundles. The notable pieces:

### 1 · The camera is Kalay, the sock is Ayla
Owlet's **Smart Sock** lives on the Ayla IoT cloud; the **Cam** is a ThroughTek Kalay device that isn't in the Ayla device list at all. The login chain that yields the camera's P2P credentials:

| Step | Endpoint | Returns |
|---|---|---|
| Firebase `verifyPassword` | `identitytoolkit` (per‑region key, with `X‑Android‑Package`/`X‑Android‑Cert`) | Firebase `idToken` |
| Ayla SSO mini token | `ayla‑sso.owletdata.com/mini/` | mini token |
| Ayla `token_sign_in` | `…aylanetworks.com/api/v1/token_sign_in` (`provider: owl_id`) | Ayla token |
| **Camera key (KMS)** | **`camera‑kms.owletdata.com/kms/{DSN}`** (Firebase `idToken`) | **`tutkid` (UID), `authKey`, `password`** |

### 2 · Running Android `.so` libraries on x86‑64 without Android
ThroughTek's `.so` files are linked to Android's **Bionic** libc, so glibc refuses them. Instead of emulating Android, the image is built `FROM termux/termux-docker:x86_64` — a Bionic userspace — so the libraries `dlopen` cleanly on the normal Unraid kernel.

### 3 · The exact TUTK call sequence (recovered by decompiling the app)
The newer Kalay SDK is **licensed** and **region‑locked** and hangs forever if you skip either. Decompiling `com.owlet.tutk.AndroidTutkSdk` gave the precise order:

```
TUTK_SDK_Set_License_Key(<key baked into the app>)
IOTC_Set_LanSearchPort(63616) ; IOTC_Setup_Session_Alive_Timeout(20)
TUTK_SDK_Set_Region(3)                  # REGION_US
IOTC_Initialize2(0) ; avInitialize(512)
IOTC_Connect_ByUIDEx(uid, sid, &St_IOTCConnectInput)
avClientStartEx(&St_AVClientStartInConfig, &out)     # DTLS AV login
avSendIOCtrl(511 = IOTYPE_USER_IPCAM_START, …)
loop avRecvFrameData2() → H.264
```

### 4 · The struct ABI — read straight out of the libraries
The connect/AV‑login calls take C structs whose layout jadx can't recover (it alphabetises Java fields). So the offsets were **disassembled directly** from the Owlet `libIOTCAPIs.so` / `libAVAPIs.so`. Two details made or broke the whole thing:

- **`St_IOTCConnectInput` (160 bytes):** the real `IOTC_Connect_ByUIDEx` first does `cmp [struct], 0xA0` — the struct **must begin with its own size, `160`**, or it returns `-46` instantly. The AuthKey is exactly **8 chars** at offset `0x08`.
- **`St_AVClientStartInConfig` (64 bytes):** same trick — a leading `structSize` of `64`, with account/password as pointers.

### 5 · The camera demands DTLS at the AV layer
The simple `avClientStart2` login is rejected with `‑20049` (a DTLS‑class error). The Dream Duo requires the encrypted **`avClientStartEx`** login the app uses — security mode **Auto** negotiates DTLS and connects on the first try.

---

## Configuration reference

Everything's set in the web UI; these env vars override or tune it:

| Var | Default | Meaning |
|---|---|---|
| `PUBLIC_HTTP_PORT` / `PUBLIC_RTSP_PORT` / `PUBLIC_WEBRTC_PORT` | `1984` / `8554` / `8555` | the **host** ports you mapped, so the UI shows reachable URLs |
| `OWLET_KEEPALIVE` | `1` | keep one camera session warm 24/7 (auto‑reconnect). **Leave on.** |
| `OWLET_AV_SECURITY_MODE` | *(auto)* | pin `0` Simple / `1` Dtls / `2` Auto |
| `OWLET_REGION_CODE` | `3` | TUTK region (US = 3) |
| `OWLET_IOTYPE_START` | `511` | start‑video IOCTL |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| **KMS `403 Forbidden`** on diagnose | DSN is wrong — it starts with the **letter `O`** (`OCD…`), not a zero. |
| **`PermissionError` / settings won't save** | `chmod -R 777 /mnt/user/appdata/owlet/config`, restart. |
| **Frigate can't reach the bridge** (`connection refused` / `no route`) | The bridge is on `br0`/macvlan — the host can't reach it. Switch to **Bridge** networking with the offset ports. |
| **Port conflict with Frigate** | Frigate owns `1984/8554/8555`; the template already offsets the bridge to `1985/18554/18555`. |
| **"Open video UI" / copy buttons don't work** | Set the `PUBLIC_*` env vars and pull `:latest` (clipboard is blocked on plain http — the new UI falls back). |
| **Stream pops in and out** | Close the **Owlet app on all phones** — the cam allows one session and they fight. Keep `OWLET_KEEPALIVE` on. Check `docker exec owlet-bridge-native tail -50 /config/tutk.log`. |
| **High CPU in Frigate** | Disable **birdseye** for the Owlet camera — it was decoding the full 1440p feed for the mosaic. |
| **`MISSING …libIOTCAPIs.so`** | Upload your Owlet APK in the **TUTK libraries** card (or drop it in the config folder). |

---

## FAQ

**Is the video local?** Yes — once connected, frames are peer‑to‑peer over your LAN and never leave your network. Only the one‑time login + camera‑key fetch use Owlet's cloud, and those creds are cached.

**Can an Owlet app update break it?** Not by itself — the bridge uses cached creds + the libraries you extracted, not the live app. Server‑side auth changes or camera firmware *could* eventually require re‑extracting from a newer APK (same maintenance model as docker‑wyze‑bridge). Keep a copy of your working `.apkm`.

**Does it survive a reboot / power loss?** Yes — `--restart unless-stopped` brings the container back and the keepalive reconnects the camera automatically.

---

## Repo layout

| Path | What |
|---|---|
| [`native-bridge/bridge/`](native-bridge/bridge) | The container: `webapp.py`, `owlet_api.py`, `tutk_client.py`, `lib_extract.py`, `go2rtc.yaml`, `Dockerfile.bionic`, web UI |
| `unraid/owlet-bridge-native.xml` | Unraid template |
| `frigate/owlet.camera.yml` | A camera block for your Frigate config |
| `.github/workflows/build-bridge.yml` | Builds the image to GHCR |

---

## Legal / ethical

This is interoperability with **your own** camera, account, and app — the same basis the Wyze/Owlet community tools and security researchers work on. The proprietary ThroughTek `.so` libraries are **not** redistributed; each user extracts them from the app they installed (the repo `.gitignore` keeps them out). Don't point any of this at devices or accounts that aren't yours.

## Acknowledgments

Built on [go2rtc](https://github.com/AlexxIT/go2rtc), inspired by [docker‑wyze‑bridge](https://github.com/mrlt8/docker-wyze-bridge), and made possible by ThroughTek's Kalay SDK shipped inside the Owlet app.
