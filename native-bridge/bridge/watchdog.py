#!/usr/bin/env python3
"""
watchdog.py — self-heal a wedged Owlet P2P / RTSP session.

go2rtc (PID 1) and the TUTK exec source can wind up "connected" but delivering
ZERO frames — an expired cloud token, a stalled Kalay session, a half-open P2P
socket. go2rtc stays alive, so Docker never restarts the container and the RTSP
stream just hangs (Frigate/viewers see a dead camera). Nothing recovers it on
its own but a restart.

This watchdog ffprobes each camera's local RTSP output on an interval. If a
camera serves no video for OWLET_WATCHDOG_STALL seconds straight, it kills
go2rtc (PID 1) so the container exits and Docker's restart policy brings it back
with a fresh login + fresh KMS creds. Recovers from any wedge cause, provable or
not.

  OWLET_WATCHDOG=0            disable entirely
  OWLET_WATCHDOG_STALL=180    seconds of no-frames before a restart
  OWLET_WATCHDOG_INTERVAL=60  seconds between probes
  OWLET_WATCHDOG_GRACE=150    startup grace before the first probe

`python3 watchdog.py --check` probes once and exits 0 (all cameras healthy, or
none configured yet) / 1 (a camera is dead) — used by the Docker HEALTHCHECK.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import config_store as cs

STALL = int(os.environ.get("OWLET_WATCHDOG_STALL", "180"))
INTERVAL = int(os.environ.get("OWLET_WATCHDOG_INTERVAL", "60"))
GRACE = int(os.environ.get("OWLET_WATCHDOG_GRACE", "150"))
PROBE_TIMEOUT = 15


def log(m: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[watchdog]", m, flush=True)


def _alive(name: str) -> bool:
    """True if the camera's local RTSP is currently serving video frames."""
    url = f"rtsp://127.0.0.1:{cs.G_RTSP}/{name}"
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
             "-rw_timeout", "12000000", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", url],
            capture_output=True, timeout=PROBE_TIMEOUT,
        )
        return r.returncode == 0 and b"video" in r.stdout
    except Exception:  # noqa: BLE001
        return False


def _names() -> list[str]:
    try:
        return list(cs.camera_names())
    except Exception:  # noqa: BLE001
        return []


def _check_once() -> int:
    names = _names()
    if not names:
        return 0  # nothing configured yet -> report healthy
    return 1 if any(not _alive(n) for n in names) else 0


def _restart() -> None:
    # go2rtc is PID 1 (start-bionic.sh execs it); killing it exits the container
    # so Docker's restart policy relaunches with a fresh login + fresh KMS creds.
    log("killing go2rtc (PID 1) so Docker restarts the container with fresh creds")
    try:
        subprocess.run(["pkill", "-TERM", "-x", "go2rtc"], timeout=10)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(6)
    try:  # belt-and-suspenders if go2rtc wasn't PID 1 for some reason
        os.kill(1, 15)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    if os.environ.get("OWLET_WATCHDOG", "1") == "0":
        return
    time.sleep(GRACE)
    bad_since: dict[str, float] = {}
    while True:
        now = time.monotonic()
        names = _names()
        for name in names:
            if _alive(name):
                if name in bad_since:
                    log(f"{name}: recovered")
                bad_since.pop(name, None)
                continue
            since = bad_since.setdefault(name, now)
            dead = int(now - since)
            log(f"{name}: no video for {dead}s (restart at {STALL}s)")
            if now - since >= STALL:
                log(f"{name} stalled >= {STALL}s -> self-healing restart")
                _restart()
                return
        # forget cameras removed from the config
        for gone in [n for n in list(bad_since) if n not in names]:
            bad_since.pop(gone, None)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(_check_once())
    main()
