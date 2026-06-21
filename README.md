# Owlet‑To‑RTSP

Turn an **Owlet Cam** (Dream Duo / Cam v1 / v2) into a plain **RTSP** stream for
**Frigate**, Home Assistant, or any RTSP client — running entirely as a single
container on your own server. No phone, no Android emulator, no cloud relay.

It works the way [docker‑wyze‑bridge](https://github.com/mrlt8/docker-wyze-bridge)
does: open a web UI, type in your Owlet email + password, and out comes
`rtsp://<host>:8554/owlet`.

> **Status: working.** Verified end‑to‑end against an **Owlet Dream Duo 3.0** on
> an Unraid x86‑64 box — live H.264 pulled straight off the camera over Kalay,
> repackaged to RTSP locally.

---

## How it works

The Owlet Cam speaks **ThroughTek / Kalay (TUTK)** — the same P2P video platform
the Wyze cameras use. This bridge talks that protocol directly:

```
 Owlet account (email/password)
        │
        ▼
  webapp.py ── control panel :8088
        │   • Owlet cloud login  (Firebase → Ayla SSO → Ayla)
        │   • camera‑key fetch   (camera‑kms.owletdata.com → UID / AuthKey / password)
        ▼  writes /config/owlet.env
  tutk_client.py ── Kalay/TUTK P2P, DTLS  ──▶  Owlet Cam
        │   • licenses + inits the TUTK SDK
        │   • IOTC_Connect_ByUIDEx  (authKey)
        │   • avClientStartEx       (DTLS AV login)
        │   • avRecvFrameData2 loop ─ raw H.264 → stdout
        ▼
  ffmpeg + go2rtc ──▶  RTSP :8554  ·  WebRTC :8555  ·  HLS / preview UI :1984
```

Everything is configurable in the browser. The **Connect & Diagnose** button runs
the *real* Owlet login, fetches the camera's Kalay credentials, and streams every
step to a live log.

---

## Quick start (Unraid)

### 1 · Provide the TUTK libraries (one time)

The proprietary ThroughTek `.so` libraries are **not** shipped — each user pulls
them from the Owlet app they downloaded. You don't have to do this by hand:

**Easiest — upload in the web UI.** Start the container (step 2), open the control
panel, and in the **🧩 TUTK libraries** card click **Upload & extract** and pick
your Owlet `.apkm` (or `.apk`). The bridge pulls the five x86‑64 libraries out of
it for you. (It also auto‑extracts any APK it finds in the mounted config folder
on startup.)

**Or by hand**, extract them into the libs folder:

```bash
mkdir -p /mnt/user/appdata/owlet/libs/x86_64 /mnt/user/appdata/owlet/config
unzip -o -j /path/to/split_config.x86_64.apk \
  'lib/x86_64/libIOTCAPIs.so' 'lib/x86_64/libAVAPIs.so' \
  'lib/x86_64/libTUTKGlobalAPIs.so' 'lib/x86_64/libP2PTunnelAPIs.so' \
  'lib/x86_64/libRDTAPIs.so' -d /mnt/user/appdata/owlet/libs/x86_64
```

> These libraries are Android binaries linked against Bionic libc. The container
> runs a **Termux (Bionic) userspace** so they load on a normal x86‑64 Linux
> kernel — no Android, no `binder`/`ashmem`, no privileged mode required.

### 2 · Run the container

```bash
docker run -d --name owlet-bridge --restart unless-stopped \
  -p 8088:8088 -p 1984:1984 -p 8554:8554 -p 8555:8555/tcp -p 8555:8555/udp \
  -v /mnt/user/appdata/owlet/config:/config \
  -v /mnt/user/appdata/owlet/libs:/app/libs:ro \
  ghcr.io/btoth525/owlet-bridge-native:latest
```

Or use the Unraid template at [`unraid/owlet-bridge-native.xml`](unraid/owlet-bridge-native.xml)
(Docker → Add Container → paste the raw URL → set the **config** and **libs**
paths → Apply).

> Already running Frigate (or anything) on host port **8554**? Map the bridge's
> RTSP somewhere else, e.g. `-p 18554:8554`, and point Frigate at
> `rtsp://<host>:18554/owlet`.

### 3 · Configure in the browser

1. Open **`http://<host>:8088`**.
2. Enter your Owlet **email / password**, pick your **region**, and put your
   camera's **DSN** (e.g. `OCD…`) in the *Camera DSN* field.
3. Click **Connect & Diagnose**. The bridge logs in, calls the Owlet camera‑key
   service, and auto‑fills the camera **UID**, **AuthKey**, and **AV password**.
4. Click **Save & (re)start stream**.

The TUTK license key and US region are pre‑filled — you don't have to find them.

### 4 · Use it

- Preview + stream links: **`http://<host>:1984`**
- **RTSP for Frigate: `rtsp://<host>:8554/owlet`**

Drop that URL into a Frigate camera (`ffmpeg` input or, cleaner, Frigate's own
`go2rtc` block).

---

## How we got here — the findings

Owlet ships no public API or RTSP. Getting here meant reverse‑engineering the
Owlet Android app and the ThroughTek libraries it bundles. The notable pieces:

### 1 · The camera is Kalay, the sock is Ayla

Owlet's **Smart Sock** lives on the Ayla IoT cloud. The **Cam** is a ThroughTek
Kalay device. The login chain that yields the camera's P2P credentials:

| Step | Endpoint | Returns |
|---|---|---|
| Firebase `verifyPassword` | `identitytoolkit` (per‑region API key, with `X‑Android‑Package`/`X‑Android‑Cert`) | Firebase `idToken` |
| Ayla SSO mini token | `ayla‑sso.owletdata.com/mini/` (raw `Authorization: <jwt>`) | mini token |
| Ayla `token_sign_in` | `…aylanetworks.com/api/v1/token_sign_in` (`provider: owl_id`) | Ayla access token |
| **Camera key (KMS)** | **`camera‑kms.owletdata.com/kms/{DSN}`** (Firebase `idToken`) | **`tutkid` (UID), `authKey`, `password`** |

`owlet_api.py` implements all of it; the KMS call is what hands over the camera's
Kalay UID + AuthKey + AV password, keyed only by the camera DSN.

### 2 · Running Android `.so` libraries on x86‑64 without Android

ThroughTek's `.so` files are linked to Android's **Bionic** libc (`needs LIBC`),
so glibc refuses to load them. Rather than emulate Android, the image is built
`FROM termux/termux-docker:x86_64` — a **Bionic userspace** — so the libraries
`dlopen` cleanly on the normal Unraid kernel. The five we load:
`libTUTKGlobalAPIs`, `libP2PTunnelAPIs`, `libRDTAPIs`, `libIOTCAPIs`, `libAVAPIs`.

### 3 · The exact TUTK call sequence (recovered by decompiling the app)

The newer Kalay SDK is **licensed** and **region‑locked** and hangs forever if
you skip either. Decompiling `com.owlet.tutk.AndroidTutkSdk` gave the precise
order, which `tutk_client.py` reproduces:

```
TUTK_SDK_Set_License_Key(<key baked into the app>)
IOTC_Set_LanSearchPort(63616)
IOTC_Setup_Session_Alive_Timeout(20)
TUTK_SDK_Set_Region(3)             # REGION_US
IOTC_Initialize2(0) ; avInitialize(512)
IOTC_Connect_ByUIDEx(uid, sid, &St_IOTCConnectInput)
avClientStartEx(&St_AVClientStartInConfig, &out)     # DTLS AV login
avSendIOCtrl(511 = IOTYPE_USER_IPCAM_START, …)
loop avRecvFrameData2() → H.264
```

The TUTK **license key** and **region (US = 3)** were recovered from the app and
are baked into the bridge, so there's nothing for you to find.

### 4 · The struct ABI — read straight out of the libraries

The connect/AV‑login calls take C structs whose layout jadx can't recover (it
alphabetises Java fields). So the offsets were disassembled directly from the
Owlet `libIOTCAPIs.so` / `libAVAPIs.so` and matched byte‑for‑byte in ctypes. Two
details made or broke the whole thing:

