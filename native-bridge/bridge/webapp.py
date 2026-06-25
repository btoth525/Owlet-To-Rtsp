#!/usr/bin/env python3
"""
webapp.py — the owlet-bridge control panel (wyze-bridge style), now multi-camera.

Everything configurable in the browser:
  - Owlet account credentials + region (shared by every camera)
  - Any number of cameras, each added by its DSN — "Connect" runs the REAL Owlet
    login + camera-key (KMS) fetch and streams every step to a live log pane
  - Each camera becomes its own go2rtc stream at rtsp://host:8554/<name>

Runs on :8088. go2rtc serves the actual video + its own UI on :1984.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque

import requests
from flask import Flask, Response, jsonify, render_template, request

import config_store as cs

app = Flask(__name__)
# Owlet APK bundles (.apkm) are large; allow up to 1 GB uploads.
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024

# Quiet the per-request access log so go2rtc / TUTK output is visible in
# `docker logs` (the status/findings polling was drowning everything out).
import logging  # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.WARNING)

GO2RTC_API = os.environ.get("GO2RTC_API", "http://127.0.0.1:1984")
TUTK_LIB_DIR = os.environ.get("TUTK_LIB_DIR", "/app/libs/x86_64")
# Host-facing ports for the copy URLs shown in the UI. Default to the in-container
# ports; set these to the mapped host ports (e.g. 18554/1985/18555) when they're
# remapped to coexist with Frigate, so the UI shows reachable URLs.
PUBLIC_HTTP_PORT = os.environ.get("PUBLIC_HTTP_PORT", "1984")
PUBLIC_RTSP_PORT = os.environ.get("PUBLIC_RTSP_PORT", "8554")
PUBLIC_WEBRTC_PORT = os.environ.get("PUBLIC_WEBRTC_PORT", "8555")

# Optional Web UI auth (off by default). Set both to require HTTP Basic auth.
UI_USER = os.environ.get("OWLET_UI_USER", "")
UI_PASS = os.environ.get("OWLET_UI_PASS", "")

LOG: deque[str] = deque(maxlen=6000)
STATE: dict = {"candidates": [], "devices": None, "busy": False, "cam_busy": set()}
# Serialize config writes — the background per-camera diagnose threads and the
# request handlers can otherwise interleave json.dump() and corrupt the file.
_CFG_LOCK = threading.Lock()


def log(msg: str = "") -> None:
    ts = time.strftime("%H:%M:%S")
    for line in str(msg).splitlines() or [""]:
        LOG.append(f"{ts}  {line}")


# --------------------------------------------------------------------------- #
# optional auth
# --------------------------------------------------------------------------- #
@app.before_request
def _auth():
    if not (UI_USER and UI_PASS):
        return None
    a = request.authorization
    if a and a.username == UI_USER and a.password == UI_PASS:
        return None
    return Response("auth required", 401, {"WWW-Authenticate": 'Basic realm="owlet-bridge"'})


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def save_config(cfg: dict) -> bool:
    with _CFG_LOCK:
        ok = cs.save_config(cfg)
    if ok:
        return True
    log(f"!! CANNOT WRITE {cs.CONFIG_DIR}. The mounted config folder isn't writable "
        "by this container — on Unraid run `chmod -R 777 "
        "/mnt/user/appdata/owlet/config` and restart. Settings won't persist until then.")
    return False


def _mask_pw(v: str) -> str:
    return "********" if v else ""


@app.get("/api/config")
def get_config():
    """Account-level settings only (cameras have their own endpoint)."""
    cfg = cs.load_config()
    out = {k: cfg.get(k, cs.ACCOUNT_DEFAULTS[k]) for k in cs.ACCOUNT_FIELDS}
    out["password"] = _mask_pw(out.get("password"))
    return jsonify(out)


@app.post("/api/config")
def post_config():
    incoming = request.json or {}
    cfg = cs.load_config()
    for k in cs.ACCOUNT_FIELDS:
        if k not in incoming:
            continue
        val = incoming[k]
        if k == "password" and val == "********":
            continue
        if (val is None or val == "") and (cfg.get(k) or "") != "":
            continue
        cfg[k] = val
    if not save_config(cfg):
        return jsonify({"ok": False, "error": "config folder not writable — see log"}), 500
    log("account settings saved.")
    restart_go2rtc()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# cameras
# --------------------------------------------------------------------------- #
def _go2rtc_streams() -> dict:
    try:
        r = requests.get(f"{GO2RTC_API}/api/streams", timeout=4)
        return r.json() if r.ok else {}
    except Exception:  # noqa: BLE001
        return {}


def _stream_status(streams: dict, name: str) -> dict:
    info = streams.get(name) or {}
    prods = info.get("producers") or []
    codec, recv = "", 0
    for p in prods:
        try:
            recv += int(p.get("recv") or 0)
        except (TypeError, ValueError):
            pass
        for m in (p.get("medias") or []):
            ml = str(m).lower()
            if "h265" in ml or "265" in ml:
                codec = "H.265"
            elif "h264" in ml or "264" in ml:
                codec = codec or "H.264"
    return {"stream_up": bool(prods), "codec": codec, "recv": recv}


@app.get("/api/cameras")
def list_cameras():
    cfg = cs.load_config()
    streams = _go2rtc_streams()
    cams = []
    for c in cfg.get("cameras") or []:
        st = _stream_status(streams, c["name"])
        cams.append({
            "name": c["name"],
            "camera_dsn": c.get("camera_dsn", ""),
            "uid": c.get("uid", ""),
            "authkey": c.get("authkey", ""),
            "av_password": _mask_pw(c.get("av_password")),
            "av_security_mode": c.get("av_security_mode", ""),
            "have_key": bool(c.get("uid") and c.get("authkey")),
            "busy": c["name"] in STATE["cam_busy"],
            **st,
        })
    return jsonify({
        "cameras": cams,
        "rtsp_port": PUBLIC_RTSP_PORT, "http_port": PUBLIC_HTTP_PORT,
        "webrtc_port": PUBLIC_WEBRTC_PORT,
    })


@app.post("/api/cameras")
def add_camera():
    body = request.json or {}
    dsn = (body.get("camera_dsn") or "").strip()
    name = body.get("name") or dsn or cs.DEFAULT_CAM_NAME
    cfg = cs.load_config()
    used = {c["name"] for c in cfg.get("cameras") or []}
    slug = cs.slugify(name)
    # de-dup the slug against existing cameras
    final = slug
    i = 2
    while final in used:
        final = f"{slug}-{i}"
        i += 1
    cam = dict(cs.CAMERA_DEFAULTS, name=final, camera_dsn=dsn)
    cfg.setdefault("cameras", []).append(cam)
    if not save_config(cfg):
        return jsonify({"ok": False, "error": "config folder not writable — see log"}), 500
    log(f"[{final}] camera added (DSN {dsn or '—'}).")
    if dsn:
        _start_camera_diagnose(final)
    return jsonify({"ok": True, "name": final})


@app.post("/api/cameras/<name>")
def update_camera(name):
    body = request.json or {}
    cfg = cs.load_config()
    cam = cs.find_camera(cfg, name)
    if not cam:
        return jsonify({"ok": False, "error": "no such camera"}), 404
    for k in ("camera_dsn", "uid", "authkey", "av_password", "av_security_mode", "name"):
        if k not in body:
            continue
        val = body[k]
        if k == "av_password" and val == "********":
            continue
        if k == "name":
            val = cs.slugify(val, cam["name"])
        cam[k] = val
    if not save_config(cfg):
        return jsonify({"ok": False, "error": "config folder not writable — see log"}), 500
    log(f"[{cam['name']}] settings saved.")
    restart_go2rtc()
    return jsonify({"ok": True, "name": cam["name"]})


@app.delete("/api/cameras/<name>")
def delete_camera(name):
    cfg = cs.load_config()
    before = len(cfg.get("cameras") or [])
    cfg["cameras"] = [c for c in cfg.get("cameras") or [] if c.get("name") != name]
    if len(cfg["cameras"]) == before:
        return jsonify({"ok": False, "error": "no such camera"}), 404
    if not save_config(cfg):
        return jsonify({"ok": False, "error": "config folder not writable — see log"}), 500
    # tidy up the camera's generated env + log (best effort)
    for p in (os.path.join(cs.CAM_DIR, f"{name}.env"),
              os.path.join(cs.CONFIG_DIR, f"tutk-{name}.log")):
        try:
            os.remove(p)
        except OSError:
            pass
    log(f"[{name}] camera removed.")
    restart_go2rtc()
    return jsonify({"ok": True})


def _start_camera_diagnose(name: str):
    if name in STATE["cam_busy"]:
        return
    threading.Thread(target=_camera_diagnose_worker, args=(name,), daemon=True).start()


def _camera_diagnose_worker(name: str):
    STATE["cam_busy"].add(name)
    try:
        from owlet_api import OwletAPI
        cfg = cs.load_config()
        cam = cs.find_camera(cfg, name)
        if not cam:
            return
        if not cfg.get("email") or not cfg.get("password"):
            log(f"[{name}] enter your Owlet email + password (account card) first.")
            return
        dsn = (cam.get("camera_dsn") or "").strip()
        if not dsn:
            log(f"[{name}] set this camera's DSN first.")
            return
        log(f"[{name}] === connecting & fetching camera key ===")
        api = OwletAPI(cfg["region"], cfg["email"], cfg["password"], log=log)
        try:
            creds = api.camera_credentials(dsn)
        except Exception as e:  # noqa: BLE001
            log(f"[{name}] could not fetch camera key: {e}")
            return
        # re-load in case the user edited meanwhile, then write our creds
        cfg = cs.load_config()
        cam = cs.find_camera(cfg, name)
        if not cam:
            return
        cam["uid"] = creds["uid"]
        cam["authkey"] = creds.get("authkey") or ""
        if creds.get("av_password"):
            cam["av_password"] = creds["av_password"]
        if save_config(cfg):
            log(f"[{name}] camera key saved (uid={creds['uid']}); (re)starting stream.")
            restart_go2rtc()
    finally:
        STATE["cam_busy"].discard(name)


@app.post("/api/cameras/<name>/diagnose")
def diagnose_camera(name):
    cfg = cs.load_config()
    if not cs.find_camera(cfg, name):
        return jsonify({"ok": False, "error": "no such camera"}), 404
    _start_camera_diagnose(name)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# account login test (lists Ayla devices — handy for confirming credentials)
# --------------------------------------------------------------------------- #
def _diagnose_worker(cfg: dict):
    STATE["busy"] = True
    try:
        from owlet_api import OwletAPI, OwletError
        if not cfg.get("email") or not cfg.get("password"):
            log("!! enter your Owlet email + password first.")
            return
        api = OwletAPI(cfg["region"], cfg["email"], cfg["password"], log=log)
        try:
            res = api.diagnose()
            STATE["candidates"] = res["candidates"]
            STATE["devices"] = res["devices"]
            log("[login] OK — credentials accepted. Add your camera(s) by DSN below.")
        except OwletError as e:
            log(f"!! login failed: {e}")
        except Exception as e:  # noqa: BLE001
            log(f"!! unexpected error: {e}")
    finally:
        STATE["busy"] = False


@app.post("/api/diagnose")
def diagnose():
    if STATE["busy"]:
        return jsonify({"error": "already running"}), 409
    cfg = cs.load_config()
    incoming = request.json or {}
    for k in ("region", "email", "password"):
        if incoming.get(k) and incoming[k] != "********":
            cfg[k] = incoming[k]
    save_config(cfg)
    LOG.clear()
    log("=== Owlet login test ===")
    threading.Thread(target=_diagnose_worker, args=(cfg,), daemon=True).start()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Owlet sensors / vitals (Smart Sock + Cam room sensors)
# --------------------------------------------------------------------------- #
VITALS_DIR = os.path.join(cs.CONFIG_DIR, "vitals")
VITALS_CACHE = os.path.join(VITALS_DIR, "snapshot.json")


def _vitals_worker(cfg: dict):
    STATE["busy"] = True
    try:
        from owlet_api import OwletAPI, OwletError
        from owlet_vitals import OwletVitals, unit_for
        if not cfg.get("email") or not cfg.get("password"):
            log("!! enter your Owlet email + password first.")
            return
        api = OwletAPI(cfg["region"], cfg["email"], cfg["password"], log=log)
        vit = OwletVitals(api, log=log)
        try:
            snap = vit.snapshot()
        except OwletError as e:
            log(f"!! login failed: {e}")
            return
        except Exception as e:  # noqa: BLE001
            log(f"!! vitals error: {e}")
            return
        log(f"[vitals] {len(snap)} Ayla device(s) on the account")
        for d in snap:
            log(f"\n  ── {d['kind'].upper()}  dsn={d['dsn']}  model={d['model']}  ({d['name']})")
            if d["sensors"]:
                pretty = ", ".join(
                    f"{k}={v}{unit_for(k)}" for k, v in d["sensors"].items() if v is not None)
                log(f"     sensors: {pretty or '(all null — sock may be asleep)'}")
            else:
                log("     sensors: none recognized")
            log(f"     all properties ({len(d['raw_props'])}):")
            for name in d["raw_props"]:
                val = d["raw_props"][name]
                sval = str(val)
                if len(sval) > 120:
                    sval = sval[:117] + "…"
                log(f"        {name} = {sval}")
        try:
            os.makedirs(VITALS_DIR, exist_ok=True)
            with open(VITALS_CACHE, "w") as f:
                import json as _json
                _json.dump({"ts": time.time(), "devices": snap}, f)
        except Exception as e:  # noqa: BLE001
            log(f"[vitals] cache write failed: {e}")
        log("\n[vitals] done — copy the property names above and we'll map them.")
    finally:
        STATE["busy"] = False


@app.post("/api/vitals/discover")
def vitals_discover():
    if STATE["busy"]:
        return jsonify({"error": "already running"}), 409
    cfg = cs.load_config()
    if not cfg.get("email") or not cfg.get("password"):
        return jsonify({"ok": False, "error": "no account configured"}), 400
    LOG.clear()
    log("=== Owlet sensor discovery ===")
    threading.Thread(target=_vitals_worker, args=(cfg,), daemon=True).start()
    return jsonify({"ok": True})


CAM_SENSOR_KEYS = ("temperature", "humidity", "noise", "brightness",
                   "motion", "sound", "wifi_rssi")


def _cam_sensor_devices() -> list[dict]:
    """Live room sensors per camera, written by tutk_client from the TUTK
    stream (temp/noise/motion/sound from frame info; humidity/brightness from
    the GetRealtimeData IOCTL)."""
    import json as _json
    out = []
    cfg = cs.load_config()
    for name in cs.camera_names(cfg):
        try:
            with open(cs.cam_sensors_path(name)) as f:
                data = _json.load(f)
        except (FileNotFoundError, ValueError):
            continue
        sensors = {k: data[k] for k in CAM_SENSOR_KEYS if k in data}
        if not sensors:
            continue
        out.append({"dsn": name, "name": name, "model": "Owlet Cam",
                    "kind": "cam", "sensors": sensors, "ts": data.get("ts")})
    return out


# Temperatures are stored raw (°C); the API serves US/Imperial (°F) so the web
# UI and the native app both get °F from one place. ?units=metric for raw °C.
_TEMP_KEYS = ("temperature", "skin_temperature")


def _convert_units(devices: list[dict], units: str) -> None:
    if units == "metric":
        return
    for d in devices:
        s = d.get("sensors") or {}
        for k in _TEMP_KEYS:
            if s.get(k) is not None:
                try:
                    s[k] = round(s[k] * 9 / 5 + 32)
                except (TypeError, ValueError):
                    pass


@app.get("/api/vitals")
def vitals_latest():
    """Live sock vitals + cam room sensors for the web UI and the native app.

    Shape: {ts, units, devices:[{dsn,name,kind,model,ts,sensors:{...}}]}.
    Temperatures are °F by default (US); pass ?units=metric for °C.
    """
    import json as _json
    units = (request.args.get("units") or "us").lower()
    payload = {"ts": None, "devices": []}
    try:
        with open(VITALS_CACHE) as f:
            payload = _json.load(f)
    except (FileNotFoundError, ValueError):
        pass
    except Exception as e:  # noqa: BLE001
        payload = {"ts": None, "devices": [], "error": str(e)}
    # Merge live cam room-sensors (these update continuously off the stream).
    cams = _cam_sensor_devices()
    if cams:
        existing = {d.get("dsn") for d in payload.get("devices", [])}
        payload.setdefault("devices", [])
        payload["devices"] += [c for c in cams if c["dsn"] not in existing]
    _convert_units(payload.get("devices", []), units)
    payload["units"] = "metric" if units == "metric" else "us"
    return jsonify(payload)


@app.get("/api/logs")
def logs():
    def gen():
        last = 0
        while True:
            cur = list(LOG)
            for ln in cur[last:]:
                yield f"data: {ln}\n\n"
            last = len(cur)
            time.sleep(0.4)
    return Response(gen(), mimetype="text/event-stream")


# --------------------------------------------------------------------------- #
# LAN camera discovery (Kalay UDP 63616)
# --------------------------------------------------------------------------- #
DISCOVERY_PORT = 63616
DISCOVERY_PROBES = [
    bytes.fromhex("f1411388"),
    bytes.fromhex("f1300000"),
    b"\x00\x00\x00\x00",
    b"\x01\x00\x00\x00",
    b"TUTK_SEARCH",
]
UID_TOKEN = re.compile(rb"[A-Z0-9]{6,}-?[A-Z0-9]{4,}-?[A-Z0-9]{0,8}")


def _discover_worker(target: str):
    LOG.clear()
    log("=== LAN camera discovery (UDP 63616) ===")
    log("NOTE: this only works if the container is on the same L2 network as the")
    log("camera — run it with `--network host` for broadcast, or give the camera IP.")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)

    dests = []
    if target:
        dests.append(target.strip())
        log(f"probing {target.strip()} directly …")
    dests.append("255.255.255.255")
    log("broadcasting to 255.255.255.255 …")

    for d in dests:
        for p in DISCOVERY_PROBES:
            try:
                s.sendto(p, (d, DISCOVERY_PORT))
            except OSError as e:
                log(f"  send to {d} failed: {e}")

    found: dict[str, list[str]] = {}
    end = time.time() + 8
    while time.time() < end:
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        toks = [t.decode(errors="ignore") for t in UID_TOKEN.findall(data)
                if 12 <= len(t) <= 24]
        log(f"  ◀ response from {addr[0]}:{addr[1]} ({len(data)} bytes) "
            f"hex={data[:32].hex()}")
        if toks:
            log(f"     UID candidate(s): {toks}")
            found.setdefault(addr[0], [])
            for t in toks:
                if t not in found[addr[0]]:
                    found[addr[0]].append(t)
    s.close()

    cands = []
    for ip, uids in found.items():
        for u in uids:
            cands.append({"field": f"LAN {ip} <uid>", "value": u})
    if cands:
        STATE["candidates"] = cands + STATE.get("candidates", [])
        log(f"\n[done] {len(cands)} UID candidate(s) on the LAN.")
    else:
        log("\n[done] No camera responded on UDP 63616.")


@app.post("/api/discover")
def discover():
    if STATE["busy"]:
        return jsonify({"error": "busy"}), 409
    target = (request.json or {}).get("ip", "") if request.is_json else ""

    def run():
        STATE["busy"] = True
        try:
            _discover_worker(target)
        finally:
            STATE["busy"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/findings")
def findings():
    return jsonify({"candidates": STATE["candidates"], "devices": STATE["devices"]})


# --------------------------------------------------------------------------- #
# status / snapshots / stream control
# --------------------------------------------------------------------------- #
def _have_libs() -> bool:
    try:
        from lib_extract import have_all
        return have_all(TUTK_LIB_DIR)
    except Exception:  # noqa: BLE001
        return os.path.exists(os.path.join(TUTK_LIB_DIR, "libIOTCAPIs.so"))


@app.get("/api/status")
def status():
    cfg = cs.load_config()
    cameras = cfg.get("cameras") or []
    streams = _go2rtc_streams()
    with_key = sum(1 for c in cameras if c.get("uid") and c.get("authkey"))
    live = sum(1 for c in cameras if _stream_status(streams, c["name"])["stream_up"])
    return jsonify({
        "have_login": bool(cfg.get("email") and cfg.get("password")),
        "have_libs": _have_libs(),
        "config_writable": os.access(cs.CONFIG_DIR, os.W_OK),
        "busy": STATE["busy"],
        "cameras": len(cameras),
        "cameras_with_key": with_key,
        "streams_live": live,
        "rtsp_port": PUBLIC_RTSP_PORT, "http_port": PUBLIC_HTTP_PORT,
        "webrtc_port": PUBLIC_WEBRTC_PORT,
    })


def _snapshot(name: str) -> Response:
    try:
        r = requests.get(f"{GO2RTC_API}/api/frame.jpeg", params={"src": name}, timeout=8)
        return Response(r.content, status=r.status_code,
                        mimetype=r.headers.get("Content-Type", "image/jpeg"))
    except Exception:  # noqa: BLE001
        return Response(status=502)


@app.get("/img/<name>.jpg")
def img(name):
    return _snapshot(name)


@app.get("/snapshot/<name>.jpg")
def snapshot(name):
    return _snapshot(name)


@app.get("/api/frame.jpeg")
def frame_proxy():
    """Back-compat snapshot of the primary camera."""
    name = cs.camera_names()[0]
    return _snapshot(name)


@app.post("/api/extract_libs")
def extract_libs_ep():
    from lib_extract import provision
    search = [os.environ.get("OWLET_APK_DIR"), cs.CONFIG_DIR, "/config", "/app/libs", "/apk"]
    ok, msg = provision(TUTK_LIB_DIR, search, log=log)
    log(f"[libs] {msg}")
    return jsonify({"ok": ok, "message": msg})


@app.post("/api/upload_apk")
def upload_apk():
    """Accept an uploaded Owlet .apk/.apkm/.xapk and extract the TUTK libs from it."""
    from werkzeug.utils import secure_filename
    from lib_extract import provision
    f = request.files.get("apk")
    if not f or not f.filename:
        return jsonify({"ok": False, "message": "no file received"}), 400
    name = secure_filename(f.filename) or "owlet-upload.apkm"
    dest_dir = cs.CONFIG_DIR
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        f.save(dest)
    except OSError as e:  # noqa: BLE001
        log(f"[upload] cannot save: {e}")
        return jsonify({"ok": False,
                        "message": f"cannot save upload ({e.strerror}) — is /config writable?"}), 500
    log(f"[upload] received {name} ({os.path.getsize(dest)//1024} KB); extracting libraries…")
    ok, msg = provision(TUTK_LIB_DIR, [dest_dir, "/config", "/app/libs"], log=log)
    log(f"[libs] {msg}")
    return jsonify({"ok": ok, "message": msg})


def restart_go2rtc():
    try:
        requests.post(f"{GO2RTC_API}/api/restart", timeout=4)
        log("requested go2rtc restart.")
    except Exception as e:  # noqa: BLE001
        log(f"restart error: {e}")


@app.post("/api/stream/restart")
def restart_stream():
    restart_go2rtc()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# talk-back + sound playback (send audio TO the camera speaker)
# --------------------------------------------------------------------------- #
PLAY_PROCS: dict = {}          # camera -> the current talk/play ffmpeg process
_PLAY_LOCK = threading.Lock()
SOUND_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")


def _ffmpeg_to_speaker(camera: str, input_args: list) -> tuple[bool, str]:
    """Transcode an audio source to AAC-LC 8kHz mono ADTS and stream it (paced to
    real time with -re) into the camera's talk FIFO; tutk_client plays it out the
    speaker. Any previous playback for that camera is interrupted."""
    fifo = cs.talk_fifo_path(camera)
    if not os.path.exists(fifo):
        return False, "camera isn't streaming yet — start its stream first"
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error"] + input_args
           + ["-ac", "1", "-ar", "8000", "-c:a", "aac", "-b:a", "24k", "-f", "adts", fifo])
    with _PLAY_LOCK:
        old = PLAY_PROCS.get(camera)
        if old and old.poll() is None:
            try:
                old.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            PLAY_PROCS[camera] = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            return False, f"ffmpeg failed: {e}"
    return True, "playing"


@app.get("/api/sounds")
def list_sounds():
    try:
        files = sorted(f for f in os.listdir(cs.SOUNDS_DIR)
                       if f.lower().endswith(SOUND_EXTS))
    except OSError:
        files = []
    return jsonify({"sounds": files})


@app.post("/api/sounds")
def upload_sound():
    from werkzeug.utils import secure_filename
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "no file received"}), 400
    name = secure_filename(f.filename)
    if not name.lower().endswith(SOUND_EXTS):
        return jsonify({"ok": False, "error": "audio files only (mp3/wav/m4a/aac/ogg)"}), 400
    try:
        os.makedirs(cs.SOUNDS_DIR, exist_ok=True)
        f.save(os.path.join(cs.SOUNDS_DIR, name))
    except OSError as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"can't save ({e.strerror}) — is /config writable?"}), 500
    log(f"[sounds] uploaded {name}")
    return jsonify({"ok": True, "name": name})


@app.delete("/api/sounds/<path:fname>")
def delete_sound(fname):
    from werkzeug.utils import secure_filename
    try:
        os.remove(os.path.join(cs.SOUNDS_DIR, secure_filename(fname)))
    except OSError:
        pass
    return jsonify({"ok": True})


@app.post("/api/play/<camera>")
def play_sound(camera):
    from werkzeug.utils import secure_filename
    fname = secure_filename((request.json or {}).get("file", ""))
    path = os.path.join(cs.SOUNDS_DIR, fname)
    if not fname or not os.path.exists(path):
        return jsonify({"ok": False, "error": "sound not found"}), 404
    ok, msg = _ffmpeg_to_speaker(camera, ["-re", "-i", path])
    log(f"[play] {camera}: {fname} -> {msg}")
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)


@app.post("/api/talk/<camera>")
def talk(camera):
    """Play a recorded mic clip out the camera speaker (hold-to-talk)."""
    f = request.files.get("audio")
    if not f:
        return jsonify({"ok": False, "error": "no audio received"}), 400
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".dat")
    f.save(tmp.name)
    tmp.close()
    ok, msg = _ffmpeg_to_speaker(camera, ["-re", "-i", tmp.name])

    # clean the temp once ffmpeg is done with it
    def _cleanup():
        p = PLAY_PROCS.get(camera)
        if p:
            try:
                p.wait(timeout=60)
            except Exception:  # noqa: BLE001
                pass
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    threading.Thread(target=_cleanup, daemon=True).start()
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)


@app.post("/api/talk/<camera>/stop")
def talk_stop(camera):
    with _PLAY_LOCK:
        p = PLAY_PROCS.get(camera)
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
    return jsonify({"ok": True})


@app.get("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    # Seed defaults + generate an initial go2rtc config only when nothing exists
    # yet — never overwrite an existing config (it may hold camera creds).
    if not os.path.exists(cs.CONFIG_PATH):
        if not save_config(cs.load_config()):
            log("starting the UI anyway so you can fix the config-folder permission.")
    app.run(host="0.0.0.0", port=int(os.environ.get("UI_PORT", "8088")), threaded=True)
