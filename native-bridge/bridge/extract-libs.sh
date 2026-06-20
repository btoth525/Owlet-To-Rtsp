#!/usr/bin/env bash
# Pull the ThroughTek/Kalay TUTK native libraries out of the Owlet APK and put
# them where this bridge expects them (./libs/<abi>/). The bridge loads them via
# ctypes — same as wyze-bridge does with Wyze's libs.
#
#   ./extract-libs.sh /path/to/owlet.apk [abi]
#   abi: x86_64 (default, matches the python:slim image), arm64-v8a, armeabi-v7a
set -euo pipefail

APK="${1:?usage: extract-libs.sh <owlet.apk> [abi]}"
ABI="${2:-x86_64}"
OUT="$(cd "$(dirname "$0")" && pwd)/libs/${ABI}"
mkdir -p "$OUT"
command -v unzip >/dev/null || { echo "need: unzip"; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "Extracting TUTK libs ($ABI) from $APK ..."
unzip -o -q "$APK" "lib/${ABI}/*" -d "$TMP" || true

found=0
for so in "$TMP/lib/${ABI}"/*.so; do
  [ -e "$so" ] || continue
  case "$(basename "$so")" in
    libIOTCAPIs.so|libAVAPIs.so|libTUTK*.so|libKalay*.so|libP2P*.so|libRDT*.so)
      cp -v "$so" "$OUT/"; found=$((found+1)) ;;
  esac
done

echo
if [ "$found" -gt 0 ]; then
  echo "[+] $found lib(s) -> $OUT"
else
  echo "[-] No TUTK libs found for ABI=$ABI. List what the APK ships:"
  echo "    unzip -l \"$APK\" | grep -iE 'lib/.*\\.so'"
fi
