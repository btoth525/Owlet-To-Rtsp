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
import fcntl
import json
import os
import struct
import sys
import threading
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
# A connect can succeed at the IOTC/AV layer yet deliver NO video when the camera's
# single P2P slot is still held by a prior/racing session. Give up on such a "dud"
# session after this many seconds with no frame, then wait out the camera's session
# hold before reconnecting (otherwise the next connect just races it again).
NO_VIDEO_TIMEOUT = int(os.environ.get("OWLET_NO_VIDEO_TIMEOUT") or "15")
RECONNECT_WAIT = int(os.environ.get("OWLET_RECONNECT_WAIT") or "25")  # > cam's 20s session hold
# AV layer (avClientStartEx, the path the Owlet app uses). security_mode:
# 0=Simple 1=Dtls 2=Auto; auth_type: 0=Password 1=Token 2=Nebula. If
# OWLET_AV_SECURITY_MODE is blank we auto-probe [Auto, Dtls, Simple] because the
# Dream Duo rejects plain avClientStart2 (simple) with a DTLS-class error.
AV_SECURITY_MODE = os.environ.get("OWLET_AV_SECURITY_MODE", "").strip()
AV_AUTH_TYPE = int(os.environ.get("OWLET_AV_AUTH_TYPE") or "0")
AV_SYNC_RECV = int(os.environ.get("OWLET_AV_SYNC_RECV") or "0")

FRAME_BUF = 2 * 1024 * 1024   # 2 MB — fits a 1440p/2K keyframe without truncation
FINFO_BUF = 64
IOTC_ER_ALREADY_INITIALIZED = -3
AV_ER_DATA_NOREADY = -20012
AV_ER_REMOTE_TIMEOUT_DISCONNECT = -20015
AV_ER_SESSION_CLOSE_BY_REMOTE = -20016

# --- camera environmental sensors (temp/humidity/noise/brightness) ------------
# Reverse-engineered from the Owlet app (see docs/owlet-cam-sensors.md). Temp +
# noise + motion + sound are embedded in the EXTENDED frame-info struct the cam
# sends with every frame (offsets below). Humidity + brightness need a separate
# GetRealtimeData IOCTL (req 960 -> resp 961). We publish everything to a JSON
# sidecar the web UI / overlay reads. Disable with OWLET_SENSORS=0.
CAM_SENSORS_PATH = os.environ.get("OWLET_CAM_SENSORS", "")
SENSORS_ENABLED = os.environ.get("OWLET_SENSORS", "1") != "0"
IOTYPE_GET_REALTIME_REQ = 960   # 0x3C0
IOTYPE_GET_REALTIME_RESP = 961  # 0x3C1
CAM_SENSOR_INTERVAL = int(os.environ.get("OWLET_SENSOR_INTERVAL") or "10")
# frame-info field offsets (little-endian) in the Owlet extended FRAMEINFO
_FI_FLAGS = 2
_FI_TEMP = 16
_FI_AUDIO_DB = 24
_FI_MIN_LEN = 28

_CAM_SENSORS: dict = {}
_CAM_SENSORS_LOCK = threading.Lock()
_last_frame_sensor = [0.0]   # throttle frame-info writes
# Some cams report temperature in tenths of °C. Default 1 (whole °C — confirmed
# correct on the reference cam). Set OWLET_TEMP_SCALE=10 if your room temp reads
# ~10x too high.
try:
    TEMP_SCALE = float(os.environ.get("OWLET_TEMP_SCALE") or "1") or 1.0
except ValueError:
    TEMP_SCALE = 1.0


def _publish_cam_sensors(**fields) -> None:
    """Merge new readings into the sidecar JSON (atomic write)."""
    if not (CAM_SENSORS_PATH and SENSORS_ENABLED):
        return
    if fields.get("temperature") is not None and TEMP_SCALE != 1.0:
        fields["temperature"] = round(fields["temperature"] / TEMP_SCALE, 1)
    with _CAM_SENSORS_LOCK:
        _CAM_SENSORS.update({k: v for k, v in fields.items() if v is not None})
        _CAM_SENSORS["ts"] = time.time()
        try:
            tmp = CAM_SENSORS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(_CAM_SENSORS, f)
            os.replace(tmp, CAM_SENSORS_PATH)
        except OSError:
            pass


def _parse_frame_sensors(info: bytes, n: int) -> None:
    """Pull temp/noise/motion/sound from the extended frame-info struct.

    Throttled to CAM_SENSOR_INTERVAL so we're not writing the sidecar at 25fps.
    """
    if not (CAM_SENSORS_PATH and SENSORS_ENABLED) or n < _FI_MIN_LEN:
        return
    now = time.time()
    if now - _last_frame_sensor[0] < CAM_SENSOR_INTERVAL:
        return
    _last_frame_sensor[0] = now
    flags = info[_FI_FLAGS]
    temp = int.from_bytes(info[_FI_TEMP:_FI_TEMP + 4], "little", signed=True)
    noise = int.from_bytes(info[_FI_AUDIO_DB:_FI_AUDIO_DB + 4], "little", signed=True)
    _publish_cam_sensors(
        temperature=temp,
        noise=noise,
        motion=1 if (flags & 0x02) else 0,
        sound=1 if (flags & 0x08) else 0,
    )


