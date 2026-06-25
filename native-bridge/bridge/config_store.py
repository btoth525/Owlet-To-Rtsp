#!/usr/bin/env python3
"""
config_store.py — the bridge's settings model + go2rtc stream generation.

Single source of truth, shared by webapp.py (the UI), render_streams.py (boot)
and keepalive.py. It stores **account-level** credentials plus a list of
**cameras**, and from that renders:

  * one env file per camera        -> /config/cameras/<name>.env
  * a generated go2rtc config       -> /config/go2rtc.gen.yaml
    (one `exec:` stream per camera, named by the camera's slug)

Backwards compatible: the old single-camera flat config (region/email/…/uid at
the top level) is migrated into a one-element `cameras` list named `owlet`, so an
existing Frigate URL `rtsp://host:8554/owlet` keeps working untouched.

Stdlib only — the go2rtc YAML is hand-rendered so we don't need PyYAML.
"""

from __future__ import annotations

import json
import os
import re

# The TUTK license key baked into the Owlet Android app (recovered by decompiling
# it). Pre-filled so a camera connects out of the box.
APP_LICENSE_KEY = (
    "AQAAAGHr2tF3sL8TGR+XirMqZSd8hKY3eBRqKIceLcUSy2okTWYU27qQmwzBORp3tw1yoqiX7l+"
    "yoikFTI+Dzh9M+utHJ/3UBjL8FkYk4kuTSdcE6FtpD3Gidjxnmu2z9TONdpEx15uXvTATqSexOC"
    "GDcldb3xtVXRmH0GoVx9SPKwVPaj7/iYJnPaaURxPzEbEr2Yfd0ckSZoZ8jRH5jxmcJdob"
)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/owlet.yaml")
ENV_PATH = os.environ.get("ENV_PATH", "/config/owlet.env")          # legacy primary env (fallback path)
GEN_PATH = os.environ.get("GO2RTC_GEN", "/config/go2rtc.gen.yaml")  # generated go2rtc config
CONFIG_DIR = os.path.dirname(CONFIG_PATH) or "/config"
CAM_DIR = os.path.join(CONFIG_DIR, "cameras")

# go2rtc listen ports INSIDE the container (host mapping is separate).
G_HTTP = int(os.environ.get("GO2RTC_HTTP_PORT", "1984"))
G_RTSP = int(os.environ.get("GO2RTC_RTSP_PORT", "8554"))
G_WEBRTC = int(os.environ.get("GO2RTC_WEBRTC_PORT", "8555"))

DEFAULT_CAM_NAME = "owlet"

# Account-level settings (shared by every camera).
ACCOUNT_FIELDS = ["region", "email", "password", "av_account",
                  "iotype_start", "av_channel", "license_key", "region_code"]
ACCOUNT_DEFAULTS: dict[str, str] = {
    "region": "world", "email": "", "password": "",
    "av_account": "admin", "iotype_start": "511", "av_channel": "0",
    "license_key": APP_LICENSE_KEY, "region_code": "3",
}

# Per-camera settings.
CAMERA_FIELDS = ["name", "camera_dsn", "uid", "authkey", "av_password",
                 "av_security_mode"]
CAMERA_DEFAULTS: dict[str, str] = {k: "" for k in CAMERA_FIELDS}

# The proven ffmpeg repackage pipeline (kept identical to the single-cam build):
# no `-fflags nobuffer` (it served codec-less RTSP); analyzeduration/probesize so
# it locks onto the first keyframe; wallclock timestamps so bursty P2P frames
# don't drift/stutter.
FFMPEG = ("ffmpeg -hide_banner -loglevel warning -fflags +genpts "
          "-use_wallclock_as_timestamps 1 -analyzeduration 5000000 "
          "-probesize 5000000 -f h264 -i - -c copy -f rtsp -rtsp_transport tcp {output}")


# --------------------------------------------------------------------------- #
# names
# --------------------------------------------------------------------------- #
def slugify(name: str, fallback: str = DEFAULT_CAM_NAME) -> str:
    """A go2rtc-/URL-safe stream name from a human nickname."""
    s = re.sub(r"[^a-z0-9_-]+", "", (name or "").strip().lower().replace(" ", "_"))
    s = s.strip("-_")
    return s or fallback


def _unique(name: str, used: set[str]) -> str:
    if name not in used:
        return name
    i = 2
    while f"{name}-{i}" in used:
        i += 1
    return f"{name}-{i}"


# --------------------------------------------------------------------------- #
# load / migrate
# --------------------------------------------------------------------------- #
def _empty() -> dict:
    cfg = dict(ACCOUNT_DEFAULTS)
    cfg["cameras"] = []
    return cfg


def _migrate_flat(data: dict) -> list[dict]:
    """Old config stored a single camera's fields at the top level — fold them
    into a one-element cameras list named 'owlet' (keeps existing URLs working)."""
    cam = {k: (data.get(k) or "") for k in CAMERA_FIELDS}
    cam["name"] = slugify(data.get("camera_name") or DEFAULT_CAM_NAME)
    has = any(cam.get(k) for k in ("camera_dsn", "uid", "authkey"))
    return [cam] if has else []


