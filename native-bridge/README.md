# owlet-bridge (native) — the docker-wyze-bridge way for the Owlet Cam

Owlet Cam v1/v2 run on **ThroughTek / Kalay (TUTK)** — the same P2P platform as
the Wyze cams [docker-wyze-bridge](https://github.com/mrlt8/docker-wyze-bridge)
talks to. So this does what wyze-bridge does: **you enter your Owlet login in a
web UI, it connects to the camera over Kalay and serves RTSP/WebRTC/HLS.** No
emulator, no redroid, no screen-capture.

```
 Owlet account ──┐
                 ▼
   webapp.py  (control panel :8088)  ── Owlet cloud login (Firebase→Ayla)
        │                                └─ live diagnostic log + findings
        ▼  config
   tutk_client.py ──Kalay/TUTK P2P──▶ Owlet Cam ──H264──▶ go2rtc ──▶ RTSP / WebRTC / HLS
                                                            :8554      + video UI :1984
```

Everything is configurable in the browser: account credentials, region, camera
UID/AuthKey, AV settings. The **Connect & Diagnose** button runs the *real*
Owlet login and streams every step to a live log you can copy and share.

## What's solved vs. what we confirm from your logs

| Piece | Status |
|---|---|
| Owlet cloud login (Firebase → Ayla SSO → Ayla API) | **Implemented** (real keys/endpoints) |
| Stream-start command | **Standard** TUTK `IOTYPE_USER_IPCAM_START` (0x01FF) — not a guess |
| TUTK connect + frame loop → go2rtc | **Implemented** |
| *Which field carries the camera's Kalay UID / AuthKey* | **Confirmed from your diagnostic log** ← the one open item |

The Owlet **Sock** lives on Ayla; the **Cam** is Kalay. The diagnostic dumps
everything the account returns so we can pinpoint the camera's UID/AuthKey (it
may be in the device list, in device properties, or on a separate endpoint —
your log tells us which). Until then you can paste a UID/AuthKey straight into
the UI and stream immediately.

---

## Deploy on Unraid

**Prereq (one-time):** the proprietary TUTK `.so` libs aren't shipped — extract
them from the Owlet APK you own into the libs folder:

```bash
# on any machine with the apk + unzip
native-bridge/bridge/extract-libs.sh /path/to/owlet.apk x86_64
# -> copies libIOTCAPIs.so, libAVAPIs.so into native-bridge/bridge/libs/x86_64
# put that folder at  /mnt/user/appdata/owlet/libs  on Unraid
```

**Then — prebuilt image (terminal):**
```bash
docker run -d --name owlet-bridge --restart unless-stopped \
  -p 8088:8088 -p 1984:1984 -p 8554:8554 -p 8555:8555/tcp -p 8555:8555/udp \
  -v /mnt/user/appdata/owlet/config:/config \
  -v /mnt/user/appdata/owlet/libs:/app/libs:ro \
  ghcr.io/btoth525/owlet-bridge-native:latest
```

**Or the Unraid template:** Docker → Add Container → paste the
`unraid/owlet-bridge-native.xml` raw URL → set the libs + config paths → apply.

**Or compose / local build:**
```bash
cd native-bridge/bridge
docker compose up -d --build
```

---

## Use it

1. Open **`http://<unraid-ip>:8088`**.
2. Enter your Owlet **email / password**, pick **region**, click **Connect & Diagnose**.
3. Watch the live log. Candidates (UID/AuthKey/etc.) appear under **Findings** —
   click one to fill the field. If nothing auto-fills, **copy the whole log and
   send it to me** and we'll find the camera credential together.
4. Once UID (+ AuthKey for v2) is set, click **Save & (re)start stream**.
5. Video at **`http://<unraid-ip>:1984`**, and **`rtsp://<unraid-ip>:8554/owlet`**
   straight into Frigate.

---

## Files (`bridge/`)

| File | Role |
|---|---|
| `owlet_api.py` | Real Owlet login (Firebase→Ayla) + device dump + candidate hunter |
| `tutk_client.py` | Kalay/TUTK connect + standard start IOCTL → raw H.264 to stdout |
| `webapp.py` | Control-panel web UI (config, diagnose, logs, findings, status) |
| `templates/`, `static/` | The UI |
| `go2rtc.yaml` | Serves RTSP/WebRTC/HLS + video UI; supervises the TUTK client |
| `extract-libs.sh` | Pull TUTK `.so` from the Owlet APK |
| `Dockerfile`, `docker-compose.yml` | The container |

## `mitm/` and `probe/` — optional, only if the cloud doesn't hand over the AuthKey

If the diagnostic shows the camera's AuthKey isn't returned by the Owlet cloud
(newer DTLS-gated firmware), `mitm/` has a mitmproxy addon + Frida scripts to
read it from your **own phone's** app traffic once (no redroid needed), and
`probe/kalay-probe.py` confirms Kalay on UDP 63616. We only reach for these if
the logs say we must.

## Legal / ethical

Interoperability with **your own** camera, account, and traffic — same basis as
the Wyze/Owlet community tools. Don't redistribute Owlet's proprietary `.so`
libraries; each user extracts them from the app they installed. Don't point this
at devices or accounts that aren't yours.