def _realtime_thread(av, av_idx, stop_evt):
    """Poll the cam's GetRealtimeData IOCTL for humidity + brightness.

    Sends req 960 (4 zero bytes), reads IOCtrl frames until resp 961, parses the
    little-endian struct {temp i32@0, humidity i32@4, noise i32@8, brightness
    i32@12, wifi_rssi i8@16}. Fully guarded — any error just skips this round so
    it can never disturb the video pipeline.
    """
    if not (CAM_SENSORS_PATH and SENSORS_ENABLED):
        return
    if not hasattr(av, "avRecvIOCtrl"):
        return
    io_type = c_int(0)
    rbuf = create_string_buffer(64)
    payload = b"\x00\x00\x00\x00"
    while not stop_evt.is_set():
        try:
            with _AV_IO:
                av.avSendIOCtrl(av_idx, IOTYPE_GET_REALTIME_REQ, payload, len(payload))
            # drain IOCtrl responses for up to ~1s looking for 961
            deadline = time.time() + 1.0
            while time.time() < deadline and not stop_evt.is_set():
                with _AV_IO:
                    rc = av.avRecvIOCtrl(av_idx, byref(io_type), rbuf, 64, 500)
                if rc < 0:
                    break
                if io_type.value == IOTYPE_GET_REALTIME_RESP and rc >= 16:
                    b = rbuf.raw
                    _publish_cam_sensors(
                        temperature=int.from_bytes(b[0:4], "little", signed=True),
                        humidity=int.from_bytes(b[4:8], "little", signed=True),
                        noise=int.from_bytes(b[8:12], "little", signed=True),
                        brightness=int.from_bytes(b[12:16], "little", signed=True),
                        # only read rssi if the response actually carries byte 16
                        wifi_rssi=(int.from_bytes(b[16:17], "little", signed=True)
                                   if rc >= 17 else None),
                    )
                    break
        except Exception:  # noqa: BLE001
            pass
        stop_evt.wait(CAM_SENSOR_INTERVAL)


# --- audio probe (step 1: capture + report the camera's audio format) ---------
# The Owlet cam carries audio on the same Kalay AV channel as video, but its codec
# / sample rate are undocumented. This probe reads the audio channel READ-ONLY (it
# never touches the video pipeline) and logs the exact codec_id + flags so we can
# mux it correctly. Disable with OWLET_AUDIO=0.
AUDIO_ENABLED = os.environ.get("OWLET_AUDIO", "1") != "0"
# Standard TUTK "start audio" IOCTL (IOTYPE_USER_IPCAM_AUDIOSTART = 0x300 = 768).
IOTYPE_AUDIOSTART = int(os.environ.get("OWLET_IOTYPE_AUDIOSTART") or "768")
AUDIO_BUF = 16 * 1024
# Best-effort decode tables for the log (raw values are logged too, so we don't
# rely on these being right — they just annotate the report).
_AUDIO_CODECS = {
    0x86: "AAC", 0x87: "AAC", 0x88: "AAC", 0x89: "PCM(s16)", 0x8a: "ADPCM",
    0x8b: "G711 u-law", 0x8c: "G711 a-law", 0x8d: "G726", 0x8e: "Speex",
    0x8f: "MP3", 0x140: "AAC", 0x141: "AAC",
}
_AUDIO_RATES = {0: 8000, 1: 11025, 2: 12000, 3: 16000, 4: 22050,
                5: 24000, 6: 32000, 7: 44100, 8: 48000}


def _guess_audio(codec_id: int, flags: int) -> str:
    codec = _AUDIO_CODECS.get(codec_id, f"unknown(0x{codec_id:04x})")
    rate = _AUDIO_RATES.get((flags >> 2) & 0x0f, "?")
    ch = "stereo" if (flags & 0x01) else "mono"
    bits = 16 if (flags & 0x02) else 8
    return f"codec≈{codec}, ~{rate}Hz, {ch}, {bits}-bit  (guess from flags — verify)"


# --- two-way talk / sound playback: send audio TO the camera's speaker ---------
# Recovered from the Owlet app: it sends IOTYPE_USER_IPCAM_SPEAKERSTART (848), then
# pushes AAC-LC 8kHz mono frames via avSendAudioData. We read AAC (ADTS) from
# OWLET_TALK_FIFO (written by the web UI's "hold to talk" / "play sound"), strip
# the ADTS header to raw AAC access units, and send them; SPEAKERSTOP (849) on idle.
IOTYPE_SPEAKERSTART = int(os.environ.get("OWLET_IOTYPE_SPEAKERSTART") or "848")
IOTYPE_SPEAKERSTOP = int(os.environ.get("OWLET_IOTYPE_SPEAKERSTOP") or "849")
# If the camera closes the AV session when it receives SPEAKERSTART (848), set
# OWLET_SKIP_SPEAKERSTART=1. Some cameras open the speaker channel bi-directionally
# from AUDIOSTART alone and reject a separate SPEAKERSTART IOCTL.
SKIP_SPEAKERSTART = os.environ.get("OWLET_SKIP_SPEAKERSTART", "0") != "0"
TALK_FIFO = os.environ.get("OWLET_TALK_FIFO", "")
# Speaker frame format. Defaults match what the cam streams to us (AAC, 8kHz mono);
# overridable, and auto-filled from the live audio probe when it reports.
SPEAKER_CODEC_ID = int(os.environ.get("OWLET_SPEAKER_CODEC_ID") or "0x88", 0)
SPEAKER_FLAGS = int(os.environ.get("OWLET_SPEAKER_FLAGS") or "0x02", 0)
# Whether to send the AAC frame with its ADTS header intact. We default to
# stripping it (raw AAC access unit) but some camera speaker decoders need the
# ADTS header to learn the sample-rate/profile and stay SILENT without it — so
# OWLET_TALK_KEEP_ADTS=1 sends the full ADTS frame. If the cam accepts frames
# (0 rejected) but you hear nothing, flip this.
TALK_KEEP_ADTS = (os.environ.get("OWLET_TALK_KEEP_ADTS", "") or "").lower() in (
    "1", "true", "yes", "on")