def _normalize_cameras(cams) -> list[dict]:
    out: list[dict] = []
    used: set[str] = set()
    for c in cams or []:
        if not isinstance(c, dict):
            continue
        cam = {k: (c.get(k) or "") for k in CAMERA_FIELDS}
        cam["name"] = _unique(slugify(cam.get("name")), used)
        used.add(cam["name"])
        out.append(cam)
    return out


def load_config() -> dict:
    """Return {account fields…, 'cameras': [ {camera fields…}, … ]}."""
    cfg = _empty()
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        raw = open(CONFIG_PATH).read()
    except Exception:  # noqa: BLE001
        return cfg
    data: dict = {}
    try:
        data = json.loads(raw) or {}
    except Exception:  # noqa: BLE001
        # tolerate an old "key: value" (yaml-ish) flat config
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, _, v = line.partition(":")
            data[k.strip()] = v.strip().strip("\"'")

    for k in ACCOUNT_FIELDS:
        v = data.get(k)
        # don't let an empty saved value clobber a baked-in default
        if (v is None or v == "") and ACCOUNT_DEFAULTS.get(k):
            continue
        if v is not None:
            cfg[k] = v

    cams = data.get("cameras")
    cfg["cameras"] = _normalize_cameras(cams) if isinstance(cams, list) and cams \
        else _migrate_flat(data)
    return cfg


def find_camera(cfg: dict, name: str) -> dict | None:
    for c in cfg.get("cameras") or []:
        if c.get("name") == name:
            return c
    return None


# --------------------------------------------------------------------------- #
# save + generate
# --------------------------------------------------------------------------- #
def _account_of(cfg: dict) -> dict:
    return {k: cfg.get(k, ACCOUNT_DEFAULTS[k]) for k in ACCOUNT_FIELDS}


def save_config(cfg: dict, regenerate: bool = True) -> bool:
    """Persist config (JSON) and regenerate the per-camera envs + go2rtc config.
    Returns False if the config folder isn't writable."""
    account = _account_of(cfg)
    cameras = _normalize_cameras(cfg.get("cameras"))
    out = dict(account)
    out["cameras"] = cameras
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as fh:
            json.dump(out, fh, indent=2)
    except OSError:
        return False
    if regenerate:
        try:
            generate(account, cameras)
        except OSError:
            return False
    return True


def camera_env(account: dict, cam: dict) -> dict[str, str]:
    """The OWLET_* env tutk_client.py reads for one camera."""
    env = {
        "OWLET_REGION": account.get("region", "world"),
        "OWLET_EMAIL": account.get("email", ""),
        "OWLET_PASSWORD": account.get("password", ""),
        "OWLET_AV_ACCOUNT": account.get("av_account", "admin"),
        "OWLET_IOTYPE_START": account.get("iotype_start", "511"),
        "OWLET_AV_CHANNEL": account.get("av_channel", "0"),
        "OWLET_LICENSE_KEY": account.get("license_key", APP_LICENSE_KEY),
        "OWLET_REGION_CODE": account.get("region_code", "3"),
        "OWLET_CAMERA_DSN": cam.get("camera_dsn", ""),
        "OWLET_UID": cam.get("uid", ""),
        "OWLET_AUTHKEY": cam.get("authkey", ""),
        "OWLET_AV_PASSWORD": cam.get("av_password", ""),
        "OWLET_CAMERA_NAME": cam.get("name", ""),
    }
    if cam.get("av_security_mode"):
        env["OWLET_AV_SECURITY_MODE"] = cam["av_security_mode"]
    return env


def _write_env(path: str, env: dict[str, str]) -> None:
    with open(path, "w") as fh:
        fh.write("# generated by owlet-bridge — do not edit (rewritten on save)\n")
        for k, v in env.items():
            fh.write(f"{k}={v}\n")


