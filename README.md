# Owlet-To-Rtsp

Get the **Owlet Cam** into Frigate / Home Assistant / any RTSP client.

Owlet Cam v1/v2 run on **ThroughTek / Kalay (TUTK)** — the same P2P platform as
the Wyze cams [docker-wyze-bridge](https://github.com/mrlt8/docker-wyze-bridge)
talks to. So the main path here is the wyze-bridge model: **enter your Owlet
login in a web UI, get RTSP/WebRTC/HLS out. No emulator.**

## Two paths

| | **Native Kalay bridge** ⭐ recommended | Screen-capture bridge (fallback) |
|---|---|---|
| Dir | [`native-bridge/`](native-bridge/README.md) | [`bridge/`](bridge/README.md) |
| How | Talks Kalay/TUTK directly to the camera | Runs the Owlet app in redroid, captures the display |
| Output | Native H.264 | Re-encoded pixels |
| Setup | Web UI: enter Owlet login | redroid + one-time app login + crop |
| Needs | TUTK `.so` libs from the APK | binder/ashmem kernel support, NVENC |
| Image | `ghcr.io/btoth525/owlet-bridge-native` | `ghcr.io/btoth525/owlet-bridge` |

**Start with the native bridge.** Drop to the screen-capture bridge only if the
camera credentials can't be obtained.

---

## Native bridge — quick start (Unraid)

**1. Prereq — extract the TUTK libs from the Owlet APK you own.** The container is
x86_64, so it needs the x86_64 libs. Put `owlet.apk` on the server, then:

```bash
mkdir -p /mnt/user/appdata/owlet/libs /mnt/user/appdata/owlet/config
unzip -l /mnt/user/appdata/owlet/owlet.apk | grep -iE 'lib/.*\.so'   # check archs
cd /mnt/user/appdata/owlet
unzip -o -j owlet.apk 'lib/x86_64/libIOTCAPIs.so' 'lib/x86_64/libAVAPIs.so' \
      'lib/x86_64/libTUTK*.so' 'lib/x86_64/libKalay*.so' -d libs/x86_64
```
> If the APK only ships `arm64-v8a` libs (no `x86_64/`), the container needs
> arm64 emulation instead — see [`native-bridge/README.md`](native-bridge/README.md).

**2. Deploy** (prebuilt image — make the GHCR package public first, or build locally):

```bash
docker run -d --name owlet-bridge --restart unless-stopped \
  -p 8088:8088 -p 1984:1984 -p 8554:8554 -p 8555:8555/tcp -p 8555:8555/udp \
  -v /mnt/user/appdata/owlet/config:/config \
  -v /mnt/user/appdata/owlet/libs:/app/libs:ro \
  ghcr.io/btoth525/owlet-bridge-native:latest
```

**3. Use** — open `http://<unraid-ip>:8088`, enter your Owlet login, click
**Connect & Diagnose**. Set the camera UID/AuthKey (auto-filled from the log or
pasted), **Save & restart stream**, then:
- video UI: `http://<unraid-ip>:1984`
- RTSP for Frigate: `rtsp://<unraid-ip>:8554/owlet`

> The libs are only needed for actual video — you can deploy + run the login
> **diagnostic** right away without them.

Full detail, the credential-discovery flow, and the arm64 case:
[`native-bridge/README.md`](native-bridge/README.md).

---

## Repo layout

| Path | What |
|---|---|
| `native-bridge/bridge/` | ⭐ The native Owlet→RTSP container (web UI, TUTK client, go2rtc) |
| `native-bridge/mitm/`, `probe/` | Optional phone-based AuthKey discovery, Kalay LAN probe |
| `bridge/` | Screen-capture bridge (redroid + scrcpy/ffmpeg + watchdog) |
| `docker-compose.yml`, `.env.example` | Screen-capture full stack (redroid + bridge) |
| `frigate/owlet.camera.yml` | Camera block for your Frigate config |
| `scripts/host-check.sh` | Screen-capture host gate (binder/ashmem + NVIDIA) |
| `unraid/` | Unraid templates: `owlet-bridge-native.xml`, `owlet-bridge.xml`, `owlet-redroid.xml` |
| `.github/workflows/build-bridge.yml` | Builds both images to GHCR |

### Prebuilt images (GHCR)

```
ghcr.io/btoth525/owlet-bridge-native:latest   # native Kalay bridge (recommended)
ghcr.io/btoth525/owlet-bridge:latest          # screen-capture bridge
```
After the build Action runs, set the package visibility to **Public** (GitHub →
repo → Packages → settings) so Unraid can pull without a login.

---

## Legal / ethical

Interoperability with **your own** camera, account, and app traffic — the same
basis the Wyze/Owlet community tools and security researchers operate on. Don't
redistribute Owlet's proprietary `.so` libraries; each user extracts them from
the app they installed. Don't point any of this at devices or accounts that
aren't yours.
