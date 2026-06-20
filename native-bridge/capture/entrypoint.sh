#!/usr/bin/env bash
# Runs mitmweb (web UI + proxy + the Owlet addon) as the main process.
# Open the Unraid container Console (>_) to run the capture steps.
set -uo pipefail

REDROID="${REDROID:-owlet-redroid:5555}"
MITM_IP="${MITM_IP:-<unraid-ip>}"
MITM_PORT="${MITM_PORT:-8080}"

cat <<EOF

================ owlet-capture =================================================
 mitmproxy web UI : http://<unraid-ip>:8081
 proxy listener   : <unraid-ip>:${MITM_PORT}   (redroid points here)
 captures saved to: ${OWLET_CAP_DIR:-/captures}/   (UID/AuthKey candidates flagged)

 ONE-TIME CAPTURE — open this container's Console (>_) and run:

   # 1. Point redroid at the proxy, install the CA, start frida-server
   /app/mitm/setup-redroid-mitm.sh ${REDROID} ${MITM_IP} ${MITM_PORT}

   # 2a. CLOUD AUTH — spawn the app unpinned, then LOG IN in the app.
   #     Watch this console + the web UI for UID / AuthKey candidates.
   /app/mitm/frida/run-frida.sh ${REDROID} unpin

   # 2b. STREAM PROTOCOL — open the camera live view, then attach the hook.
   #     Note the avClientStart2 args + the avSendIOCtrl start command.
   /app/mitm/frida/run-frida.sh ${REDROID} ioctl

   # optional: confirm Kalay on the LAN
   python3 /app/probe/kalay-probe.py <camera-ip>

 Then send the captures/* output back so we wire in the 3 values.
================================================================================

EOF

# Generate the CA up front so setup-redroid-mitm.sh can install it immediately.
if [ ! -f /root/.mitmproxy/mitmproxy-ca-cert.pem ]; then
  echo "[entrypoint] generating mitmproxy CA ..."
  timeout 5 mitmdump -q >/dev/null 2>&1 || true
fi

exec mitmweb \
  --web-host 0.0.0.0 --web-port 8081 \
  --listen-host 0.0.0.0 --listen-port "${MITM_PORT}" \
  --set web_password="${MITM_WEB_PASSWORD:-owlet}" \
  -s /app/mitm/owlet_addon.py