_PROBED_AUDIO: dict = {"codec_id": None, "flags": None}

# Serializes every avSendIOCtrl / avRecvIOCtrl / avSendAudioData call. ctypes
# releases the GIL, so the talk thread, the realtime-sensor poller and the
# startup IOCTLs would otherwise run truly parallel inside the C SDK on one
# av_idx — a known segfault/queue-corruption source. (Frame/audio RECV stay on
# their own threads, unlocked.)
_AV_IO = threading.Lock()

# Half-duplex coordination. The Owlet camera has ONE P2P session and the TUTK
# lib is NOT safe doing simultaneous audio RECV (the probe) and audio SEND (talk)
# on the same channel — doing both corrupts/kills the session (video drops and
# avSendAudioData can hang the process, needing a container restart). So while the
# talk thread is actively sending speaker audio, the audio-receive probe PAUSES
# (exactly how the Owlet app behaves: it stops listening while you hold talk).
_TALKING = threading.Event()


def _talk_frameinfo(ts_ms: int) -> bytes:
    """16-byte audio frame header, byte-for-byte matching the Owlet app's
    e1.d.parseContent(short codec, byte flags, byte, byte, int ts):
        [0:2]  codec_id   little-endian uint16   (0x0088 = AAC)
        [2]    flags       (0x02)
        [3]    0
        [4]    0
        [5:12] 0
        [12:16] timestamp  little-endian uint32   <-- NOT offset 8!
    The camera reads the timestamp at offset 12; we previously wrote it at 8,
    so every frame carried ts=0 and the camera's audio jitter buffer never
    scheduled playback (frames accepted, 0 rejected, but SILENT). codec/flags
    come from the live probe when known, else the configured defaults."""
    codec = _PROBED_AUDIO["codec_id"] if _PROBED_AUDIO["codec_id"] is not None else SPEAKER_CODEC_ID
    flags = _PROBED_AUDIO["flags"] if _PROBED_AUDIO["flags"] is not None else SPEAKER_FLAGS
    fi = bytearray(16)
    struct.pack_into("<H", fi, 0, codec & 0xFFFF)        # codec_id  @0
    fi[2] = flags & 0xFF                                  # flags     @2
    struct.pack_into("<I", fi, 12, ts_ms & 0xFFFFFFFF)   # timestamp @12 (per app)
    return bytes(fi)


def _send_adts(av, av_idx, buf: bytes, ts0: float, stats: list,
               stop_evt=None) -> bytes:
    """Parse ADTS AAC frames from buf, send each as a raw AAC access unit via
    avSendAudioData. Returns leftover bytes (an incomplete trailing frame).
    stats=[sent, rejected] is updated so the talk loop can log delivery."""
    i, n = 0, len(buf)
    while n - i >= 7:
        # Bail immediately between frames if shutdown is requested — avClientStop
        # is called before this, but avSendAudioData can still hang on a dead
        # channel inside the C lib.  Checking here lets us exit after the current
        # in-flight C call returns rather than queuing more frames onto a dead av_idx.
        if stop_evt is not None and stop_evt.is_set():
            return b""
        if buf[i] != 0xFF or (buf[i + 1] & 0xF0) != 0xF0:   # ADTS sync 0xFFFx
            i += 1
            continue
        frame_len = ((buf[i + 3] & 0x03) << 11) | (buf[i + 4] << 3) | (buf[i + 5] >> 5)
        if frame_len < 7:
            i += 1
            continue
        if n - i < frame_len:
            break  # frame not fully buffered yet
        hdr = 7 if (buf[i + 1] & 0x01) else 9   # protection_absent -> 7-byte header
        # Default: strip ADTS -> raw AAC access unit. With OWLET_TALK_KEEP_ADTS=1
        # send the whole frame (header included) — some cam decoders need it.
        raw = buf[i:i + frame_len] if TALK_KEEP_ADTS else buf[i + hdr:i + frame_len]
        fi = _talk_frameinfo(int((time.time() - ts0) * 1000))
        with _AV_IO:
            rc = av.avSendAudioData(av_idx, c_char_p(raw), len(raw), c_char_p(fi), len(fi))
        if rc is not None and rc < 0:
            stats[1] += 1
            # Record a session-close so the talk loop can stop immediately
            # instead of hammering a dead channel (which can hang the process).
            if rc in (AV_ER_REMOTE_TIMEOUT_DISCONNECT, AV_ER_SESSION_CLOSE_BY_REMOTE) \
                    and len(stats) > 2:
                stats[2] = rc
                return buf[i + frame_len:]
        else:
            stats[0] += 1
        i += frame_len
    return buf[i:]


