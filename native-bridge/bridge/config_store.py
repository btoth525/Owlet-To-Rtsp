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

# A WebRTC candidate is "<host-or-ip>:<port>" (v4, bracketed v6, or hostname).
_CAND_RE = re.compile(r"^[\w.\[\]:-]+:\d{1,5}$")


def _atomic_write(path: str, text: str) -> None:
    """Write text to path atomically: tmp file + fsync + os.replace. A crash,
    SIGKILL, or full disk mid-write can never leave a half-written/empty file
    (which previously could silently wipe the saved config)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _sh_squote(v) -> str:
    """Single-quote a value for a sourced shell env file so credential/UI values
    containing $(...), backticks, ;, newlines, etc. can never execute when the
    go2rtc exec sources it with `. <file>`."""
    s = str(v).replace("\n", " ").replace("\r", " ")
    return "'" + s.replace("'", "'\\''") + "'"

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
                  "iotype_start", "av_channel", "license_key", "region_code",
                  "webrtc_candidate", "ui_user", "ui_pass"]
ACCOUNT_DEFAULTS: dict[str, str] = {
    "region": "world", "email": "", "password": "",
    "av_account": "admin", "iotype_start": "511", "av_channel": "0",
    "license_key": APP_LICENSE_KEY, "region_code": "3",
    "webrtc_candidate": "", "ui_user": "", "ui_pass": "",
}

# Per-camera settings.
CAMERA_FIELDS = ["name", "camera_dsn", "uid", "authkey", "av_password",
                 "av_security_mode"]
CAMERA_DEFAULTS: dict[str, str] = {k: "" for k in CAMERA_FIELDS}

# Home Assistant / MQTT — settable in the UI (env vars still work as defaults).
MQTT_FIELDS = ["enabled", "host", "port", "user", "password", "prefix"]
MQTT_DEFAULTS: dict[str, str] = {
    "enabled": "", "host": "", "port": "1883", "user": "", "password": "",
    "prefix": "homeassistant",
}

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
    cfg["mqtt"] = dict(MQTT_DEFAULTS)
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
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw) or {}
        except Exception:  # noqa: BLE001
            # Corrupt JSON: DON'T silently fall through to an empty config (the
            # next save would persist that, wiping every camera). Preserve the
            # original so it can be recovered by hand, then return defaults.
            try:
                os.replace(CONFIG_PATH, CONFIG_PATH + ".bad")
            except OSError:
                pass
            return cfg
    else:
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

    mq = data.get("mqtt") if isinstance(data.get("mqtt"), dict) else {}

    def _mqv(k: str):
        v = mq.get(k)
        # port/prefix have meaningful defaults — don't let a blank override them
        if (v is None or v == "") and k in ("port", "prefix"):
            return MQTT_DEFAULTS[k]
        return v if v is not None else MQTT_DEFAULTS[k]
    cfg["mqtt"] = {k: _mqv(k) for k in MQTT_FIELDS}
    return cfg


def mqtt_settings(cfg: dict | None = None) -> dict:
    """Effective MQTT config: UI settings, falling back to env vars. `enabled`
    is true if explicitly enabled in the UI or an OWLET_MQTT_HOST env is set."""
    cfg = cfg or load_config()
    m = dict(MQTT_DEFAULTS)
    m.update(cfg.get("mqtt") or {})
    env = os.environ
    host = m.get("host") or env.get("OWLET_MQTT_HOST", "")
    out = {
        "host": host,
        "port": str(m.get("port") or env.get("OWLET_MQTT_PORT") or "1883"),
        "user": m.get("user") or env.get("OWLET_MQTT_USER", ""),
        "password": m.get("password") or env.get("OWLET_MQTT_PASS", ""),
        "prefix": m.get("prefix") or env.get("OWLET_MQTT_PREFIX") or "homeassistant",
    }
    ui_enabled = str(m.get("enabled")).lower() in ("1", "true", "on", "yes")
    out["enabled"] = bool(host) and (ui_enabled or bool(env.get("OWLET_MQTT_HOST")))
    return out


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
    mq = cfg.get("mqtt")
    if isinstance(mq, dict):
        out["mqtt"] = {k: mq.get(k, MQTT_DEFAULTS[k]) for k in MQTT_FIELDS}
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        _atomic_write(CONFIG_PATH, json.dumps(out, indent=2))
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
    lines = ["# generated by owlet-bridge — do not edit (rewritten on save)"]
    for k, v in env.items():
        lines.append(f"{k}={_sh_squote(v)}")
    _atomic_write(path, "\n".join(lines) + "\n")


def _exec_source(name: str) -> str:
    """go2rtc `exec:` source for one camera: source its env, set up the audio +
    talk FIFOs, run tutk_client (H.264 -> stdout, AAC -> FIFO) piped to ffmpeg,
    which muxes both into RTSP at {output}.

    The ffmpeg flags match the PROVEN single-cam (:latest) pipeline:
      * `-fflags +genpts` — NOT `nobuffer`. `nobuffer` makes ffmpeg emit the RTSP
        stream BEFORE it has parsed the H.264 SPS/PPS, so every consumer (Frigate,
        browser, WebRTC) gets a stream with no codec info and decodes nothing
        ("Invalid data found"). This was the regression that broke playback.
      * generous analyzeduration/probesize (5s/5MB) so it locks onto the first
        keyframe before declaring the stream.
      * wallclock timestamps so bursty P2P frames don't drift/stutter.
    Audio is optional (`-map 1:a?`) and re-encoded to AAC (ffmpeg's RTSP muxer
    rejects the cam's header-less ADTS on copy)."""
    envf = "/config/cameras/%s.env" % name
    logf = "/config/tutk-%s.log" % name
    cmd = (
        'set -a; [ -f %(e)s ] && . %(e)s; '
        'D="${TMPDIR:-/tmp}"; mkdir -p "$D" 2>/dev/null; '
        'F="$D/owlet-audio-%(n)s"; rm -f "$F"; mkfifo "$F" 2>/dev/null; '
        'T="$D/owlet-talk-%(n)s"; rm -f "$T"; mkfifo "$T" 2>/dev/null; '
        'mkdir -p /config/vitals 2>/dev/null; '
        'export OWLET_AUDIO_FIFO="$F" OWLET_TALK_FIFO="$T" '
        'OWLET_CAM_SENSORS="/config/vitals/cam-%(n)s.json"; '
        'trap "rm -f $F $T" EXIT; '
        'python3 /app/tutk_client.py 2>>%(l)s | '
        'ffmpeg -hide_banner -loglevel warning -fflags +genpts '
        '-use_wallclock_as_timestamps 1 -analyzeduration 5000000 -probesize 5000000 -f h264 -i - '
        '-use_wallclock_as_timestamps 1 -thread_queue_size 512 -f aac -i "$F" '
        '-map 0:v -map 1:a? -c:v copy -c:a aac -ar 16000 -ac 1 -b:a 64k '
        '-f rtsp -rtsp_transport tcp {output}'
    ) % {"e": envf, "l": logf, "n": slugify(name)}
    return "exec:bash -c '" + cmd + "'"


def _overlay_source(name: str) -> str:
    """A SEPARATE on-demand stream `<name>_overlay`: pull the already-running
    clean stream from go2rtc and burn the glass HUD (overlay text file, written
    by vitals_poller) into it. Costs nothing until something actually views it;
    the clean `<name>` stream is never touched. Uses h264_nvenc if the runtime
    has it, else libx264 -tune zerolatency."""
    name = slugify(name)  # defense in depth: never interpolate a raw name into bash
    src = "rtsp://127.0.0.1:%s/%s" % (G_RTSP, name)
    cmd = (
        'OV="/config/vitals/overlay-%(n)s.txt"; mkdir -p /config/vitals 2>/dev/null; '
        '[ -f "$OV" ] || printf " " > "$OV"; '
        'VENC="-c:v libx264 -preset veryfast -tune zerolatency -g 60 -pix_fmt yuv420p"; '
        'ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_nvenc && '
        'VENC="-c:v h264_nvenc -preset p2 -tune ll -g 60"; '
        'exec ffmpeg -hide_banner -loglevel warning -fflags nobuffer -flags low_delay '
        '-rtsp_transport tcp -i %(src)s '
        '-filter:v drawtext=fontfile=/app/fonts/DejaVuSans.ttf:textfile=$OV:reload=1'
        ':fontcolor=white:fontsize=h/26:line_spacing=6:box=1:boxcolor=black@0.40:boxborderw=18'
        ':shadowcolor=black@0.6:shadowx=2:shadowy=2:x=(w-tw)/2:y=h-th-(h/16) '
        '$VENC -c:a copy -muxdelay 0 -muxpreload 0 -f rtsp -rtsp_transport tcp {output}'
    ) % {"n": name, "src": src}
    return "exec:bash -c '" + cmd + "'"


def render_go2rtc(cameras: list[dict], candidate: str | None = None) -> str:
    lines = [
        "# generated by owlet-bridge from your saved cameras — do not edit by hand.",
        "log:", "  level: info", "",
        "api:", f'  listen: ":{G_HTTP}"', "",
        "rtsp:", f'  listen: ":{G_RTSP}"', "",
        "webrtc:", f'  listen: ":{G_WEBRTC}"',
    ]
    # External WebRTC candidate(s) for sub-second from phones/other hosts. Comes
    # from the saved config (UI field, survives restarts) or the
    # OWLET_WEBRTC_CANDIDATE env var. Value is "<host-ip>:<webrtc host port>".
    cand = (candidate if candidate is not None
            else os.environ.get("OWLET_WEBRTC_CANDIDATE", "")).strip()
    if cand:
        # Validate + YAML-quote each entry so a bad free-text value can't brick
        # the whole go2rtc config (which would survive every restart).
        valid = [c.strip() for c in cand.split(",")
                 if c.strip() and _CAND_RE.match(c.strip())]
        if valid:
            lines.append("  candidates:")
            for c in valid:
                lines.append(f'    - "{c}"')
    lines += ["", "streams:"]
    for cam in cameras:
        nm = slugify(cam.get("name") or "")  # never interpolate a raw name
        lines.append(f"  {nm}:")
        lines.append("    - " + _exec_source(nm))
        # Optional glass-HUD variant, on-demand (free unless viewed).
        lines.append(f"  {nm}_overlay:")
        lines.append("    - " + _overlay_source(nm))
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
    # Candidate from the saved config (boot-safe — the Termux base image drops
    # docker -e vars before the app starts) falling back to the env var.
    cand = (account.get("webrtc_candidate") or "").strip() \
        or os.environ.get("OWLET_WEBRTC_CANDIDATE", "")
    _atomic_write(GEN_PATH, render_go2rtc(render_cams, candidate=cand))
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
