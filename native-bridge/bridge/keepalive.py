#!/usr/bin/env python3
"""
keepalive.py — hold one persistent internal RTSP viewer per camera.

The Owlet cam allows only ONE P2P session, and go2rtc tears an on-demand source
down when the last viewer leaves — so a viewer (Frigate/VLC) reconnecting could
briefly find the camera's slot empty and fail. We keep exactly one internal
consumer per camera alive so connect/disconnect by real viewers never touches the
camera session; everyone shares the one warm stream.

This supervisor re-reads the camera list every few seconds, so cameras you add or
remove in the web UI get (or lose) their keepalive without a container restart.
Set OWLET_KEEPALIVE=0 to disable entirely.
"""

from __future__ import annotations

import subprocess
import time

import config_store as cs


def _spawn(name: str) -> subprocess.Popen:
    url = f"rtsp://127.0.0.1:{cs.G_RTSP}/{name}"
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
         "-rw_timeout", "15000000",   # bail if the stream stalls (don't hang the warm viewer)
         "-i", url, "-c", "copy", "-f", "mpegts", "/dev/null"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main() -> None:
    time.sleep(8)  # let webapp + go2rtc come up first
    procs: dict[str, subprocess.Popen] = {}
    started: dict[str, float] = {}   # name -> spawn time
    backoff: dict[str, float] = {}   # name -> seconds to wait before next respawn
    nextok: dict[str, float] = {}    # name -> earliest respawn time
    while True:
        now = time.monotonic()
        try:
            want = set(cs.camera_names())
        except Exception:  # noqa: BLE001
            want = set(procs)  # keep what we have if the config can't be read
        # start cameras that are missing or whose keepalive died, with backoff so
        # a cam that's offline/contended isn't hammered with a respawn every 5s.
        for name in want:
            p = procs.get(name)
            if p is not None and p.poll() is None:
                continue
            if p is not None:  # it died — was it short-lived?
                ran = now - started.get(name, now)
                if ran < 20:
                    backoff[name] = min((backoff.get(name) or 5) * 2, 60)
                else:
                    backoff[name] = 0  # healthy run -> reset
                nextok[name] = now + backoff[name]
            if now < nextok.get(name, 0):
                continue
            try:
                procs[name] = _spawn(name)
                started[name] = now
            except Exception:  # noqa: BLE001
                pass
        # stop keepalives for cameras that were removed
        for name in list(procs):
            if name not in want:
                try:
                    procs[name].terminate()
                except Exception:  # noqa: BLE001
                    pass
                procs.pop(name, None)
                started.pop(name, None); backoff.pop(name, None); nextok.pop(name, None)
        time.sleep(5)


if __name__ == "__main__":
    main()