def _talk_thread(av: CDLL, av_idx: int, stop_evt: threading.Event) -> None:
    """Play whatever AAC arrives on the talk FIFO out the camera's speaker."""
    if not TALK_FIFO:
        log("[talk] OWLET_TALK_FIFO not set — talk/speaker disabled")
        return
    if not hasattr(av, "avSendAudioData"):
        log("[talk] avSendAudioData not found in TUTK lib — talk/speaker disabled "
            "(older lib build; two-way audio requires a lib with avSendAudioData)")
        return
    try:
        # O_RDWR so the FIFO never EOFs / blocks when no writer is connected.
        fd = os.open(TALK_FIFO, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:  # noqa: BLE001
        log(f"[talk] cannot open {TALK_FIFO}: {e}")
        return
    log(f"[talk] FIFO open (fd={fd}) — ready to receive audio on {TALK_FIFO}")
    buf = b""
    speaking = False
    last_audio = 0.0
    ts0 = time.time()
    stats = [0, 0, 0]   # [sent, rejected, dead_rc] avSendAudioData results
    last_alive = time.monotonic()

    def _spk(ioctl):
        sp = smsg_av_stream(AV_CHANNEL)
        with _AV_IO:
            return av.avSendIOCtrl(av_idx, ioctl, c_char_p(sp), len(sp))

    try:
        while not stop_evt.is_set():
            # Periodic heartbeat so we can confirm the thread is alive in logs.
            if time.monotonic() - last_alive > 30:
                log(f"[talk] alive, listening on FIFO (speaking={speaking})")
                last_alive = time.monotonic()
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                chunk = b""
            if chunk:
                log(f"[talk] read {len(chunk)} bytes from FIFO")
                if not speaking:
                    # Pause the audio-RECV probe BEFORE we start sending so the
                    # two never overlap on the cam's single channel.
                    _TALKING.set()
                    time.sleep(0.06)   # let the probe loop notice + park
                    if SKIP_SPEAKERSTART:
                        rc = 0
                        log(f"[talk] speaker on (SPEAKERSTART skipped per OWLET_SKIP_SPEAKERSTART)")
                    else:
                        rc = _spk(IOTYPE_SPEAKERSTART)
                        cid = _PROBED_AUDIO["codec_id"]
                        log(f"[talk] speaker on (start ioctl={IOTYPE_SPEAKERSTART} rc={rc}, "
                            f"codec=0x{(cid if cid is not None else SPEAKER_CODEC_ID):04x}, "
                            f"adts={'kept' if TALK_KEEP_ADTS else 'stripped'})")
                        # If the camera closed the session in response to SPEAKERSTART,
                        # bail NOW — don't try avSendAudioData on a dead av_idx (that
                        # would hang the _AV_IO lock and freeze the video loop).
                        if rc in (AV_ER_REMOTE_TIMEOUT_DISCONNECT, AV_ER_SESSION_CLOSE_BY_REMOTE):
                            log(f"[talk] SPEAKERSTART killed the session (rc={rc}). "
                                f"Set OWLET_SKIP_SPEAKERSTART=1 in Advanced settings to bypass.")
                            _TALKING.clear()
                            return
                    speaking = True
                    ts0 = time.time()
                    stats[0] = stats[1] = stats[2] = 0
                buf = _send_adts(av, av_idx, buf + chunk, ts0, stats, stop_evt=stop_evt)
                last_audio = time.monotonic()
                # If the camera tore the session down mid-send, stop NOW so we
                # don't keep calling avSendAudioData on a dead channel (hangs).
                if stats[2]:
                    log(f"[talk] camera closed the session mid-send (rc={stats[2]}); "
                        f"stopping talk. Tick 'Skip SPEAKERSTART' if this persists.")
                    speaking = False
                    _TALKING.clear()
                    return
            else:
                if speaking and time.monotonic() - last_audio > 0.8:
                    _spk(IOTYPE_SPEAKERSTOP)
                    speaking = False
                    buf = b""
                    _TALKING.clear()   # resume the audio-RECV probe
                    log(f"[talk] speaker off (idle) — sent {stats[0]} frames, "
                        f"{stats[1]} rejected")
                time.sleep(0.02)
    finally:
        _TALKING.clear()   # always let the audio-RECV probe resume
        # Skip SPEAKERSTOP when the session is already being torn down by
        # avClientStop (stop_evt set) — avSendIOCtrl on a stopped av_idx can
        # block inside the C lib and cause join() to time out, leaking this
        # thread across reconnects (showing as heartbeats every ~10s instead of 30s).
        if speaking and not stop_evt.is_set():
            try:
                _spk(IOTYPE_SPEAKERSTOP)
            except Exception:  # noqa: BLE001
                pass
        try:
            os.close(fd)
        except Exception:  # noqa: BLE001
            pass


def _audio_probe(av: CDLL, av_idx: int, stop_evt: threading.Event) -> None:
    """Capture the audio channel and, if OWLET_AUDIO_FIFO is set, write its raw
    bytes to that FIFO so ffmpeg can mux the audio next to the video. Read-only
    w.r.t. the video pipeline (never blocks the picture). Runs in its own thread."""
    if not hasattr(av, "avRecvAudioData"):
        log("[audio] avRecvAudioData missing from this lib build — no audio capture")
        return
    fifo_path = os.environ.get("OWLET_AUDIO_FIFO", "")
    fifo_fd = -1
    abuf = create_string_buffer(AUDIO_BUF)
    ainfo = create_string_buffer(FINFO_BUF)
    aidx = c_uint(0)
    frames = 0
    reported = False
    warned = False
    last_log = time.time()
    started = time.time()
    try:
        while not stop_evt.is_set():
            # Half-duplex: while the talk thread is pushing speaker audio, STOP
            # receiving audio. Simultaneous send+recv on the cam's single AV
            # channel corrupts the session (drops video / wedges the process).
            if _TALKING.is_set():
                time.sleep(0.05)
                continue
            try:
                rc = av.avRecvAudioData(av_idx, abuf, AUDIO_BUF, ainfo, FINFO_BUF, byref(aidx))
            except Exception as e:  # noqa: BLE001
                log(f"[audio] avRecvAudioData error: {e}")
                return
            if rc > 0:
                frames += 1
                if not reported:
                    info = ainfo.raw[:16]
                    codec_id = int.from_bytes(info[0:2], "little")
                    flags = info[2]
                    # feed the real format into the talk/speaker path
                    _PROBED_AUDIO["codec_id"] = codec_id
                    _PROBED_AUDIO["flags"] = flags
                    log("[audio] ===== AUDIO DETECTED =====")
                    log(f"[audio] codec_id=0x{codec_id:04x} flags=0x{flags:02x} "
                        f"frame_size={rc}B info={info.hex()}")
                    log(f"[audio] first frame bytes: {abuf.raw[:min(rc, 24)].hex()}")
                    log(f"[audio] best guess: {_guess_audio(codec_id, flags)}")
                    reported = True
                # Open the FIFO non-blocking (retry until ffmpeg opens the read end),
                # then switch it to blocking so each frame is written WHOLE — keeping
                # the AAC/ADTS framing intact (a partial write would corrupt it).
                if fifo_fd < 0 and fifo_path:
                    try:
                        fifo_fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                        fl = fcntl.fcntl(fifo_fd, fcntl.F_GETFL)
                        fcntl.fcntl(fifo_fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)
                        log(f"[audio] muxing audio -> {fifo_path}")
                    except OSError:
                        fifo_fd = -1  # no reader yet; try again on the next frame
                if fifo_fd >= 0:
                    try:
                        os.write(fifo_fd, abuf.raw[:rc])
                    except (BrokenPipeError, OSError):
                        try:
                            os.close(fifo_fd)
                        except Exception:  # noqa: BLE001
                            pass
                        fifo_fd = -1  # ffmpeg went away; reopen when it's back
                if time.time() - last_log > 30:
                    log(f"[audio] {frames} audio frames forwarded")
                    last_log = time.time()
            elif rc == AV_ER_DATA_NOREADY:
                if not reported and not warned and time.time() - started > 12:
                    log("[audio] no audio 12s after AUDIOSTART — the cam may use a "
                        "different audio-start IOCTL, or audio is muted. Video unaffected.")
                    warned = True
                time.sleep(0.02)
            else:
                time.sleep(0.03)
    finally:
        if fifo_fd >= 0:
            try:
                os.close(fifo_fd)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# St_IOTCConnectInput — the struct IOTC_Connect_ByUIDEx() takes as arg 3.
#
# This layout is NOT a guess: it was read straight out of the Owlet x86_64
# libIOTCAPIs.so by disassembling both the real IOTC_Connect_ByUIDEx() and its
# JNI wrapper. The exact offsets / sizes the binary enforces:
#
#   0x00 int  structSize         -- MUST equal 0xA0 (160); the C function does
#                                   `cmp [rdx],0xA0; jne -> return -46` first.
#   0x04 int  authenticationType -- must be 0 for the password path.
#   0x08 char authKey[8]         -- the JNI checks strlen(authKey)==8 exactly
#                                   (the Owlet KMS AuthKey is 8 chars).
#   0x10 char deviceRegion[132]  -- left empty.
#   0x94 int  timeout            -- seconds.
#   0x98 int  dataTransmitMode   -- 0 (validated <=2, !=1).
#   0x9c byte lanModeDisable
#   0x9d byte p2pModeDisable
#   total sizeof == 0xA0 (160).
# --------------------------------------------------------------------------- #
class St_IOTCConnectInput(Structure):
    _fields_ = [
        ("structSize", c_int),          # 0x00  must be 160
        ("authenticationType", c_int),  # 0x04
        ("authKey", c_char * 8),        # 0x08  exactly 8 chars
        ("deviceRegion", c_char * 132), # 0x10
        ("timeout", c_int),             # 0x94
        ("dataTransmitMode", c_int),    # 0x98
        ("lanModeDisable", c_byte),     # 0x9c
        ("p2pModeDisable", c_byte),     # 0x9d
        ("_pad", c_byte * 2),           # 0x9e -> 0xA0
    ]


# --------------------------------------------------------------------------- #
# St_AVClientStartInConfig / St_AVClientStartOutConfig — args to avClientStartEx,
# the AV-layer login the Owlet app uses (com.owlet.tutk.AndroidTutkSdk.startClient).
# Layout read straight out of the Owlet libAVAPIs.so (real avClientStartEx + its
# JNI wrapper). Both carry a leading structSize the C side validates:
#   IN  must be 0x40 (64);  OUT must be 0x20 (32).
# Field offsets (IN): 0x04 session, 0x08 channel(byte), 0x0c timeout, 0x10 account*,
#   0x18 password*, 0x20 resend, 0x24 security_mode, 0x28 auth_type,
#   0x2c sync_recv_data, 0x30 dtls_cipher_suites*.
# --------------------------------------------------------------------------- #
class St_AVClientStartInConfig(Structure):
    _fields_ = [
        ("structSize", c_int),                  # 0x00 = 64
        ("iotc_session_id", c_int),             # 0x04
        ("iotc_channel_id", c_byte),            # 0x08 (byte)
        ("_p08", c_byte * 3),                   # 0x09
        ("timeout_sec", c_int),                 # 0x0c
        ("account_or_identity", c_char_p),      # 0x10
        ("password_or_token", c_char_p),        # 0x18
        ("resend", c_int),                      # 0x20
        ("security_mode", c_int),               # 0x24
        ("auth_type", c_int),                   # 0x28
        ("sync_recv_data", c_int),              # 0x2c
        ("dtls_cipher_suites", c_char_p),       # 0x30
        ("_r38", c_int),                        # 0x38
        ("_r3c", c_int),                        # 0x3c  -> 0x40
    ]


class St_AVClientStartOutConfig(Structure):
    # 32 bytes; the SDK only reads structSize (==0x20) on the way in and writes
    # negotiated results (server_type, two_way_streaming, …) we don't need.
    _fields_ = [
        ("structSize", c_int),                  # 0x00 = 32
        ("_rest", c_int * 7),                   # 0x04 -> 0x20
    ]


def log(*a):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(ts, "[tutk]", *a, file=sys.stderr, flush=True)


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


def stream_once(uid: str, sec_mode: int) -> int:
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
    _set_sig(av, "avClientStartEx", c_int,
             [POINTER(St_AVClientStartInConfig), POINTER(St_AVClientStartOutConfig)])
    _set_sig(av, "avSendIOCtrl", c_int, [c_int, c_uint, c_char_p, c_int])
    if hasattr(av, "avRecvIOCtrl"):
        _set_sig(av, "avRecvIOCtrl", c_int,
                 [c_int, POINTER(c_int), c_char_p, c_int, c_int])
    _set_sig(av, "avClientStop", c_int, [c_int])
    _set_sig(av, "avRecvFrameData2", c_int,
             [c_int, c_char_p, c_int, POINTER(c_int), POINTER(c_int),
              c_char_p, c_int, POINTER(c_int), POINTER(c_int)])
    _set_sig(av, "avRecvAudioData", c_int,
             [c_int, c_char_p, c_int, c_char_p, c_int, POINTER(c_uint)])
    _set_sig(av, "avSendAudioData", c_int,
             [c_int, c_char_p, c_int, c_char_p, c_int])
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
    if rc < 0 and rc != IOTC_ER_ALREADY_INITIALIZED:
        return 3
    av.avInitialize(512)

    session = -1
    av_idx = -1
    audio_stop = threading.Event()
    audio_thr = None
    talk_thr = None
    sensors_thr = None
    try:
        sid = iotc.IOTC_Get_SessionID()
        log(f"IOTC_Get_SessionID -> {sid}")
        if sid < 0:
            return 3

        log(f"connecting UID={uid} authKey={'yes' if AUTHKEY else 'no'} …")
        ak = AUTHKEY.encode()
        if AUTHKEY and len(ak) != 8:
            log(f"WARNING: authKey is {len(ak)} chars; the SDK requires exactly 8 "
                "(IOTC_Connect_ByUIDEx will reject it with -46)")
        inp = St_IOTCConnectInput()
        inp.structSize = 160               # 0xA0 — required size/version guard
        inp.authenticationType = 0
        inp.authKey = ak[:8]
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

        av_idx = av_client_start(av, session, sec_mode)
        if av_idx < 0:
            return 5

        payload = smsg_av_stream(AV_CHANNEL)
        av.avSendIOCtrl(av_idx, IOTYPE_START, c_char_p(payload), len(payload))
        log(f"sent IOTYPE_START={IOTYPE_START} payload={payload.hex()}")

        # Kick off audio capture alongside the video loop. It reports the codec and,
        # when the exec gave us an OWLET_AUDIO_FIFO, feeds AAC into it for muxing.
        # Always run when a FIFO is present so ffmpeg's audio input never starves.
        if AUDIO_ENABLED or os.environ.get("OWLET_AUDIO_FIFO"):
            try:
                ap = smsg_av_stream(AV_CHANNEL)
                av.avSendIOCtrl(av_idx, IOTYPE_AUDIOSTART, c_char_p(ap), len(ap))
                log(f"sent IOTYPE_AUDIOSTART={IOTYPE_AUDIOSTART}")
                audio_thr = threading.Thread(
                    target=_audio_probe, args=(av, av_idx, audio_stop), daemon=True)
                audio_thr.start()
            except Exception as e:  # noqa: BLE001
                log(f"[audio] probe start failed: {e}")

        # Talk / sound-playback: play AAC arriving on the talk FIFO out the speaker.
        has_send = hasattr(av, "avSendAudioData")
        log(f"[talk] avSendAudioData={'found' if has_send else 'NOT FOUND in lib'} "
            f"TALK_FIFO={TALK_FIFO!r}")
        if TALK_FIFO:
            try:
                talk_thr = threading.Thread(
                    target=_talk_thread, args=(av, av_idx, audio_stop), daemon=True)
                talk_thr.start()
                log(f"[talk] thread started — listening on {TALK_FIFO}")
            except Exception as e:  # noqa: BLE001
                log(f"[talk] start failed: {e}")

        # Room sensors: poll the GetRealtimeData IOCTL for humidity + brightness
        # (temp/noise/motion/sound already ride the frame info).
        if CAM_SENSORS_PATH and SENSORS_ENABLED:
            try:
                sensors_thr = threading.Thread(
                    target=_realtime_thread, args=(av, av_idx, audio_stop), daemon=True)
                sensors_thr.start()
                log(f"[sensors] publishing room sensors to {CAM_SENSORS_PATH}")
            except Exception as e:  # noqa: BLE001
                log(f"[sensors] start failed: {e}")

        buf = create_string_buffer(FRAME_BUF)
        finfo = create_string_buffer(FINFO_BUF)
        actual = c_int(0); expected = c_int(0); finfo_len = c_int(0); frmno = c_int(0)
        out = sys.stdout.buffer
        frames = 0
        last_log = time.time()
        last_data = time.time()
        while True:
            rc = av.avRecvFrameData2(av_idx, buf, FRAME_BUF,
                                     byref(actual), byref(expected),
                                     finfo, FINFO_BUF, byref(finfo_len), byref(frmno))
            if rc >= 0 and actual.value > 0:
                # zero-copy: write a view of just the frame bytes, not a full
                # 2 MB .raw copy of the whole buffer every frame (matters at 25fps).
                out.write(memoryview(buf)[:actual.value]); out.flush()
                frames += 1
                last_data = time.time()
                # temp/noise/motion/sound ride in the extended frame-info struct
                _parse_frame_sensors(finfo.raw, finfo_len.value)
                if time.time() - last_log > 15:
                    log(f"{frames} frames forwarded"); last_log = time.time()
            elif rc == AV_ER_DATA_NOREADY:
                # No frame yet. If it's been silent too long this session is a dud
                # (or has silently stalled) — bail so main() waits out the camera's
                # session hold and reconnects, instead of hanging here forever (which
                # used to require a manual "Connect & Diagnose" in the UI).
                if time.time() - last_data > NO_VIDEO_TIMEOUT:
                    log(f"no video for {NO_VIDEO_TIMEOUT}s (frames={frames}) — dud/"
                        "stalled session; reconnecting after the camera releases it")
                    return 6
                time.sleep(0.01)
            elif rc in (AV_ER_REMOTE_TIMEOUT_DISCONNECT, AV_ER_SESSION_CLOSE_BY_REMOTE):
                log(f"stream ended rc={rc}"); break
            else:
                if time.time() - last_data > NO_VIDEO_TIMEOUT:
                    log(f"no video for {NO_VIDEO_TIMEOUT}s — session stalled; reconnecting")
                    return 6
                time.sleep(0.02)
        return 0
    finally:
        # Signal the worker threads, then avClientStop FIRST — that unblocks any
        # thread parked inside avRecvAudioData/avRecvIOCtrl (which have their own
        # timeouts but can sit longer than the join). Joining BEFORE the stop
        # risked deInitialize() running while a worker was still in the C lib
        # (use-after-free / segfault on reconnect).
        audio_stop.set()
        if av_idx >= 0:
            try:
                av.avClientStop(av_idx)
            except Exception:  # noqa: BLE001
                pass
        for thr in (audio_thr, talk_thr, sensors_thr):
            if thr is not None:
                thr.join(timeout=3)
                if thr.is_alive():
                    log(f"[warn] {thr.name!r} still alive after 3s — possible thread "
                        "leak (avSendAudioData/avSendIOCtrl may be hung in C lib)")
        # Always tear down so the next attempt's IOTC_Initialize2 doesn't return
        # -3 (ALREADY_INITIALIZED) and avClientStart doesn't see a stale channel.
        if session >= 0:
            iotc.IOTC_Session_Close(session)
        av.avDeInitialize()
        iotc.IOTC_DeInitialize()


SEC_NAMES = {0: "Simple", 1: "Dtls", 2: "Auto"}


def av_client_start(av: CDLL, session: int, mode: int) -> int:
    """AV-layer login the Owlet app uses: avClientStartEx with the given DTLS
    security mode. Falls back to the legacy scalar avClientStart2 if Ex is
    missing from the lib."""
    if hasattr(av, "avClientStartEx"):
        cin = St_AVClientStartInConfig()
        cin.structSize = 64
        cin.iotc_session_id = session
        cin.iotc_channel_id = AV_CHANNEL
        cin.timeout_sec = CONNECT_TIMEOUT
        cin.account_or_identity = AV_ACCOUNT
        cin.password_or_token = AV_PASSWORD
        cin.resend = 1
        cin.security_mode = mode
        cin.auth_type = AV_AUTH_TYPE
        cin.sync_recv_data = AV_SYNC_RECV
        cin.dtls_cipher_suites = None
        cout = St_AVClientStartOutConfig()
        cout.structSize = 32
        av_idx = av.avClientStartEx(byref(cin), byref(cout))
        log(f"avClientStartEx(security={SEC_NAMES.get(mode, mode)}, "
            f"auth={AV_AUTH_TYPE}) -> {av_idx}")
        if av_idx >= 0:
            log(f"AV channel={av_idx}")
        return av_idx

    # Legacy fallback (no Ex export).
    serv = c_uint(0); resend = c_uint(1)
    av_idx = av.avClientStart2(session, c_char_p(AV_ACCOUNT), c_char_p(AV_PASSWORD),
                              c_uint(CONNECT_TIMEOUT), byref(serv), c_ubyte(AV_CHANNEL),
                              byref(resend))
    log(f"avClientStart2 -> {av_idx}")
    if av_idx >= 0:
        log(f"AV channel={av_idx} servType={serv.value}")
    return av_idx


def resolve_creds(refresh: bool = True) -> str:
    """Return the camera UID, FIRST refreshing UID/AuthKey/AV-password from the
    Owlet KMS so a *stale* saved key never blocks a (re)connect.

    This is exactly the fetch the web UI's "Connect & Diagnose" performs. Doing it
    automatically on every stream (re)start means that after a container update,
    a power blip, or a Frigate reconnect, the camera comes back on its own — no
    manual re-diagnose. The Owlet camera key can rotate and the cam also holds its
    single P2P slot for ~20s after the previous container dies, so reconnecting
    with the old key used to fail until someone re-fetched the key by hand.

    If the Owlet cloud is briefly unreachable we fall back to the saved key, so a
    transient outage doesn't kill an otherwise-working stream. Set
    OWLET_REFRESH_KMS=0 to skip the refresh and use only the saved key.
    """
    global AUTHKEY, AV_PASSWORD
    dsn = os.environ.get("OWLET_CAMERA_DSN", "").strip()
    email = os.environ.get("OWLET_EMAIL")
    pw = os.environ.get("OWLET_PASSWORD")
    region = os.environ.get("OWLET_REGION", "world")
    if refresh and os.environ.get("OWLET_REFRESH_KMS", "1") != "0" and dsn and email and pw:
        try:
            from owlet_api import OwletAPI
            api = OwletAPI(region, email, pw, log=lambda m: log(m))
            creds = api.camera_credentials(dsn)
            if creds.get("uid"):
                if creds.get("authkey"):
                    AUTHKEY = creds["authkey"]
                if creds.get("av_password"):
                    AV_PASSWORD = creds["av_password"].encode()
                log(f"refreshed camera key from KMS (uid={creds['uid']})")
                return creds["uid"]
        except Exception as e:  # noqa: BLE001
            log(f"KMS refresh failed ({e}); falling back to the saved key")
    if UID:
        return UID
    log("No OWLET_UID and KMS refresh unavailable. Set the Camera DSN + account "
        "login in the UI.")
    sys.exit(1)


def main() -> None:
    # On SIGTERM (go2rtc stopping the source / container shutdown) raise SystemExit
    # so stream_once()'s finally releases the camera session cleanly — otherwise the
    # cam holds the single P2P slot and the next connect is refused.
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    uid = resolve_creds()                 # fresh key on first start
    last_kms = time.monotonic()
    # Security mode to try. If pinned via env, use only that; otherwise cycle
    # [Auto, Dtls, Simple] across reconnects — each on a FRESH session so a
    # failed DTLS handshake on one mode can't poison the next attempt.
    modes = [int(AV_SECURITY_MODE)] if AV_SECURITY_MODE != "" else [2, 1, 0]
    i = 0
    while True:
        mode = modes[i % len(modes)]
        backoff = 5
        try:
            rc = stream_once(uid, mode)
            if rc == 5:
                # AV login failed for this security mode — advance to the next.
                i += 1
                log(f"AV login failed (security={SEC_NAMES.get(mode, mode)}); "
                    f"trying {SEC_NAMES.get(modes[i % len(modes)])} next")
            elif rc == 6:
                # Connected but got no video — the camera's single session was still
                # held. Wait past its ~20s session-alive-timeout so it fully releases;
                # reconnecting sooner just races it and gets another empty session.
                backoff = RECONNECT_WAIT
                log(f"no-video session — waiting {RECONNECT_WAIT}s for the camera to "
                    "release its session before reconnecting")
            elif rc != 0:
                log(f"exit rc={rc}; retry in 5s")
            # On a connect/AV-login/no-video failure the saved key may be stale
            # (rotated by Owlet, or the cam's single slot was still held) — pull a
            # fresh one, rate-limited to once per 15s so a long camera outage doesn't
            # hammer the Owlet cloud.
            if rc in (4, 5, 6) and time.monotonic() - last_kms >= 15:
                uid = resolve_creds()
                last_kms = time.monotonic()
        except SystemExit:
            raise
        except BrokenPipeError:
            # Our stdout consumer (ffmpeg) died — e.g. go2rtc recycled the stream.
            # Don't sit here writing into a dead pipe (the old behaviour looped
            # forever, so go2rtc never restarted the exec and Frigate got nothing
            # for hours). Exit so go2rtc relaunches the whole pipeline with a fresh
            # ffmpeg. Back off briefly first: stream_once's finally just released
            # the camera's single P2P slot, and this pause lets it settle so the
            # relaunched process doesn't immediately reconnect into a still-held
            # slot and churn the camera.
            log("downstream ffmpeg closed the pipe — backing off, then exiting for "
                "a clean go2rtc restart")
            time.sleep(5)
            return
        except Exception as e:  # noqa: BLE001
            log(f"error: {e}; retry in 5s")
        time.sleep(backoff)


if __name__ == "__main__":
    main()
