#!/usr/bin/env bash
# Launch the Owlet app under Frida with the chosen script.
#   ./run-frida.sh <ip:5555> [unpin|ioctl] [package]
#
# unpin  -> ssl-unpinning.js   (use while mitmproxy captures the cloud auth)
# ioctl  -> hook-tutk-ioctl.js (use while live-viewing to dump the stream-start protocol)
set -euo pipefail

DEV="${1:?usage: run-frida.sh <ip:5555> [unpin|ioctl] [package]}"
MODE="${2:-unpin}"
PKG="${3:-com.owletcare.owletcare}"
HERE="$(cd "$(dirname "$0")" && pwd)"

case "$MODE" in
  unpin) SCRIPT="$HERE/ssl-unpinning.js" ;;
  ioctl) SCRIPT="$HERE/hook-tutk-ioctl.js" ;;
  *) echo "mode must be 'unpin' or 'ioctl'"; exit 1 ;;
esac

command -v frida >/dev/null || { echo "install frida tools:  pip install frida-tools"; exit 1; }
export ANDROID_SERIAL="$DEV"

# In a container we reach frida-server over a forwarded TCP port (set by
# setup-redroid-mitm.sh). Otherwise fall back to USB/adb enumeration.
FRIDA_HOST="${FRIDA_HOST:-127.0.0.1:27042}"
if frida-ps -H "$FRIDA_HOST" >/dev/null 2>&1; then
  TARGET=(-H "$FRIDA_HOST")
  echo "Frida [$MODE] -> $PKG via $FRIDA_HOST"
else
  TARGET=(-U)
  echo "Frida [$MODE] -> $PKG via USB/adb"
fi

if [ "$MODE" = "unpin" ]; then
  # Spawn fresh so pinning is bypassed from process start.
  frida "${TARGET[@]}" -f "$PKG" -l "$SCRIPT" --no-pause
else
  # Attach to the already-running app mid-live-view.
  frida "${TARGET[@]}" -n "$PKG" -l "$SCRIPT"
fi
