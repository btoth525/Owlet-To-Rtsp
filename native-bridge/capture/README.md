# owlet-capture — one-time AuthKey grab (throwaway redroid)

The Dream Duo's camera credential (**Kalay UID + AuthKey**) only exists in the
Owlet app's own traffic. This stack runs the app once in a rooted Android
container, defeats cert-pinning with Frida, and captures the credential through
mitmproxy. Then you tear it down — the actual bridge never uses redroid.

> **Honest risk:** the newest Owlet Dream app may detect the emulator (Google
> Play Integrity) and refuse to log in, or require Google Play Services that
> stock redroid doesn't have. If that happens, see *If the app won't run* below.

## Run it

```bash
# on Unraid (needs the Compose Manager plugin, or run from a shell)
cd native-bridge/capture
MITM_IP=<your-unraid-ip> docker compose up -d --build
docker logs -f owlet-capture        # watch it auto-configure redroid, prints the runbook
```

This brings up **redroid** (Android) + **owlet-capture** (mitmproxy + adb +
frida-tools). The capture container waits for redroid to boot, then installs the
mitmproxy CA, sets the proxy, and starts frida-server automatically.

## Capture the AuthKey

**1. Install + drive the app** (from any PC with `adb` + `scrcpy`):
```bash
adb connect <unraid-ip>:5555
adb -s <unraid-ip>:5555 install owlet.apk
scrcpy -s <unraid-ip>:5555          # a window opens showing Android
```

**2. Start the cert-unpinning capture** — in the **owlet-capture container console**
(Unraid: the container's `>_` icon, or `docker exec -it owlet-capture bash`):
```bash
/app/mitm/frida/run-frida.sh owlet-redroid:5555 unpin
```

**3. In the scrcpy window: log into the Owlet app and OPEN THE CAMERA LIVE VIEW.**
That's what makes the app fetch the camera's UID + AuthKey. Watch:
- the mitmproxy web UI at `http://<unraid-ip>:8081` (password `owlet`)
- the console / `/captures` folder — credential candidates are flagged

**4. (While the camera is live) capture the TUTK stream params too:**
```bash
/app/mitm/frida/run-frida.sh owlet-redroid:5555 ioctl
```
This dumps the `avClientStart2` account/password and the `avSendIOCtrl` start
command — the rest of what the native bridge needs.

**5. Send me the `/captures` output** (`/mnt/user/appdata/owlet/captures`). I'll
wire the UID/AuthKey/AV-creds into the bridge and you're streaming.

## Tear down when done

```bash
docker compose down
docker volume rm 2>/dev/null || true
rm -rf /mnt/user/appdata/owlet/redroid-data   # optional: wipe the throwaway Android
```

## If the app won't run (Play Integrity / Google Play)

- **Won't install / "device not certified":** the app needs Google Play Services.
  Use a redroid **GApps** image (community builds of `redroid` include Play). Swap
  the `redroid/redroid:11.0.0-latest` image in `docker-compose.yml` for a GApps
  variant, then sign into Google first.
- **Installs but login fails / "can't verify device":** Play Integrity is blocking
  the emulator. Options: try a GApps image with a registered/CTS-profile device,
  or switch to the **phone capture** route (real device + patched APK) — tell me
  and I'll set that up.
- **App crashes on the camera screen:** try the Android 13 image
  (`redroid/redroid:13.0.0-latest`).

## What's bundled

| File | Role |
|---|---|
| `mitm/owlet_addon.py` | flags UID/AuthKey candidates from the auth flow |
| `mitm/frida/ssl-unpinning.js` | makes the app accept the mitm CA |
| `mitm/frida/hook-tutk-ioctl.js` | dumps the live TUTK stream-start protocol |
| `mitm/setup-redroid-mitm.sh` | CA + proxy + frida-server onto redroid (auto-run) |
| `mitm/frida/run-frida.sh` | launch the app under either Frida script |
| `probe/kalay-probe.py` | confirm Kalay on UDP 63616 |
