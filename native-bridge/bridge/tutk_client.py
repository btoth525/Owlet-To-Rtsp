#!/usr/bin/env python3
"""
tutk_client.py — connect to the Owlet camera over TUTK/Kalay and write raw H.264
to stdout for go2rtc.

The call sequence below is the EXACT one the Owlet Android app uses, recovered by
decompiling `com.owlet.tutk.AndroidTutkSdk` (camera-sdk_release):

    TUTK_SDK_Set_License_Key(<app license key>)
    IOTC_Set_LanSearchPort(63616)
    IOTC_Setup_Session_Alive_Timeout(20)
    TUTK_SDK_Set_Region(3)              # REGION_US
    IOTC_Initialize2(0)
    avInitialize(512)                   # MAX_CAMERAS
    sid = IOTC_Get_SessionID()
    IOTC_Connect_ByUIDEx(uid, sid, St_IOTCConnectInput{authType=0, authKey, timeout=20})
    avClientStart2(sid, "admin", <password>, 20, &servType, 0, &resend)
    avSendIOCtrl(av, 511 /*IOTYPE_USER_IPCAM_START*/, SMsgAVIoctrlAVStream{channel})
    loop: avRecvFrameData2(...) -> stdout

Config comes from the web UI (/config/owlet.env) or env vars:
  OWLET_UID, OWLET_AUTHKEY, OWLET_AV_ACCOUNT, OWLET_AV_PASSWORD,
  OWLET_IOTYPE_START (default 511), OWLET_AV_CHANNEL (default 0),
  OWLET_LICENSE_KEY (defaults to the app key), OWLET_REGION_CODE (default 3)

Needs the TUTK .so libs (libIOTCAPIs.so, libAVAPIs.so, libTUTKGlobalAPIs.so, …)
in TUTK_LIB_DIR.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import time
from ctypes import (
    CDLL,
    POINTER,
    Structure,
    byref,
    c_byte,
    c_char,
    c_char_p,
    c_int,
    c_ubyte,
    c_uint,
    create_string_buffer,
)

LIB_DIR = os.environ.get("TUTK_LIB_DIR", "/app/libs/x86_64")

# The license key baked into the Owlet Android app (com.owletcare.owletcare).
# Required by the newer Kalay SDK before any IOTC call, or connect hangs forever.
APP_LICENSE_KEY = (
    "AQAAAGHr2tF3sL8TGR+XirMqZSd8hKY3eBRqKIceLcUSy2okTWYU27qQmwzBORp3tw1yoqiX7l+"
    "yoikFTI+Dzh9M+utHJ/3UBjL8FkYk4kuTSdcE6FtpD3Gidjxnmu2z9TONdpEx15uXvTATqSexOC"
    "GDcldb3xtVXRmH0GoVx9SPKwVPaj7/iYJnPaaURxPzEbEr2Yfd0ckSZoZ8jRH5jxmcJdob"
)

UID = os.environ.get("OWLET_UID", "")
AUTHKEY = os.environ.get("OWLET_AUTHKEY", "")
AV_ACCOUNT = os.environ.get("OWLET_AV_ACCOUNT", "admin").encode()
AV_PASSWORD = os.environ.get("OWLET_AV_PASSWORD", "").encode()
# `or "<default>"` (not the .get default) so an env var present-but-empty — which
# is exactly what an older saved /config/owlet.env contains — falls back cleanly
# instead of crashing on int("").
IOTYPE_START = int(os.environ.get("OWLET_IOTYPE_START") or "511", 0)   # 0x01FF
AV_CHANNEL = int(os.environ.get("OWLET_AV_CHANNEL") or "0")
LICENSE_KEY = os.environ.get("OWLET_LICENSE_KEY") or APP_LICENSE_KEY
REGION_CODE = int(os.environ.get("OWLET_REGION_CODE") or "3")          # 3 = REGION_US
CONNECT_TIMEOUT = int(os.environ.get("OWLET_CONNECT_TIMEOUT") or "20")

FRAME_BUF = 1024 * 1024
FINFO_BUF = 64
AV_ER_DATA_NOREADY = -20012
AV_ER_REMOTE_TIMEOUT_DISCONNECT = -20015
AV_ER_SESSION_CLOSE_BY_REMOTE = -20016


# --------------------------------------------------------------------------- #
# St_IOTCConnectInput — the struct IOTC_Connect_ByUIDEx() takes as arg 3.
#
# jadx lists the Java mirror's fields alphabetically (authKey, authenticationType,
# dataTransmitMode, deviceRegion, lanModeDisable, p2pModeDisable, timeout) which is
# NOT the C declaration order. The layout below is the canonical ThroughTek
# IOTCAPIs.h order. We over-allocate a trailing reserved[] so the native side
# never reads past our allocation even if its real struct is a little larger.
#
# The app only ever sets authenticationType=0, authKey=<token>, timeout=20 and
# leaves everything else zero, so only those three offsets must be correct.
# --------------------------------------------------------------------------- #
class St_IOTCConnectInput(Structure):
    _pack_ = 4
    _fields_ = [
        ("authKey", c_char * 20),
        ("authenticationType", c_int),
        ("deviceRegion", c_char * 8),
        ("lanModeDisable", c_byte),
        ("p2pModeDisable", c_byte),
        ("dataTransmitMode", c_byte),
        ("_pad0", c_byte),
        ("timeout", c_int),
        ("reserved", c_int * 16),
    ]


def log(*a):
    print("[tutk]", *a, file=sys.stderr, flush=True)


def _set_sig(lib, name, restype, argtypes):
    """Set restype/argtypes if the symbol exists (prevents 64-bit ptr truncation)."""
    fn = getattr(lib, name, None)
    if fn is not None:
        fn.restype = restype
        fn.argtypes = argtypes
    return fn


def load() -> tuple[CDLL, CDLL, CDLL | None]:
    iotc_p = os.path.join(LIB_DIR, "libIOTCAPIs.so")
    av_p = os.path.join(LIB_DIR, "libAVAPIs.so")
    for p in (iotc_p, av_p):
        if not os.path.exists(p):
            log(f"MISSING {p} — copy the Owlet x86_64 TUTK libs into {LIB_DIR}/")
            sys.exit(2)
    # Preload the ThroughTek dependency libs (globally) so IOTC/AV resolve their
    # symbols. crypto/ssl/curl first, then globals/tunnel/RDT.
    for dep in (
        "libcrypto.so", "libcrypto.so.1.1", "libssl.so", "libssl.so.1.1",
        "libcurl.so", "libTUTKGlobalAPIs.so", "libP2PTunnelAPIs.so", "libRDTAPIs.so",
    ):
        dp = os.path.join(LIB_DIR, dep)
        if os.path.exists(dp):
            try:
                CDLL(dp, mode=ctypes.RTLD_GLOBAL)
                log(f"preloaded {dep}")
            except OSError as e:
                log(f"preload {dep} failed: {e}")
    tutk = None
    tp = os.path.join(LIB_DIR, "libTUTKGlobalAPIs.so")
    if os.path.exists(tp):
        try:
            tutk = CDLL(tp, mode=ctypes.RTLD_GLOBAL)
        except OSError as e:
            log(f"load libTUTKGlobalAPIs failed: {e}")
    return (CDLL(iotc_p, mode=ctypes.RTLD_GLOBAL),
            CDLL(av_p, mode=ctypes.RTLD_GLOBAL),
            tutk)


def smsg_av_stream(channel: int) -> bytes:
    # struct SMsgAVIoctrlAVStream { unsigned int channel; unsigned char reserved[4]; }
    return struct.pack("<I4s", channel, b"\x00\x00\x00\x00")


def stream_once(uid: str) -> int:
    iotc, av, tutk = load()

    # ---- bind signatures (critical on x86_64 — default int return truncates ptrs)
    _set_sig(iotc, "IOTC_Set_LanSearchPort", c_int, [c_int])
    _set_sig(iotc, "IOTC_Setup_Session_Alive_Timeout", c_int, [c_uint])
    _set_sig(iotc, "IOTC_Initialize2", c_int, [c_uint])
    _set_sig(iotc, "IOTC_Get_SessionID", c_int, [])
    _set_sig(iotc, "IOTC_Connect_ByUIDEx", c_int,
             [c_char_p, c_int, POINTER(St_IOTCConnectInput)])
    _set_sig(iotc, "IOTC_Connect_ByUID_Parallel", c_int, [c_char_p, c_int])
    _set_sig(iotc, "IOTC_Session_Close", c_int, [c_int])
    _set_sig(iotc, "IOTC_DeInitialize", c_int, [])
    _set_sig(av, "avInitialize", c_int, [c_int])
    _set_sig(av, "avDeInitialize", c_int, [])
    _set_sig(av, "avClientStart2", c_int,
             [c_int, c_char_p, c_char_p, c_uint, POINTER(c_uint), c_ubyte,
              POINTER(c_uint)])
    _set_sig(av, "avSendIOCtrl", c_int, [c_int, c_uint, c_char_p, c_int])
    _set_sig(av, "avClientStop", c_int, [c_int])
    _set_sig(av, "avRecvFrameData2", c_int,
             [c_int, c_char_p, c_int, POINTER(c_int), POINTER(c_int),
              c_char_p, c_int, POINTER(c_int), POINTER(c_int)])
    if tutk is not None:
        _set_sig(tutk, "TUTK_SDK_Set_License_Key", c_int, [c_char_p])
        _set_sig(tutk, "TUTK_SDK_Set_Region", c_int, [c_int])

    # ---- license + region the newer Kalay SDK requires BEFORE init -----------
    if tutk is not None and hasattr(tutk, "TUTK_SDK_Set_License_Key"):
        rc = tutk.TUTK_SDK_Set_License_Key(LICENSE_KEY.encode())
        log(f"TUTK_SDK_Set_License_Key rc={rc}")
    else:
        log("WARNING: libTUTKGlobalAPIs/TUTK_SDK_Set_License_Key unavailable")

    if hasattr(iotc, "IOTC_Set_LanSearchPort"):
        log(f"IOTC_Set_LanSearchPort(63616) rc={iotc.IOTC_Set_LanSearchPort(63616)}")
    if hasattr(iotc, "IOTC_Setup_Session_Alive_Timeout"):
        log("IOTC_Setup_Session_Alive_Timeout(20) rc="
            f"{iotc.IOTC_Setup_Session_Alive_Timeout(20)}")
    if tutk is not None and hasattr(tutk, "TUTK_SDK_Set_Region"):
        log(f"TUTK_SDK_Set_Region({REGION_CODE}) rc={tutk.TUTK_SDK_Set_Region(REGION_CODE)}")

    rc = iotc.IOTC_Initialize2(0)
    log(f"IOTC_Initialize2 rc={rc}")
    if rc < 0:
        return 3
    av.avInitialize(512)

    sid = iotc.IOTC_Get_SessionID()
    log(f"IOTC_Get_SessionID -> {sid}")
    if sid < 0:
        return 3

    log(f"connecting UID={uid} authKey={'yes' if AUTHKEY else 'no'} …")
    inp = St_IOTCConnectInput()
    inp.authenticationType = 0
    inp.authKey = AUTHKEY.encode()[:19]
    inp.timeout = CONNECT_TIMEOUT
    session = iotc.IOTC_Connect_ByUIDEx(uid.encode(), sid, byref(inp))
    log(f"IOTC_Connect_ByUIDEx -> {session}")
    if session < 0 and hasattr(iotc, "IOTC_Connect_ByUID_Parallel"):
        # Fallback for cams that don't require the authKey.
        session = iotc.IOTC_Connect_ByUID_Parallel(uid.encode(), sid)
        log(f"IOTC_Connect_ByUID_Parallel -> {session}")
    if session < 0:
        log(f"connect failed: {session}")
        return 4
    log(f"IOTC session={session}")

    serv = c_uint(0)
    resend = c_uint(1)
    av_idx = av.avClientStart2(session, c_char_p(AV_ACCOUNT), c_char_p(AV_PASSWORD),
                              c_uint(CONNECT_TIMEOUT), byref(serv), c_ubyte(AV_CHANNEL),
                              byref(resend))
    if av_idx < 0:
        log(f"avClientStart2 failed: {av_idx} (check AV account/password)")
        return 5
    log(f"AV channel={av_idx} servType={serv.value}")

    payload = smsg_av_stream(AV_CHANNEL)
    av.avSendIOCtrl(av_idx, IOTYPE_START, c_char_p(payload), len(payload))
    log(f"sent IOTYPE_START={IOTYPE_START} payload={payload.hex()}")

    buf = create_string_buffer(FRAME_BUF)
    finfo = create_string_buffer(FINFO_BUF)
    actual = c_int(0); expected = c_int(0); finfo_len = c_int(0); frmno = c_int(0)
    out = sys.stdout.buffer
    frames = 0
    last_log = time.time()
    while True:
        rc = av.avRecvFrameData2(av_idx, buf, FRAME_BUF,
                                 byref(actual), byref(expected),
                                 finfo, FINFO_BUF, byref(finfo_len), byref(frmno))
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

    av.avClientStop(av_idx)
    iotc.IOTC_Session_Close(session)
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
