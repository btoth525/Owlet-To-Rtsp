#!/usr/bin/env bash
# Watchdog — the load-bearing part.
# The Owlet app suspends its live view after an idle period and may show a
# "still watching?" prompt. A periodic tap on the center of the video keeps it
# alive and dismisses prompts. If the app loses foreground focus (dialog,
# logout, launcher), relaunch it.
set -uo pipefail

DEV="${ADB_DEVICE:-redroid:5555}"
PKG="${OWLET_PACKAGE:-com.owletcare.owletcare}"
TAP_X="${TAP_X:-640}"
TAP_Y="${TAP_Y:-360}"
INTERVAL="${WATCHDOG_INTERVAL:-120}"
GUARD="${WATCHDOG_APP_GUARD:-true}"

log() { echo "[watchdog] $*"; }

adb connect "$DEV" >/dev/null 2>&1 || true
log "tap (${TAP_X},${TAP_Y}) every ${INTERVAL}s; app-guard=${GUARD}"

while true; do
  if [ "$GUARD" = "true" ]; then
    focus="$(adb -s "$DEV" shell dumpsys window 2>/dev/null | tr -d '\r' | grep -m1 -E 'mCurrentFocus|mFocusedApp' || true)"
    if [ -n "$focus" ] && ! echo "$focus" | grep -q "$PKG"; then
      log "Owlet app not in focus, relaunching. (focus: ${focus})"
      adb -s "$DEV" shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
      sleep 8
    fi
  fi
  adb -s "$DEV" shell input tap "$TAP_X" "$TAP_Y" >/dev/null 2>&1 || \
    adb connect "$DEV" >/dev/null 2>&1 || true
  sleep "$INTERVAL"
done
