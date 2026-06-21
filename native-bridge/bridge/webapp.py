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
