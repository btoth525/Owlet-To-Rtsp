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

echo "Frida [$MODE] -> $PKG on $DEV"
if [ "$MODE" = "unpin" ]; then
  # Spawn fresh so pinning is bypassed from process start.
  frida -U -f "$PKG" -l "$SCRIPT" --no-pause
else
  # Attach to the already-running app mid-live-view.
  frida -U -n "$PKG" -l "$SCRIPT"
fi
