# Screen-capture bridge (fallback) — `bridge/`

> This is the **fallback** path. The recommended one is the native Kalay bridge
> in [`native-bridge/`](../native-bridge/README.md) (no emulator, real H.264).
> Use this only if the native path can't get the camera credentials, or you want
> a method that works for *any* app-only camera.

Runs the official Owlet **app inside a headless Android (redroid)**, captures the
rendered display, and re-serves it as RTSP. The app decrypts + displays the video
normally; we capture the output. No protocol work — but it's a UI capture
(re-encoded pixels), so quality/latency are set by decode→render→recapture→encode.
Fine for monitoring + Frigate detection, not broadcast.

```
owlet-redroid                 owlet-bridge
┌────────────┐  adb/scrcpy  ┌──────────────────────────┐
│  Android + │ ───────────▶ │ scrcpy/screenrecord      │
│  Owlet app │   raw H264   │   → ffmpeg (NVENC) → RTSP │ → go2rtc :8554 → Frigate
└────────────┘              │   + watchdog (adb taps)  │
      ▲                     └──────────────────────────┘
      └── adb taps (keep live view awake) ──┘
```

You picked **NVIDIA**, so redroid renders Android in software (`guest` GPU mode,
most compatible) and the GPU does **NVENC** encoding in the bridge.

---

## Step 0 — Host prerequisites (the gate)

redroid needs Android **binder/ashmem** kernel support on the host. Run:

```bash
bash scripts/host-check.sh
```

- Modern Unraid kernels usually expose **binderfs** — you're good.
- If binder is missing, add to `/boot/config/go` and reboot:
  ```bash
  modprobe binder_linux devices="binder,hwbinder,vndbinder"
  modprobe ashmem_linux        # only if your kernel has it
  ```
- If the stock kernel lacks them, you need a custom kernel (community
  **Unraid-Kernel-Helper**). **The one true blocker — sort it first.**

For **NVENC**, install the Unraid **Nvidia-Driver** plugin and reboot. Then:

```bash
docker network create owlet-net
```

## Deploy

**docker-compose** (Compose Manager plugin) — uses the repo-root `docker-compose.yml`:
```bash
cp .env.example .env
docker compose up -d
```
**Or Unraid templates:** add `unraid/owlet-redroid.xml` then `unraid/owlet-bridge.xml`
(confirm `--runtime=nvidia` + `NVIDIA_VISIBLE_DEVICES`). Both on `owlet-net`.

## Step 1 — One-time install + login

From any PC with `adb` + `scrcpy`:
```bash
adb connect <unraid-ip>:5555
adb -s <unraid-ip>:5555 install owlet.apk
scrcpy -s <unraid-ip>:5555          # sign in (secondary account), open live view
docker restart owlet-bridge
```
Login persists in the `/data` volume. Use a **dedicated secondary account** to
avoid the concurrent-session limit fighting your phone.

## Step 2 — Crop

```bash
adb -s <unraid-ip>:5555 exec-out screencap -p > frame.png
```
Measure the video rectangle, set `CROP=W:H:X:Y`, and set the watchdog
`TAP_X/TAP_Y` to the center of that region.

## Step 3 — Verify & add to Frigate

```bash
ffprobe -rtsp_transport tcp rtsp://<unraid-ip>:8554/owlet   # or http://<ip>:1984
```
Merge `frigate/owlet.camera.yml` into your Frigate config (NVDEC decode).

---

## Tunables (env)

| Var | Default | Notes |
|---|---|---|
| `ADB_DEVICE` | `owlet-redroid:5555` | redroid host:port |
| `OWLET_PACKAGE` | `com.owletcare.owletcare` | `adb shell pm list packages \| grep -i owlet` |
| `CAPTURE_METHOD` | `auto` | scrcpy → screenrecord fallback |
| `ENCODER` | `auto` | NVENC if present, else libx264 |
| `CROP` | *(empty)* | `W:H:X:Y`; empty = full screen |
| `FPS` / `BITRATE` | `15` / `4000000` | output |
| `WATCHDOG_INTERVAL` | `120` | below the app idle timeout |
| `TAP_X` / `TAP_Y` | `640` / `360` | center of video |
| `WATCHDOG_APP_GUARD` | `true` | relaunch app if it loses focus |

## Troubleshooting

| Symptom | Fix |
|---|---|
| redroid won't boot | binder/ashmem — Step 0 |
| Feed freezes after N min | lower `WATCHDOG_INTERVAL` |
| Black frame, tapping | `WATCHDOG_APP_GUARD=true`; re-check login |
| High latency / nvenc error | confirm `--runtime=nvidia`, or `ENCODER=libx264` |
| Glitch every ~3 min | force `CAPTURE_METHOD=scrcpy` |
| Drops when parent opens app | use the secondary account in the emulator |

The bridge healthchecks its own RTSP and restarts if no video is produced.

## Validate

1. redroid boots (`getprop sys.boot_completed` = 1).
2. App stays logged in across a restart.
3. `ffprobe rtsp://<ip>:8554/owlet` shows video.
4. Frigate records on motion.
5. **Overnight test** — watchdog holds the feed past the idle timeout. The real
   pass/fail.