**`St_IOTCConnectInput` (160 bytes).** The very first thing the real
`IOTC_Connect_ByUIDEx` does is `cmp [struct], 0xA0` — the struct **must** begin
with its own size, `160`, or it returns `-46` instantly. The AuthKey is exactly
8 chars at offset `0x08`.

| off | field | | off | field |
|----|----|---|----|----|
| 0x00 | `structSize` = 160 | | 0x94 | `timeout` |
| 0x04 | `authenticationType` = 0 | | 0x98 | `dataTransmitMode` |
| 0x08 | `authKey[8]` | | 0x9c | `lanModeDisable` |
| 0x10 | `deviceRegion[132]` | | 0x9d | `p2pModeDisable` |

**`St_AVClientStartInConfig` (64 bytes).** Same trick — a leading `structSize`
of `64`, with the account/password as pointers:

| off | field | | off | field |
|----|----|---|----|----|
| 0x00 | `structSize` = 64 | | 0x24 | `security_mode` |
| 0x04 | `iotc_session_id` | | 0x28 | `auth_type` |
| 0x08 | `iotc_channel_id` (byte) | | 0x2c | `sync_recv_data` |
| 0x0c | `timeout_sec` | | 0x30 | `dtls_cipher_suites*` |
| 0x10 | `account_or_identity*` | | | |
| 0x18 | `password_or_token*` | 0x20 | `resend` | |

