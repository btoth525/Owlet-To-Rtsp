#!/data/data/com.termux/files/usr/bin/bash
# run-cam.sh <camera-name> <go2rtc-output-url>
#
# go2rtc launches this as an `exec:` source. It sets up the audio/talk FIFOs,
# runs `tutk_client.py | ffmpeg` to publish RTSP to <output>, and — crucially —
# kills that whole pipeline when go2rtc stops/restarts this source.
#
# Why this script exists: when this ran as an inline `bash -c '… | …'`, go2rtc
# SIGTERM'd only the wrapper shell on restart; the tutk_client|ffmpeg children
# orphaned and kept holding the camera's SINGLE P2P session. Duplicates piled up
# and fought over it, so the stream flapped (connect→drop→reconnect). Here we run
# the pipeline in its own process group and TERM the whole group on exit, so no
# orphans survive and exactly one session exists at a time.
set -u
name="$1"
output="$2"

# Belt-and-suspenders: if a previous instance for THIS camera leaked, reap it
# before we start so we never stack two sessions. Match by the camera's audio
# FIFO path, which is unique per camera (process args alone aren't).
D="${TMPDIR:-/tmp}"
mkdir -p "$D" 2>/dev/null
F="$D/owlet-audio-$name"
T="$D/owlet-talk-$name"

# Load this camera's OWLET_* env (exported to children).
set -a
[ -f "/config/cameras/$name.env" ] && . "/config/cameras/$name.env"
set +a

rm -f "$F" "$T"
mkfifo "$F" 2>/dev/null
mkfifo "$T" 2>/dev/null
mkdir -p /config/vitals 2>/dev/null
export OWLET_AUDIO_FIFO="$F" OWLET_TALK_FIFO="$T" \
       OWLET_CAM_SENSORS="/config/vitals/cam-$name.json"

PGID=""
cleanup() {
  [ -n "$PGID" ] && kill -TERM -"$PGID" 2>/dev/null
  rm -f "$F" "$T"
  exit 0
}
trap cleanup TERM INT EXIT

# `set -m` puts the backgrounded pipeline in its own process group so we can
# signal the whole thing (tutk_client AND ffmpeg) in one shot.
set -m
python3 /app/tutk_client.py 2>>"/config/tutk-$name.log" \
  | ffmpeg -hide_banner -loglevel warning -fflags nobuffer+genpts -flags low_delay \
      -avioflags direct -max_delay 200000 \
      -use_wallclock_as_timestamps 1 -analyzeduration 1000000 -probesize 1000000 -f h264 -i - \
      -use_wallclock_as_timestamps 1 -thread_queue_size 512 -f aac -i "$F" \
      -map 0:v -map 1:a? -c:v copy -c:a aac -ar 16000 -ac 1 -b:a 64k \
      -muxdelay 0 -muxpreload 0 -f rtsp -rtsp_transport tcp "$output" &
CHILD=$!
# pgid of the pipeline (job-control groups all stages under one pgid).
PGID=$(ps -o pgid= -p "$CHILD" 2>/dev/null | tr -d ' ')
wait "$CHILD"
