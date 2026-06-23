#!/data/data/com.termux/files/usr/bin/bash
# Bionic runtime startup: control panel (Flask) + go2rtc. The Owlet TUTK libs
# load here because this is a Bionic (Termux) userspace.
export PATH=/data/data/com.termux/files/usr/bin:/usr/local/bin:/usr/bin:/bin
export LD_LIBRARY_PATH=/app/libs/x86_64:/data/data/com.termux/files/usr/lib

mkdir -p /config 2>/dev/null
if [ ! -f /config/owlet.env ]; then
  if ! (echo "# owlet-bridge" > /config/owlet.env) 2>/dev/null; then
    echo "[owlet-bridge/bionic] WARN: /config is not writable by this container."
    echo "[owlet-bridge/bionic]   Settings won't persist. On Unraid, run:"
    echo "[owlet-bridge/bionic]     chmod -R 777 /mnt/user/appdata/owlet/config"
    echo "[owlet-bridge/bionic]   then restart this container."
  fi
fi

echo "[owlet-bridge/bionic] control panel : http://<host>:8088"
echo "[owlet-bridge/bionic] video UI       : http://<host>:1984"
echo "[owlet-bridge/bionic] RTSP           : rtsp://<host>:8554/owlet"

# Cap the TUTK log so it can't slowly fill appdata. tutk_client appends to it via
# go2rtc's exec; keep the tail and start fresh if it's grown past ~20 MB.
if [ -f /config/tutk.log ] && [ "$(stat -c%s /config/tutk.log 2>/dev/null || echo 0)" -gt 20971520 ]; then
  tail -c 2097152 /config/tutk.log > /config/tutk.log.tmp 2>/dev/null && mv /config/tutk.log.tmp /config/tutk.log 2>/dev/null
  echo "[owlet-bridge/bionic] trimmed oversized /config/tutk.log"
fi

# Auto-provision the proprietary TUTK libs from a dropped Owlet APK (.apk/.apkm)
# if they're not already in the mounted libs folder. No-op once they're present.
python3 -c 'import sys; sys.path.insert(0,"/app"); from lib_extract import provision; ok,m=provision("/app/libs/x86_64",["/config","/app/libs","/apk"]); print("[owlet-bridge/bionic] libs:",m)' 2>&1

# sanity: confirm the TUTK libs load at startup (logs to container output)
python3 - <<'PY' 2>&1 || echo "[owlet-bridge/bionic] WARN: TUTK libs not loadable yet (mount /app/libs)"
import ctypes, os
d = os.environ.get("TUTK_LIB_DIR", "/app/libs/x86_64")
for n in ("libTUTKGlobalAPIs.so","libP2PTunnelAPIs.so","libRDTAPIs.so","libIOTCAPIs.so","libAVAPIs.so"):
    p = os.path.join(d, n)
    if os.path.exists(p):
        ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
print("[owlet-bridge/bionic] TUTK libs load OK")
PY

python3 /app/webapp.py &

# Keep the camera stream warm 24/7. The Owlet cam allows only one P2P session, and
# go2rtc tears the on-demand source down when the last viewer leaves — so a viewer
# (VLC/Frigate) reconnecting could fail until a manual restart. A persistent
# internal consumer keeps exactly one camera session alive that everyone shares,
# so connect/disconnect never touches it. Set OWLET_KEEPALIVE=0 to disable.
if [ "${OWLET_KEEPALIVE:-1}" = "1" ]; then
  ( sleep 8
    while true; do
      ffmpeg -hide_banner -loglevel error -rtsp_transport tcp \
             -i rtsp://127.0.0.1:8554/owlet -c copy -f mpegts /dev/null 2>/dev/null
      sleep 5
    done ) &
fi

exec go2rtc -config /app/go2rtc.yaml
