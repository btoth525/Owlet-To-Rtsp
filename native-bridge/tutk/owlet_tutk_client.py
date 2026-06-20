#!/usr/bin/env python3
"""
owlet_tutk_client.py — native Owlet/Kalay stream puller (the wyze-bridge pattern).

Connects to the Owlet camera over ThroughTek/Kalay using the TUTK SDK libs
extracted from the app, authenticates, starts the video stream, and writes the
raw H.264 elementary stream to stdout. go2rtc reads stdout and serves
RTSP/WebRTC/HLS + a web UI (see go2rtc.yaml) — exactly like docker-wyze-bridge.

    UID + AuthKey come from owlet_auth.py (your cloud login).
    Then:  python3 owlet_tutk_client.py | <go2rtc reads this>

Connection modes (TUTK): LAN -> P2P -> relay, tried in that order by the SDK.

==================== THREE OWLET-SPECIFIC VALUES ====================
Everything here is generic TUTK EXCEPT three constants that are vendor-specific
and must be filled in ONCE from the capture step (native-bridge/mitm):

  1. AV_ACCOUNT / AV_PASSWORD  — the view credentials passed to avClientStart2.
     Often a fixed string or a salted hash of an app secret. Get them from the
     hook-tutk-ioctl.js dump of avClientStart2 args.
  2. IOTYPE_START / START_PAYLOAD — the avSendIOCtrl command that tells the
     camera to begin streaming. Get the type (0x....) and payload bytes from
     the hook-tutk-ioctl.js dump of avSendIOCtrl.
  3. CODEC handling — confirm the frame codec id (H264 vs H265) from the
     frame-info struct; default assumes H264.
====================================================================
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import time
from ctypes import (CDLL, POINTER, byref, c_char, c_char_p, c_int, c_uint,
                    c_ubyte, create_string_buffer)

LIB_DIR = os.environ.get("TUTK_LIB_DIR", os.path.join(os.path.dirname(__file__), "libs", "arm64-v8a"))

# ---- TODO(capture): fill from native-bridge/mitm dumps -----------------
AV_ACCOUNT = os.environ.get("OWLET_AV_ACCOUNT", "admin").encode()
AV_PASSWORD = os.environ.get("OWLET_AV_PASSWORD", "").encode()
IOTYPE_START = int(os.environ.get("OWLET_IOTYPE_START", "0x01FF"), 0)
START_PAYLOAD = bytes.fromhex(os.environ.get("OWLET_START_PAYLOAD_HEX", "00000000"))
# ------------------------------------------------------------------------

FRAME_BUF = 512 * 1024
FRAMEINFO_BUF = 64


def log(*a):
    print("[tutk]", *a, file=sys.stderr, flush=True)


def load_libs() -> tuple[CDLL, CDLL]:
    # Load IOTC first (AV depends on it). Names can vary; adjust if needed.
    iotc_path = os.path.join(LIB_DIR, "libIOTCAPIs.so")
    av_path = os.path.join(LIB_DIR, "libAVAPIs.so")
    for p in (iotc_path, av_path):
        if not os.path.exists(p):
            log(f"MISSING {p} — run tutk/extract-libs.sh against the Owlet APK first.")
            sys.exit(2)
    iotc = CDLL(iotc_path)
    av = CDLL(av_path)
    return iotc, av


def connect_and_stream(uid: str, authkey: str | None) -> None:
    iotc, av = load_libs()

    # ---- IOTC init + connect ------------------------------------------
    if iotc.IOTC_Initialize2(0) < 0:
        log("IOTC_Initialize2 failed"); sys.exit(3)
    av.avInitialize(4)

    sid = iotc.IOTC_Get_SessionID()
    if sid < 0:
        log("IOTC_Get_SessionID failed"); sys.exit(3)

    log(f"connecting to UID {uid} ...")
    # Newer Kalay uses the AuthKey/DTLS variant; fall back to plain parallel.
    session = -1
    if authkey and hasattr(iotc, "IOTC_Connect_ByUID_Parallel2"):
        session = iotc.IOTC_Connect_ByUID_Parallel2(
            c_char_p(uid.encode()), sid, c_char_p(authkey.encode()))
    else:
        session = iotc.IOTC_Connect_ByUID_Parallel(c_char_p(uid.encode()), sid)
    if session < 0:
        log(f"IOTC_Connect_ByUID_Parallel failed: {session}"); sys.exit(4)
    log(f"IOTC session {session} established")

    # ---- AV client start ----------------------------------------------
    serv_type = c_uint(0)
    resend = c_uint(1)
    av_index = av.avClientStart2(
        c_int(session), c_char_p(AV_ACCOUNT), c_char_p(AV_PASSWORD),
        c_uint(20), byref(serv_type), c_ubyte(0), byref(resend))
    if av_index < 0:
        log(f"avClientStart2 failed: {av_index} "
            "(check AV_ACCOUNT/AV_PASSWORD from the capture)")
        sys.exit(5)
    log(f"AV channel {av_index} started (servType={serv_type.value})")

    # ---- tell the camera to start streaming (vendor IOCTL) ------------
    av.avSendIOCtrl(c_int(av_index), c_uint(IOTYPE_START),
                    c_char_p(START_PAYLOAD), c_int(len(START_PAYLOAD)))
    log(f"sent start IOCTL type=0x{IOTYPE_START:04x} len={len(START_PAYLOAD)}")

    # ---- receive frames -> stdout (raw H.264) -------------------------
    buf = create_string_buffer(FRAME_BUF)
    finfo = create_string_buffer(FRAMEINFO_BUF)
    actual = c_int(0)
    expected = c_int(0)
    finfo_len = c_int(0)
    frmno = c_int(0)
    out = sys.stdout.buffer
    frames = 0
    while True:
        rc = av.avRecvFrameData2(
            c_int(av_index), buf, c_int(FRAME_BUF),
            byref(actual), byref(expected),
            finfo, c_int(FRAMEINFO_BUF), byref(finfo_len), byref(frmno))
        if rc >= 0 and actual.value > 0:
            out.write(buf.raw[:actual.value])
            out.flush()
            frames += 1
            if frames % 150 == 0:
                log(f"{frames} frames forwarded")
        elif rc == -20012:        # AV_ER_DATA_NOREADY
            time.sleep(0.01)
        elif rc in (-20015, -20016):  # remote stopped / timeout
            log(f"stream ended rc={rc}"); break
        else:
            time.sleep(0.02)

    av.avClientStop(c_int(av_index))
    iotc.IOTC_Session_Close(c_int(session))


def main() -> None:
    uid = os.environ.get("OWLET_UID")
    authkey = os.environ.get("OWLET_AUTHKEY")
    if not uid:
        # Resolve from the cloud using owlet_auth (your login).
        try:
            from owlet_auth import get_camera_credentials
            creds = get_camera_credentials()
            uid = creds["uid"]
            authkey = creds.get("authkey")
        except Exception as e:  # noqa: BLE001
            log(f"no OWLET_UID set and owlet_auth failed: {e}")
            sys.exit(1)
    while True:
        try:
            connect_and_stream(uid, authkey)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"error: {e}; reconnecting in 5s")
        time.sleep(5)


if __name__ == "__main__":
    main()
