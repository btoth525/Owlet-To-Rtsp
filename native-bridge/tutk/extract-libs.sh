#!/usr/bin/env bash
# Pull the ThroughTek/Kalay TUTK native libraries out of the Owlet APK.
# These are the SAME SDK the app uses; the bridge loads them via ctypes.
# (wyze-bridge does the equivalent with Wyze's libs.)
#
#   ./extract-libs.sh /path/to/owlet.apk [abi]
#
# abi: arm64-v8a (physical phone), x86_64 (redroid/emulator), armeabi-v7a
set -euo pipefail

APK="${1:?usage: extract-libs.sh <owlet.apk> [abi]}"
ABI="${2:-arm64-v8a}"
OUT="$(cd "$(dirname "$0")" && pwd)/libs/${ABI}"
mkdir -p "$OUT"

command -v unzip >/dev/null || { echo "need: unzip"; exit 1; }

echo "Extracting TUTK libs ($ABI) from $APK ..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
unzip -o -q "$APK" "lib/${ABI}/*" -d "$TMP" || true

found=0
for so in "$TMP/lib/${ABI}"/*.so; do
  [ -e "$so" ] || continue
  base="$(basename "$so")"
  case "$base" in
    libIOTCAPIs.so|libAVAPIs.so|libTUTK*.so|libKalay*.so|libP2P*.so|libRDT*.so)
      cp -v "$so" "$OUT/"; found=$((found+1)) ;;
  esac
done

echo
if [ "$found" -gt 0 ]; then
  echo "[+] Copied $found TUTK lib(s) to $OUT"
  echo "    These get baked into the native-bridge image."
else
  echo "[-] No obvious TUTK libs found for ABI=$ABI."
  echo "    List everything the APK ships and look for IOTC/AV/Kalay/TUTK:"
  echo "      unzip -l \"$APK\" | grep -iE 'lib/.*\\.so'"
fi