### 5 · The camera demands DTLS at the AV layer

The simple `avClientStart2` login is rejected with `‑20049` — an undocumented
error sitting right in the SDK's DTLS cluster (`‑20039 … ‑20041`). The Dream Duo
requires the encrypted **`avClientStartEx`** login the app uses. The bridge calls
it with security mode **Auto** (which negotiates DTLS) and falls back to `Dtls` /
`Simple` across reconnects if needed. `Auto` connects on the first try.

After that it's standard Kalay IP‑cam: send `IOTYPE_USER_IPCAM_START` (`0x01FF` /
`511`) and pull frames with `avRecvFrameData2`.

---

## Configuration reference

Settings live in the web UI and persist to `/config/owlet.yaml` (+ a mirrored
`/config/owlet.env` the streamer reads). Useful overrides (env vars):

| Var | Default | Meaning |
|---|---|---|
| `OWLET_EMAIL` / `OWLET_PASSWORD` | — | Owlet account login |
| `OWLET_REGION` | `world` | `world` or `europe` |
| `OWLET_CAMERA_DSN` | — | camera DSN; auto‑fetches UID/AuthKey/password from KMS |
| `OWLET_UID` / `OWLET_AUTHKEY` | — | camera Kalay UID + 8‑char AuthKey (auto‑filled) |
| `OWLET_AV_ACCOUNT` / `OWLET_AV_PASSWORD` | `admin` / — | AV‑layer login |
| `OWLET_AV_SECURITY_MODE` | *(auto)* | pin one of `0` Simple / `1` Dtls / `2` Auto |
| `OWLET_REGION_CODE` | `3` | TUTK region (US = 3) |
| `OWLET_LICENSE_KEY` | *(app key)* | TUTK SDK license key |
| `OWLET_IOTYPE_START` | `511` | start‑video IOCTL |

---

## Troubleshooting

Run the streamer by hand to see every step:

```bash
docker exec owlet-bridge bash -lc \
  'set -a; . /config/owlet.env; timeout 45 python3 /app/tutk_client.py >/dev/null'
```

| Symptom | Meaning / fix |
|---|---|
| `MISSING …libIOTCAPIs.so` | Libraries not mounted — see step 1; they go in `…/libs/x86_64/`. |
| `IOTC_Connect_ByUIDEx -> -46` | Struct/version mismatch — you're on old code; pull `:latest`. |
| `IOTC_Connect_ByUIDEx -> -68/-71` | Wrong/missing AuthKey — re‑run **Connect & Diagnose**. |
| `avClientStartEx … -> -20049` (all modes) | AV account/password wrong — re‑fetch via diagnose. |
| `IOTC_Connect_ByUIDEx -> 0` then frames | Working. |

---

## Repo layout

| Path | What |
|---|---|
| [`native-bridge/bridge/`](native-bridge/bridge) | The bridge container: `webapp.py`, `owlet_api.py`, `tutk_client.py`, `go2rtc.yaml`, `Dockerfile.bionic`, web UI |
| `native-bridge/probe/` | Optional Kalay LAN probe (UDP 63616) |
| `frigate/owlet.camera.yml` | A camera block for your Frigate config |
| `unraid/owlet-bridge-native.xml` | Unraid template |
| `.github/workflows/build-bridge.yml` | Builds the image to GHCR |

### Prebuilt image (GHCR)

```
ghcr.io/btoth525/owlet-bridge-native:latest
```

After the build Action runs, set the package to **Public** (GitHub → repo →
Packages → settings) so Unraid can pull it without a login.

---

## Legal / ethical

This is interoperability with **your own** camera, account, and app — the same
basis the Wyze/Owlet community tools and security researchers work on. The
proprietary ThroughTek `.so` libraries are **not** redistributed; each user
extracts them from the app they installed (the repo `.gitignore` keeps them out).
Don't point any of this at devices or accounts that aren't yours.
