#!/usr/bin/env python3
"""
tutk_client.py — connect to the Owlet camera over TUTK/Kalay and write raw H.264
to stdout for go2rtc. Uses the STANDARD Kalay control protocol:

  IOTYPE_USER_IPCAM_START (0x01FF) + SMsgAVIoctrlAVStream{ channel; reserved[4] }

which is the documented start-video command shared across TUTK IP cameras (the
Kalay sample app, wyzecam, etc.) — so this is not a guess.

Config comes from the web UI (/config/owlet.env) or env vars:
  OWLET_UID, OWLET_AUTHKEY, OWLET_AV_ACCOUNT, OWLET_AV_PASSWORD,
  OWLET_IOTYPE_START (default 0x01FF), OWLET_AV_CHANNEL (default 0)

Needs the TUTK .so libs (libIOTCAPIs.so, libAVAPIs.so) in TUTK_LIB_DIR.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import time
from ctypes import CDLL, byref, c_char_p, c_int, c_ubyte, c_uint, create_string_buffer

LIB_DIR = os.environ.get("TUTK_LIB_DIR", "/app/libs/x86_64")

UID = os.environ.get("OWLET_UID", "")
AUTHKEY = os.environ.get("OWLET_AUTHKEY", "")
AV_ACCOUNT = os.environ.get("OWLET_AV_ACCOUNT", "admin").encode()
AV_PASSWORD = os.environ.get("OWLET_AV_PASSWORD", "").encode()
IOTYPE_START = int(os.environ.get("OWLET_IOTYPE_START", "0x01FF"), 0)
AV_CHANNEL = int(os.environ.get("OWLET_AV_CHANNEL", "0"))

FRAME_BUF = 1024 * 1024
FINFO_BUF = 64
AV_ER_DATA_NOREADY = -20012
AV_ER_REMOTE_TIMEOUT_DISCONNECT = -20015
AV_ER_SESSION_CLOSE_BY_REMOTE = -20016


def log(*a):
    print("[tutk]", *a, file=sys.stderr, flush=True)


def load() -> tuple[CDLL, CDLL]:
    iotc_p = os.path.join(LIB_DIR, "libIOTCAPIs.so")
    av_p = os.path.join(LIB_DIR, "libAVAPIs.so")
    for p in (iotc_p, av_p):
        if not os.path.exists(p):
            log(f"MISSING {p} — extract the TUTK libs from the Owlet APK (extract-libs.sh).")
            sys.exit(2)
    return CDLL(iotc_p), CDLL(av_p)


def smsg_av_stream(channel: int) -> bytes:
    # struct SMsgAVIoctrlAVStream { unsigned int channel; unsigned char reserved[4]; }
    return struct.pack("<I4s", channel, b"\x00\x00\x00\x00")


def stream_once(uid: str) -> int:
    iotc, av = load()

    if iotc.IOTC_Initialize2(0) < 0:
        log("IOTC_Initialize2 failed"); return 3
    av.avInitialize(4)

    sid = iotc.IOTC_Get_SessionID()
    if sid < 0:
        log("IOTC_Get_SessionID failed"); return 3

    log(f"connecting UID={uid} (authKey={'yes' if AUTHKEY else 'no'}) …")
    # The Owlet app uses IOTC_Connect_ByUIDEx(UID, authKey, ...). Try that with
    # the AuthKey first; fall back to the AuthKey-less parallel connect.
    session = -1
    if AUTHKEY and hasattr(iotc, "IOTC_Connect_ByUIDEx"):
        session = iotc.IOTC_Connect_ByUIDEx(c_char_p(uid.encode()),
                                            c_char_p(AUTHKEY.encode()), None)
    if session < 0:
        session = iotc.IOTC_Connect_ByUID_Parallel(c_char_p(uid.encode()), sid)
    if session < 0:
        log(f"connect failed: {session}"); return 4
    log(f"IOTC session={session}")

    serv = c_uint(0)
    resend = c_uint(1)
    av_idx = av.avClientStart2(c_int(session), c_char_p(AV_ACCOUNT), c_char_p(AV_PASSWORD),
                               c_uint(20), byref(serv), c_ubyte(AV_CHANNEL), byref(resend))
    if av_idx < 0:
        log(f"avClientStart2 failed: {av_idx} (check AV account/password)"); return 5
    log(f"AV channel={av_idx} servType={serv.value}")

    payload = smsg_av_stream(AV_CHANNEL)
    av.avSendIOCtrl(c_int(av_idx), c_uint(IOTYPE_START), c_char_p(payload), c_int(len(payload)))
    log(f"sent IOTYPE_START=0x{IOTYPE_START:04x} payload={payload.hex()}")

    buf = create_string_buffer(FRAME_BUF)
    finfo = create_string_buffer(FINFO_BUF)
    actual = c_int(0); expected = c_int(0); finfo_len = c_int(0); frmno = c_int(0)
    out = sys.stdout.buffer
    frames = 0
    last_log = time.time()
    while True:
        rc = av.avRecvFrameData2(c_int(av_idx), buf, c_int(FRAME_BUF),
                                 byref(actual), byref(expected),
                                 finfo, c_int(FINFO_BUF), byref(finfo_len), byref(frmno))
        if rc >= 0 and actual.value > 0:
            out.write(buf.raw[:actual.value]); out.flush()
            frames += 1
            if time.time() - last_log > 10:
                log(f"{frames} frames forwarded"); last_log = time.time()
        elif rc == AV_ER_DATA_NOREADY:
            time.sleep(0.01)
        elif rc in (AV_ER_REMOTE_TIMEOUT_DISCONNECT, AV_ER_SESSION_CLOSE_BY_REMOTE):
            log(f"stream ended rc={rc}"); break
        else:
            time.sleep(0.02)

    av.avClientStop(c_int(av_idx))
    iotc.IOTC_Session_Close(c_int(session))
    av.avDeInitialize()
    iotc.IOTC_DeInitialize()
    return 0


def resolve_uid() -> str:
    global AUTHKEY, AV_PASSWORD
    if UID:
        return UID
    # No UID set — fetch it (+AuthKey +AV password) from the Owlet camera KMS,
    # which needs only the account login and the camera DSN.
    dsn = os.environ.get("OWLET_CAMERA_DSN", "").strip()
    email = os.environ.get("OWLET_EMAIL"); pw = os.environ.get("OWLET_PASSWORD")
    region = os.environ.get("OWLET_REGION", "world")
    if dsn and email and pw:
        try:
            from owlet_api import OwletAPI
            api = OwletAPI(region, email, pw, log=lambda m: log(m))
            creds = api.camera_credentials(dsn)
            if creds.get("authkey"):
                AUTHKEY = creds["authkey"]
            if creds.get("av_password"):
                AV_PASSWORD = creds["av_password"].encode()
            log(f"resolved UID {creds['uid']} from KMS")
            return creds["uid"]
        except Exception as e:  # noqa: BLE001
            log(f"KMS resolve failed: {e}")
    log("No OWLET_UID set and none resolved. Set the Camera DSN + login in the UI.")
    sys.exit(1)


def main() -> None:
    uid = resolve_uid()
    while True:
        try:
            rc = stream_once(uid)
            if rc != 0:
                log(f"exit rc={rc}; retry in 5s")
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"error: {e}; retry in 5s")
        time.sleep(5)


if __name__ == "__main__":
    main()
