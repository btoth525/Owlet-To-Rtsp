#!/usr/bin/env bash
# owlet-capture entrypoint: run mitmproxy (proxy + web UI + addon) as the main
# process, and once redroid is up, auto-configure it (CA + proxy + frida-server).
# The remaining steps (install the app, drive the UI, run the frida scripts) are
# done by you — instructions are printed below and in the README.
set -uo pipefail

REDROID="${REDROID:-owlet-redroid:5555}"
MITM_IP="${MITM_IP:-<unraid-ip>}"
MITM_PORT="${MITM_PORT:-8080}"

setup_when_ready() {
  echo "[setup] waiting for mitmproxy CA to be generated …"
  for _ in $(seq 1 60); do
    [ -f /root/.mitmproxy/mitmproxy-ca-cert.pem ] && break
    sleep 1
  done

  echo "[setup] waiting for redroid to boot ($REDROID) …"
  adb connect "$REDROID" >/dev/null 2>&1 || true
  for _ in $(seq 1 120); do
    [ "$(adb -s "$REDROID" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r\n')" = "1" ] && break
    adb connect "$REDROID" >/dev/null 2>&1 || true
    sleep 3
  done

  echo "[setup] configuring redroid: CA + proxy + frida-server …"
  /app/mitm/setup-redroid-mitm.sh "$REDROID" "$MITM_IP" "$MITM_PORT" || true

  cat <<EOF

================= owlet-capture ready =========================================
 mitmproxy web UI : http://$MITM_IP:8081   (password: ${MITM_WEB_PASSWORD:-owlet})
 captures saved   : /captures   (UID / AuthKey candidates flagged)

 STEP 1 — install + drive the Owlet app (from your PC):
   adb connect $REDROID
   adb -s $REDROID install /path/to/owlet.apk
   scrcpy -s $REDROID         # log in, then OPEN THE CAMERA LIVE VIEW

 STEP 2 — capture the cloud auth (in THIS container's console):
   /app/mitm/frida/run-frida.sh $REDROID unpin
   # then in scrcpy, log in / open the camera; watch the web UI + /captures

 STEP 3 — capture the TUTK stream params while the camera is live:
   /app/mitm/frida/run-frida.sh $REDROID ioctl

 We're hunting for the camera's Kalay UID + AuthKey. Share /captures output.
 If the app won't launch or log in (Play Integrity / needs Google Play), tell
 me — we switch to a redroid GApps image or the phone route.
===============================================================================

EOF
}

setup_when_ready &

exec mitmweb \
  --web-host 0.0.0.0 --web-port 8081 \
  --listen-host 0.0.0.0 --listen-port "$MITM_PORT" \
  --set web_password="${MITM_WEB_PASSWORD:-owlet}" \
  -s /app/mitm/owlet_addon.py
