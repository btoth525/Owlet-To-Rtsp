"""
owlet_addon.py — mitmproxy addon that captures the Owlet app's cloud-auth flow
and surfaces the ThroughTek/Kalay (TUTK) credentials the native bridge needs.

This reads YOUR app's traffic to YOUR account/camera for interoperability.

Run:
    mitmdump -s owlet_addon.py
    # or with the bundled web UI:
    mitmweb -s owlet_addon.py

Output:
    ./captures/flows.log         human-readable log of relevant requests
    ./captures/<host>-<ts>.json  full request+response bodies for matched hosts
    Console: highlighted CREDENTIAL CANDIDATES (UID / AuthKey / etc.)

The hinge: a successful login should, somewhere in the device-list /
camera-credential calls, return a Kalay UID (often 20 chars) plus an AuthKey
(DTLS key) and/or a legacy enc/init string. Those are what we feed to TUTK.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from mitmproxy import http

CAP_DIR = os.environ.get("OWLET_CAP_DIR", "captures")
os.makedirs(CAP_DIR, exist_ok=True)

# Hosts we care about. Owlet's cloud + anything ThroughTek/Kalay.
HOST_PATTERNS = [
    r"owlet", r"owletcare", r"owletdata",
    r"throughtek", r"tutk", r"kalay", r"iotcplatform",
    r"amazonaws",          # Owlet backends sit behind AWS; noisy but useful
]

# JSON keys (case-insensitive) that likely hold the credentials we need.
KEY_PATTERNS = [
    "uid", "authkey", "auth_key", "p2p", "kalay", "tutk",
    "initstring", "init_string", "enckey", "enc_key", "secret",
    "license", "dtls", "psk", "credential", "device_id", "serial",
    "token", "access_token", "id_token", "refresh_token", "password",
]

HOST_RE = re.compile("|".join(HOST_PATTERNS), re.I)
KEY_RE = re.compile("|".join(re.escape(k) for k in KEY_PATTERNS), re.I)

# A Kalay UID is commonly 20 uppercase alphanumerics; AuthKey often base64-ish.
UID_RE = re.compile(r"\b[A-Z0-9]{20}\b")


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _log(line: str) -> None:
    with open(os.path.join(CAP_DIR, "flows.log"), "a") as fh:
        fh.write(line + "\n")


def _walk(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    """Recursively yield (json_path, value) for interesting leaf keys."""
    hits: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            if KEY_RE.search(str(k)):
                if not isinstance(v, (dict, list)):
                    hits.append((here, v))
            hits.extend(_walk(v, here))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(_walk(v, f"{path}[{i}]"))
    return hits


def _try_json(raw: bytes) -> Any | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def response(flow: http.HTTPFlow) -> None:
    host = flow.request.pretty_host
    if not HOST_RE.search(host):
        return

    req = flow.request
    resp = flow.response
    line = f"[{_ts()}] {req.method} {req.pretty_url} -> {resp.status_code if resp else '?'}"
    _log(line)
    print("\033[36m" + line + "\033[0m")

    # Dump the full flow for offline analysis.
    record = {
        "ts": _ts(),
        "method": req.method,
        "url": req.pretty_url,
        "req_headers": dict(req.headers),
        "req_body": req.get_text(strict=False),
        "status": resp.status_code if resp else None,
        "resp_headers": dict(resp.headers) if resp else {},
        "resp_body": resp.get_text(strict=False) if resp else None,
    }
    safe_host = re.sub(r"[^a-zA-Z0-9._-]", "_", host)
    with open(os.path.join(CAP_DIR, f"{safe_host}-{_ts()}.json"), "w") as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)

    # Surface credential candidates from both request and response bodies.
    candidates: list[tuple[str, Any]] = []
    for body in (req.get_text(strict=False), resp.get_text(strict=False) if resp else None):
        parsed = _try_json((body or "").encode())
        if parsed is not None:
            candidates.extend(_walk(parsed))
        # Also scan raw text for bare 20-char UIDs even if not in JSON.
        if body:
            for m in set(UID_RE.findall(body)):
                candidates.append(("<bare-uid-match>", m))

    if candidates:
        print("\033[33m  CREDENTIAL CANDIDATES:\033[0m")
        for p, v in candidates:
            sval = str(v)
            if len(sval) > 120:
                sval = sval[:117] + "..."
            print(f"    \033[32m{p}\033[0m = {sval}")
            _log(f"    CANDIDATE {p} = {sval}")


def load(loader):  # noqa: D401  (mitmproxy hook)
    print(f"[owlet_addon] capturing to ./{CAP_DIR}/  — hosts matching: {HOST_PATTERNS}")
