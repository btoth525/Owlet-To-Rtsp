#!/usr/bin/env bash
# Capture the redroid display and push it to go2rtc as RTSP.
# Invoked by go2rtc as:  exec:bash /app/capture.sh {output}
# go2rtc supervises this process and restarts it when a consumer connects.
#
# Strategy:
#   CAPTURE_METHOD=auto  -> try scrcpy (low latency); if it dies within 10s,
#                           drop a flag and use screenrecord from then on.
#   CAPTURE_METHOD=scrcpy / screenrecord -> force that method.
#
# Encoder:
#   ENCODER=auto -> h264_nvenc if available, else libx264
#   ENCODER=libx264 / h264_nvenc -> force
set -uo pipefail

OUTPUT="${1:?go2rtc must pass {output} as arg 1}"
DEV="${ADB_DEVICE:-redroid:5555}"
FPS="${FPS:-15}"
BITRATE="${BITRATE:-4000000}"
CROP="${CROP:-}"
FALLBACK_FLAG="/tmp/owlet_capture_fallback"

log() { echo "[capture] $*" >&2; }

adb connect "$DEV" >/dev/null 2>&1 || true

# ---- video filter (crop is optional) -------------------------------------
FILTER=()
if [ -n "$CROP" ]; then
  FILTER=(-vf "crop=${CROP}")
  log "cropping to ${CROP}"
fi

# ---- pick encoder ---------------------------------------------------------
ENC="${ENCODER:-auto}"
if [ "$ENC" = "auto" ]; then
  if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'h264_nvenc'; then
    ENC=h264_nvenc
  else
    ENC=libx264
    log "h264_nvenc not found in ffmpeg; using libx264"
  fi
fi

if [ "$ENC" = "h264_nvenc" ]; then
  VCODEC=(-c:v h264_nvenc -preset p4 -tune ll -rc cbr -b:v "$BITRATE" \
          -g 30 -bf 0 -pix_fmt yuv420p)
else
  VCODEC=(-c:v libx264 -preset veryfast -tune zerolatency -b:v "$BITRATE" \
          -g 30 -pix_fmt yuv420p)
fi
log "encoder=${ENC} bitrate=${BITRATE} fps=${FPS}"

run_ffmpeg() {  # reads the capture stream on stdin, pushes RTSP to $OUTPUT
  local infmt=("$@")
  ffmpeg -hide_banner -loglevel warning \
    -fflags nobuffer -flags low_delay \
    "${infmt[@]}" -i - \
    -r "$FPS" "${FILTER[@]}" "${VCODEC[@]}" -an \
    -f rtsp -rtsp_transport tcp "$OUTPUT"
}

capture_scrcpy() {
  command -v scrcpy >/dev/null 2>&1 || return 127
  log "capture method: scrcpy"
  SDL_VIDEODRIVER=dummy scrcpy -s "$DEV" \
    --no-playback --no-audio \
    --video-codec=h264 --max-fps="$FPS" \
    --record=- --record-format=mkv 2>/tmp/scrcpy.err \
  | run_ffmpeg
}

capture_screenrecord() {
  log "capture method: screenrecord"
  # screenrecord caps at 180s, so loop it. Brief glitch at each restart.
  { while true; do
      adb -s "$DEV" exec-out screenrecord --output-format=h264 \
        --time-limit=180 --bit-rate="$BITRATE" - 2>/dev/null || break
    done; } \
  | run_ffmpeg -f h264
}

# ---- choose + run ---------------------------------------------------------
METHOD="${CAPTURE_METHOD:-auto}"
if [ "$METHOD" = "auto" ]; then
  if [ -f "$FALLBACK_FLAG" ]; then METHOD=screenrecord; else METHOD=scrcpy; fi
fi

start=$(date +%s)
case "$METHOD" in
  scrcpy)       capture_scrcpy ;       rc=$? ;;
  screenrecord) capture_screenrecord ; rc=$? ;;
  *) log "unknown CAPTURE_METHOD='$METHOD'; using screenrecord"; capture_screenrecord; rc=$? ;;
esac
end=$(date +%s)

# If scrcpy died almost immediately (missing binary, headless issue, etc.),
# remember to use screenrecord on the next go2rtc-supervised restart.
if [ "$METHOD" = "scrcpy" ] && [ $((end - start)) -lt 10 ]; then
  log "scrcpy pipeline exited after $((end - start))s — switching to screenrecord for subsequent restarts"
  touch "$FALLBACK_FLAG"
fi

exit "${rc:-0}"
