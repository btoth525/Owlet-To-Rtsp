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
exec go2rtc -config /app/go2rtc.yaml
