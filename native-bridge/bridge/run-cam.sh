#!/data/data/com.termux/files/usr/bin/bash
# run-cam.sh <camera-name> <go2rtc-output-url>
#
# go2rtc launches this as an `exec:` source (with #killsignal=15&killtimeout=5 so
# it gets SIGTERM, not SIGKILL, on restart). It wires up the FIFOs, runs
# tutk_client -> ffmpeg -> RTSP at <output>, and — crucially — reaps that whole
# pipeline when go2rtc stops/restarts the source.
#
# Why this exists: the Owlet cam allows only ONE P2P session. When this ran as an
# inline `bash -c '… | …'`, go2rtc only signalled the wrapper shell on restart;
# the tutk_client/ffmpeg children orphaned and kept holding the session, so
# duplicates piled up and fought over it (stream flapped). Here we run the two
# stages as SEPARATE jobs (each with its own PID), exit the instant EITHER dies
# (so a dead first stage can't deadlock the wrapper), and kill both on the way out.
set -u
name="$1"
output="$2"

D="${TMPDIR:-/tmp}"
mkdir -p "$D" 2>/dev/null
F="$D/owlet-audio-$name"   # camera audio:  tutk_client -> ffmpeg
T="$D/owlet-talk-$name"    # talk/speaker:  webapp -> tutk_client
V="$D/owlet-video-$name"   # camera H.264:  tutk_client -> ffmpeg

# Load this camera's OWLET_* env (per-camera file; else the legacy single-cam env
# used by the baked-in fallback config).
set -a
if [ -f "/config/cameras/$name.env" ]; then
  . "/config/cameras/$name.env"
elif [ -f /config/owlet.env ]; then
  . /config/owlet.env
fi
set +a

# Belt-and-suspenders: kill any leaked prior holder of THIS camera's FIFOs so the
# single P2P slot is actually free before we reconnect (keyed by FIFO path, which
# is unique per camera — process args alone are not).
command -v fuser >/dev/null 2>&1 && fuser -k "$F" "$T" "$V" 2>/dev/null
rm -f "$F" "$T" "$V"
mkfifo "$F" 2>/dev/null
mkfifo "$T" 2>/dev/null
mkfifo "$V" 2>/dev/null
mkdir -p /config/vitals 2>/dev/null
export OWLET_AUDIO_FIFO="$F" OWLET_TALK_FIFO="$T" \
       OWLET_CAM_SENSORS="/config/vitals/cam-$name.json"

TUTK=""
FF=""
cleanup() {
  [ -n "${DONE:-}" ] && return
  DONE=1
  [ -n "$TUTK" ] && kill -TERM "$TUTK" 2>/dev/null
  [ -n "$FF" ] && kill -TERM "$FF" 2>/dev/null
  # last-resort: anything still holding the FIFOs (covers a forked grandchild)
  command -v fuser >/dev/null 2>&1 && fuser -k "$F" "$T" "$V" 2>/dev/null
  rm -f "$F" "$T" "$V"
}
trap cleanup TERM INT EXIT

# tutk_client writes H.264 to $V (stdout) and AAC to $F (OWLET_AUDIO_FIFO);
# ffmpeg muxes both into RTSP. Separate background jobs -> separate PIDs.
python3 /app/tutk_client.py > "$V" 2>>"/config/tutk-$name.log" &
TUTK=$!
ffmpeg -hide_banner -loglevel warning -fflags nobuffer+genpts -flags low_delay \
    -avioflags direct -max_delay 200000 \
    -use_wallclock_as_timestamps 1 -analyzeduration 1000000 -probesize 1000000 -f h264 -i "$V" \
    -use_wallclock_as_timestamps 1 -thread_queue_size 512 -f aac -i "$F" \
    -map 0:v -map 1:a? -c:v copy -c:a aac -ar 16000 -ac 1 -b:a 64k \
    -muxdelay 0 -muxpreload 0 -f rtsp -rtsp_transport tcp "$output" &
FF=$!

# Exit the moment EITHER stage dies; the EXIT trap then reaps the other so go2rtc
# always sees the source die and relaunches it.
wait -n "$TUTK" "$FF" 2>/dev/null
