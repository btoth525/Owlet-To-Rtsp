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
echo "[owlet-bridge/bionic] RTSP           : rtsp://<host>:8554/<camera>"

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

# Generate the go2rtc config (one stream per saved camera) + per-camera env files.
# Falls back to the baked-in single-camera /app/go2rtc.yaml if /config isn't
# writable (so a permission problem degrades gracefully to the old behaviour).
GO2RTC_CFG=/app/go2rtc.yaml
if python3 /app/render_streams.py; then
  [ -f /config/go2rtc.gen.yaml ] && GO2RTC_CFG=/config/go2rtc.gen.yaml
fi
echo "[owlet-bridge/bionic] go2rtc config : $GO2RTC_CFG"

python3 /app/webapp.py &

# Keep every camera's stream warm 24/7 (one persistent internal viewer each). The
# supervisor re-reads the camera list, so cameras added/removed in the UI are
# picked up without a restart. Set OWLET_KEEPALIVE=0 to disable.
if [ "${OWLET_KEEPALIVE:-1}" = "1" ]; then
  python3 /app/keepalive.py &
fi

exec go2rtc -config "$GO2RTC_CFG"
