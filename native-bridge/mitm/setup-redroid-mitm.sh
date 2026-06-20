#!/usr/bin/env bash
# Prepare the redroid container to be MITM'd + Frida-instrumented.
# redroid is rooted, so we can install the mitmproxy CA into the system store
# and push a matching frida-server. Run this from a machine with adb.
#
#   ./setup-redroid-mitm.sh <redroid-ip:5555> <mitm-host-ip> [mitm-port] [frida-version] [arch]
#
# Defaults: mitm-port=8080, frida-version=16.5.6, arch=x86_64 (redroid is x86_64)
set -euo pipefail

DEV="${1:?usage: setup-redroid-mitm.sh <ip:5555> <mitm-host-ip> [port] [frida-ver] [arch]}"
MITM_IP="${2:?need mitmproxy host IP}"
MITM_PORT="${3:-8080}"
FRIDA_VER="${4:-16.5.6}"
ARCH="${5:-x86_64}"   # redroid = x86_64; physical phones usually arm64

adb connect "$DEV" >/dev/null 2>&1 || true
adb -s "$DEV" wait-for-device

echo "== 1. Set HTTP proxy -> ${MITM_IP}:${MITM_PORT} =="
adb -s "$DEV" shell settings put global http_proxy "${MITM_IP}:${MITM_PORT}"

echo "== 2. Install mitmproxy CA into the system trust store =="
# mitmproxy writes ~/.mitmproxy/mitmproxy-ca-cert.pem on first run.
CA="${HOME}/.mitmproxy/mitmproxy-ca-cert.pem"
if [ ! -f "$CA" ]; then
  echo "  !! $CA not found. Start mitmproxy once to generate it, then re-run." >&2
  exit 1
fi
# Android system certs are named <subject_hash>.0 (old-style openssl hash).
HASH="$(openssl x509 -inform PEM -subject_hash_old -in "$CA" | head -1)"
adb -s "$DEV" root >/dev/null 2>&1 || true
adb -s "$DEV" remount >/dev/null 2>&1 || true
adb -s "$DEV" push "$CA" "/data/local/tmp/${HASH}.0" >/dev/null
adb -s "$DEV" shell "mount -o rw,remount /system 2>/dev/null; \
  cp /data/local/tmp/${HASH}.0 /system/etc/security/cacerts/${HASH}.0 2>/dev/null; \
  chmod 644 /system/etc/security/cacerts/${HASH}.0 2>/dev/null" || \
  echo "  (system store may be read-only; Frida unpinning will cover trust anyway)"

echo "== 3. Push + start frida-server (${ARCH}, v${FRIDA_VER}) =="
FS="frida-server-${FRIDA_VER}-android-${ARCH}"
if [ ! -f "$FS" ]; then
  echo "  downloading $FS ..."
  curl -fsSL -o "${FS}.xz" \
    "https://github.com/frida/frida/releases/download/${FRIDA_VER}/${FS}.xz"
  xz -d "${FS}.xz"
fi
adb -s "$DEV" push "$FS" /data/local/tmp/frida-server >/dev/null
adb -s "$DEV" shell "chmod 755 /data/local/tmp/frida-server"
adb -s "$DEV" shell "pkill -f frida-server 2>/dev/null; \
  nohup /data/local/tmp/frida-server >/dev/null 2>&1 &" || true
sleep 2

echo "== done. Verify with:  frida-ps -U | grep -i owlet =="
echo "Then run: ./frida/run-frida.sh $DEV  (cert capture)  or hook-tutk-ioctl.js (stream RE)"