def _exec_source(name: str) -> str:
    """go2rtc `exec:` source for one camera: source its env, set up an audio FIFO,
    run tutk_client (H.264 video -> stdout, AAC audio -> FIFO), and let ffmpeg mux
    BOTH into RTSP at {output}. The Owlet audio is AAC-LC (ADTS), so it's read with
    `-f aac` and copied through with no re-encode. tutk_client stderr -> per-cam log.
    If the camera has no audio, the FIFO just stays empty and video still flows."""
    envf = "/config/cameras/%s.env" % name
    logf = "/config/tutk-%s.log" % name
    cmd = (
        'set -a; [ -f %(e)s ] && . %(e)s; '
        'D="${TMPDIR:-/tmp}"; mkdir -p "$D" 2>/dev/null; '
        'F="$D/owlet-audio-%(n)s"; rm -f "$F"; mkfifo "$F" 2>/dev/null; '
        'T="$D/owlet-talk-%(n)s"; rm -f "$T"; mkfifo "$T" 2>/dev/null; '
        'mkdir -p /config/vitals 2>/dev/null; '
        'OV="/config/vitals/overlay-%(n)s.txt"; [ -f "$OV" ] || printf " " > "$OV"; '
        'export OWLET_AUDIO_FIFO="$F" OWLET_TALK_FIFO="$T" '
        'OWLET_CAM_SENSORS="/config/vitals/cam-%(n)s.json"; '
        'trap "rm -f $F $T" EXIT; '
        # Video output: default is a pure copy (lowest latency, no CPU). With
        # OWLET_OVERLAY=1 we burn the glass HUD (overlay text file) into the feed
        # — that requires re-encoding, so we use h264_nvenc if this ffmpeg has it,
        # else libx264 -tune zerolatency to keep the added latency small.
        'VENC="-c:v copy"; VF=""; '
        'if [ "${OWLET_OVERLAY:-0}" = "1" ]; then '
        '  if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_nvenc; then '
        '    VENC="-c:v h264_nvenc -preset p2 -tune ll -g 60"; '
        '  else '
        '    VENC="-c:v libx264 -preset veryfast -tune zerolatency -g 60 -pix_fmt yuv420p"; '
        '  fi; '
        '  VF="-filter:v drawtext=fontfile=/app/fonts/DejaVuSans.ttf:textfile=$OV:reload=1'
        ':fontcolor=white:fontsize=h/26:box=1:boxcolor=black@0.40:boxborderw=18'
        ':shadowcolor=black@0.6:shadowx=2:shadowy=2:x=(w-tw)/2:y=h-th-(h/16)"; '
        'fi; '
        'python3 /app/tutk_client.py 2>>%(l)s | '
        # Low-latency ingest: nobuffer + low_delay so ffmpeg doesn't sit on frames,
        # small analyze/probe so it locks on fast.
        'ffmpeg -hide_banner -loglevel warning -fflags nobuffer+genpts -flags low_delay '
        '-avioflags direct -max_delay 200000 '
        '-use_wallclock_as_timestamps 1 -analyzeduration 1000000 -probesize 1000000 -f h264 -i - '
        '-use_wallclock_as_timestamps 1 -thread_queue_size 512 -f aac -i "$F" '
        # Re-encode AAC (don't copy): the camera's ADTS AAC has no global headers,
        # which ffmpeg's RTSP muxer rejects ("AAC with no global headers"); the
        # encoder emits proper headers. 16k mono is plenty for voice and ~free CPU.
        '-map 0:v -map 1:a? $VF $VENC -c:a aac -ar 16000 -ac 1 -b:a 64k '
        '-muxdelay 0 -muxpreload 0 -f rtsp -rtsp_transport tcp {output}'
    ) % {"e": envf, "l": logf, "n": name}
    return "exec:bash -c '" + cmd + "'"


def render_go2rtc(cameras: list[dict]) -> str:
    lines = [
        "# generated by owlet-bridge from your saved cameras — do not edit by hand.",
        "log:", "  level: info", "",
        "api:", f'  listen: ":{G_HTTP}"', "",
        "rtsp:", f'  listen: ":{G_RTSP}"', "",
        "webrtc:", f'  listen: ":{G_WEBRTC}"', "",
        "streams:",
    ]
    for cam in cameras:
        lines.append(f"  {cam['name']}:")
        lines.append("    - " + _exec_source(cam["name"]))
    return "\n".join(lines) + "\n"


def generate(account: dict, cameras: list[dict]) -> str:
    """Write per-camera env files + the generated go2rtc config. With no cameras
    yet, still emit a single placeholder `owlet` stream so default URLs resolve."""
    render_cams = cameras or [dict(CAMERA_DEFAULTS, name=DEFAULT_CAM_NAME)]
    os.makedirs(CAM_DIR, exist_ok=True)
    for cam in render_cams:
        _write_env(os.path.join(CAM_DIR, cam["name"] + ".env"), camera_env(account, cam))
    # legacy primary env, for the baked-in single-cam fallback go2rtc.yaml
    _write_env(ENV_PATH, camera_env(account, render_cams[0]))
    with open(GEN_PATH, "w") as fh:
        fh.write(render_go2rtc(render_cams))
    return GEN_PATH


def camera_names(cfg: dict | None = None) -> list[str]:
    cfg = cfg or load_config()
    return [c["name"] for c in cfg.get("cameras") or []] or [DEFAULT_CAM_NAME]


# Drag-and-drop lullaby/sound MP3s live here; persisted with the config.
SOUNDS_DIR = os.path.join(CONFIG_DIR, "sounds")


def talk_fifo_path(name: str) -> str:
    """The FIFO tutk_client reads talk/sound audio from — same path the generated
    exec creates (${TMPDIR:-/tmp}/owlet-talk-<name>)."""
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, "owlet-talk-" + name)


# Per-camera room-sensor sidecar (temp/humidity/noise/brightness/motion/sound),
# written by tutk_client from the TUTK frame info + GetRealtimeData IOCTL.
VITALS_DIR = os.path.join(CONFIG_DIR, "vitals")


def cam_sensors_path(name: str) -> str:
    return os.path.join(VITALS_DIR, "cam-" + name + ".json")
