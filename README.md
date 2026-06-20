# Owlet-To-Rtsp

Bring an **app-only camera** (Owlet Cam) into Frigate by running the official
Android app in a headless emulator, capturing its **rendered display**, and
re-serving those pixels as **RTSP** for go2rtc / Frigate.

This is a *screen-capture bridge*. The app decrypts and displays the video
normally; we capture the output. No protocol or encryption work is involved.

> **Honest expectations:** this works, but it's a UI-capture bridge, not a
> native RTSP cam. Quality/latency are set by decode→render→recapture→encode —
> fine for monitoring + Frigate detection, not broadcast. The real effort is
> Step 0 (host kernel modules) and the watchdog (keeping the session alive
> unattended). If it ever feels like more upkeep than it's worth, a ~$30
> RTSP/ONVIF cam drops straight into the same go2rtc + Frigate config.

```
┌──────────────── Unraid host (NVIDIA GPU) ─────────────────┐
│                                                            │
│  owlet-redroid                 owlet-bridge                │
│  ┌────────────┐  adb/scrcpy  ┌──────────────────────────┐  │
│  │  Android + │ ───────────▶ │ scrcpy/screenrecord      │  │
│  │  Owlet app │   raw H264   │   → ffmpeg (NVENC) → RTSP │  │
│  └────────────┘              │   → go2rtc :8554         │  │
│        ▲                     │   + watchdog (adb taps)  │  │
│        └── adb taps ─────────┤                          │  │
│                              └────────────┬─────────────┘  │
│                                           ▼                │
│                                    Frigate (your existing) │
└────────────────────────────────────────────────────────────┘
```

You picked **NVIDIA**, so: redroid renders Android in software (`guest` GPU mode,
the most compatible), and your GPU is used for **NVENC** encoding in the bridge.

---

## What's in this repo

| Path | What |
|---|---|
| `bridge/` | The bridge container: `Dockerfile`, `entrypoint.sh`, `capture.sh`, `watchdog.sh`, `go2rtc.yaml` |
| `docker-compose.yml` | Full stack (redroid + bridge) for the Compose Manager plugin |
| `.env.example` | All tunables; copy to `.env` |
| `unraid/` | Unraid **Docker-tab templates** (`owlet-redroid.xml`, `owlet-bridge.xml`) |
| `frigate/owlet.camera.yml` | Camera block to merge into your Frigate config |
| `scripts/host-check.sh` | **Run first** — checks binder/ashmem + NVIDIA on the host |
| `.github/workflows/build-bridge.yml` | Builds & pushes the bridge image to GHCR |

Two ways to deploy — **docker-compose** (recommended) or **Unraid templates**.
Pick one; the steps below cover both.

---

## Step 0 — Host prerequisites (the gate)

redroid needs Android **binder/ashmem** kernel support on the *host*. If that's
missing, redroid won't boot and nothing else matters. Run the checker on the
Unraid host (terminal):

```bash
bash scripts/host-check.sh
```

- Modern Unraid kernels usually expose **binderfs** — you're good.
- If binder is missing, add to `/boot/config/go` and reboot:
  ```bash
  modprobe binder_linux devices="binder,hwbinder,vndbinder"
  modprobe ashmem_linux        # only if your kernel has it
  ```
- If the stock kernel lacks them entirely, you need a custom kernel (community
  **Unraid-Kernel-Helper**). **This is the one true blocker — sort it first.**

For **NVENC**, install the Unraid **Nvidia-Driver** plugin (Community Apps) and
reboot. `nvidia-smi` should list your GPU. (No NVIDIA? Set `ENCODER=libx264`.)

Create the shared docker network once:

```bash
docker network create owlet-net
```

---

## Deploy A — docker-compose (recommended)

Needs the **Compose Manager** plugin (Community Apps).

```bash
git clone https://github.com/btoth525/Owlet-To-Rtsp.git
cd Owlet-To-Rtsp
cp .env.example .env        # edit if you like; defaults are fine to start
docker compose up -d
```

The first run **builds** the bridge image locally. (Once the GitHub Action has
published to GHCR, compose will pull `ghcr.io/btoth525/owlet-bridge:latest`
instead — comment out the `build:` line if you prefer always-pull.)

Then jump to **Step 1 — one-time login**.

## Deploy B — Unraid Docker-tab templates

1. In Unraid → **Docker** → **Add Container**, paste the template URL for
   `owlet-redroid` (`unraid/owlet-redroid.xml` raw URL), set the appdata path,
   apply. Wait for it to boot.
2. Add the `owlet-bridge` template the same way. Confirm **Extra Parameters**
   contains `--runtime=nvidia` and set `NVIDIA_VISIBLE_DEVICES` (`all` or your
   GPU UUID from `nvidia-smi -L`). Apply.

Both containers must be on the **owlet-net** network so the bridge can reach
redroid as `owlet-redroid:5555` (already the template default).

---

## Step 1 — One-time install + login (interactive)

Do this **once** from your PC (any machine with `adb` + `scrcpy` installed):

```bash
adb connect <unraid-ip>:5555
adb devices                              # should list the redroid instance

adb -s <unraid-ip>:5555 install owlet.apk   # supply the APK (your phone or APKMirror)
```

