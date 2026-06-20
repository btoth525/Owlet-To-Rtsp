#!/usr/bin/env bash
# Bridge entrypoint:
#   1. connect adb to redroid and wait for Android boot
#   2. set a fixed resolution/density (stable cropping)
#   3. auto-launch the Owlet app (self-heals after a restart)
#   4. start the watchdog (keeps the live view awake)
#   5. exec go2rtc, which owns the capture pipeline + serves RTSP
set -uo pipefail

DEV="${ADB_DEVICE:-redroid:5555}"
PKG="${OWLET_PACKAGE:-com.owletcare.owletcare}"

log() { echo "[entrypoint] $*"; }

adb start-server >/dev/null 2>&1 || true

log "Connecting adb to ${DEV} ..."
for _ in $(seq 1 60); do
  out="$(adb connect "$DEV" 2>/dev/null || true)"
  echo "$out" | grep -qiE "connected|already" && break
  sleep 2
done

log "Waiting for Android to finish booting (sys.boot_completed) ..."
for _ in $(seq 1 120); do
  bc="$(adb -s "$DEV" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r\n')"
  [ "$bc" = "1" ] && break
  adb connect "$DEV" >/dev/null 2>&1 || true
  sleep 3
done
if [ "$(adb -s "$DEV" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r\n')" != "1" ]; then
  log "WARNING: Android did not report boot_completed. Continuing anyway; go2rtc will retry capture."
else
  log "Android boot complete."
fi

# Fixed geometry => stable crop coordinates.
if [ -n "${SCREEN_SIZE:-}" ]; then
  adb -s "$DEV" shell wm size "$SCREEN_SIZE" >/dev/null 2>&1 || true
fi
if [ -n "${SCREEN_DENSITY:-}" ]; then
  adb -s "$DEV" shell wm density "$SCREEN_DENSITY" >/dev/null 2>&1 || true
fi
# Keep the screen on while plugged in (stayon = USB|AC|wireless)
adb -s "$DEV" shell settings put global stay_on_while_plugged_in 7 >/dev/null 2>&1 || true
adb -s "$DEV" shell svc power stayon true >/dev/null 2>&1 || true

# Auto-launch the app if it's installed (one-time login persists in /data).
if adb -s "$DEV" shell pm list packages 2>/dev/null | tr -d '\r' | grep -q "package:${PKG}$"; then
  log "Launching ${PKG} ..."
  adb -s "$DEV" shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
  sleep 6
else
  log "NOTE: ${PKG} is not installed yet. Do the one-time install + login (see README), then restart this container."
fi

# Watchdog runs alongside go2rtc.
log "Starting watchdog ..."
/app/watchdog.sh &

log "Starting go2rtc ..."
exec go2rtc -config /app/go2rtc.yaml