Drive the UI once with a GUI:

```bash
scrcpy -s <unraid-ip>:5555
```

- Sign in with a **dedicated secondary Owlet account** — the app enforces a
  concurrent-session limit, and using the parent account here will fight with
  the phone app. Share the camera to the secondary account.
- Open the camera and **start the live view**.
- The login persists in the `/data` volume, so you only do this once.

Restart the bridge afterwards so it auto-launches the now-installed app:
`docker restart owlet-bridge`.

---

## Step 2 — Find your crop coordinates

By default the bridge streams the **whole screen**. To crop to just the video:

```bash
adb -s <unraid-ip>:5555 exec-out screencap -p > frame.png
```

Open `frame.png`, measure the video rectangle (width, height, top-left X, top-left Y),
and set `CROP=WIDTH:HEIGHT:X:Y` (e.g. `CROP=1280:720:0:140`). In compose put it
in `.env`; on the Docker tab set the `CROP` variable. Update Frigate's
`detect.width/height` to match the cropped size.

Set the **watchdog tap** (`TAP_X`, `TAP_Y`) to the center of that video region
so "tap to resume" lands on the video, not a button that navigates away.

---

## Step 3 — Verify the stream

```bash
# go2rtc web UI — click "owlet" to preview
http://<unraid-ip>:1984

# or probe directly
ffprobe -rtsp_transport tcp rtsp://<unraid-ip>:8554/owlet
```

You should see a live H264 video stream.

---

## Step 4 — Add to Frigate

Merge `frigate/owlet.camera.yml` into your Frigate `config.yml` (replace
`<unraid-ip>`), restart Frigate. The camera appears and records on motion. It
decodes with **NVDEC** (`hwaccel_args: preset-nvidia-h264`).

---

## Tunables (env vars)

| Var | Default | Notes |
|---|---|---|
| `ADB_DEVICE` | `redroid:5555` / `owlet-redroid:5555` | redroid host:port |
| `OWLET_PACKAGE` | `com.owletcare.owletcare` | verify: `adb shell pm list packages \| grep -i owlet` |
| `CAPTURE_METHOD` | `auto` | `auto` tries scrcpy then falls back to screenrecord |
| `ENCODER` | `auto` | `auto` → NVENC if present, else libx264 |
| `CROP` | *(empty)* | `W:H:X:Y`; empty = full screen |
| `FPS` / `BITRATE` | `15` / `4000000` | output stream |
| `SCREEN_SIZE` / `SCREEN_DENSITY` | `1280x720` / `240` | keep fixed for stable crop |
| `WATCHDOG_INTERVAL` | `120` | seconds; set below the app idle timeout |
| `TAP_X` / `TAP_Y` | `640` / `360` | watchdog tap = center of video |
| `WATCHDOG_APP_GUARD` | `true` | relaunch app if it loses focus |

---

## How capture/encoder selection works

- **Capture `auto`:** the bridge tries **scrcpy** (low latency, clean). If the
  scrcpy pipeline dies within 10s (missing binary, headless quirk), it drops a
  flag and uses **`adb screenrecord`** from then on — rock-solid and needs only
  android-tools. screenrecord caps at 180s per run, so there's a sub-second
  glitch every ~3 min (invisible to Frigate detection). Force either with
  `CAPTURE_METHOD=scrcpy` or `=screenrecord`.
- **Encoder `auto`:** uses `h264_nvenc` if ffmpeg reports it, else `libx264`.
  go2rtc supervises the whole pipeline and restarts it as needed.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| redroid won't boot | binder/ashmem missing on host | Step 0 — load modules / custom kernel |
| Feed freezes after N minutes | app idle timeout | lower `WATCHDOG_INTERVAL` below the timeout |
| Black frame, watchdog tapping | app logged out / dialog | `WATCHDOG_APP_GUARD=true` relaunches; re-check login |
| High latency | software encode / buffering | confirm NVENC (`ENCODER=auto`), `--runtime=nvidia` set |
| `nvenc` errors / no GPU | Nvidia-Driver plugin / runtime missing | install plugin, add `--runtime=nvidia`, or `ENCODER=libx264` |
| Glitch every ~3 min | screenrecord 180s cap | force `CAPTURE_METHOD=scrcpy` |
| Stream drops when parent opens app | concurrent-session limit | use the dedicated secondary account in the emulator |
| App crashes on launch | Android version / GPU mode | try a different redroid Android tag |
| Re-login needed each reboot | `/data` not persisted | check the appdata volume mount |
| Bridge can't reach redroid | not on same network | both on `owlet-net`; `ADB_DEVICE=owlet-redroid:5555` |

The bridge has a **healthcheck** that ffprobes its own RTSP; if no video is
produced it goes unhealthy (and restarts under compose).

---

## Validate before calling it done

1. redroid boots, `adb shell getprop sys.boot_completed` → `1`.
2. App stays logged in across a container restart.
3. `ffprobe rtsp://<ip>:8554/owlet` shows a live video stream.
4. Frigate shows the camera and records on motion.
5. **Leave it overnight** — the watchdog holds the feed past the idle timeout
   with no manual intervention. This is the real pass/fail.
6. Latency acceptable for your use (dashboard tile / detection).