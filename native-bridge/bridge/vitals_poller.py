#!/usr/bin/env python3
"""
vitals_poller.py — continuously read Owlet Smart Sock vitals + Owlet Cam room
sensors and fan them out to everything that wants them:

  * /config/vitals/snapshot.json  -> the web UI and the REST API (/api/vitals),
    which is also what your native app polls. Live, no manual "probe" needed.
  * Home Assistant via MQTT discovery (auto-creates entities) — enable by
    setting OWLET_MQTT_HOST (+ optional _PORT/_USER/_PASS/_PREFIX).
  * /config/vitals/overlay-<cam>.txt -> the burned-in video HUD (read by ffmpeg
    drawtext). Written here so it's ready the moment the overlay is switched on.

Sock vitals come from Owlet's Ayla cloud (needs APP_ACTIVE keep-alive, handled
in OwletVitals). Cam sensors are written continuously by tutk_client from the
live TUTK stream; we just merge them in.

Disable entirely with OWLET_VITALS_POLL=0.
"""

from __future__ import annotations

import json
import os
import time

import config_store as cs

POLL = int(os.environ.get("OWLET_VITALS_INTERVAL") or "15")
SNAP = os.path.join(cs.VITALS_DIR, "snapshot.json")

STATE_PREFIX = "owlet"

# US/Imperial: report temperatures in °F.
TEMP_KEYS = ("temperature", "skin_temperature")


def _f(c):
    try:
        return round(c * 9 / 5 + 32)
    except (TypeError, ValueError):
        return c


def _to_us(sensors: dict) -> dict:
    out = dict(sensors)
    for k in TEMP_KEYS:
        if out.get(k) is not None:
            out[k] = _f(out[k])
    return out


# ---- Home Assistant entity metadata -----------------------------------------
# key -> (friendly, unit, device_class, is_binary)
HA_META = {
    "heart_rate":        ("Heart rate", "bpm", None, False),
    "oxygen":            ("Oxygen", "%", None, False),
    "oxygen_avg":        ("Oxygen (10-min avg)", "%", None, False),
    "skin_temperature":  ("Skin temperature", "°F", "temperature", False),
    "battery":           ("Sock battery", "%", "battery", False),
    "battery_minutes":   ("Battery remaining", "min", "duration", False),
    "signal_strength":   ("Sock signal", "dBm", "signal_strength", False),
    "sleep_state":       ("Sleep state", None, None, False),
    "movement":          ("Movement", None, None, False),
    "temperature":       ("Room temperature", "°F", "temperature", False),
    "humidity":          ("Room humidity", "%", "humidity", False),
    "noise":             ("Room noise", "dB", "sound_pressure", False),
    "brightness":        ("Room brightness", "lx", "illuminance", False),
    "wifi_rssi":         ("Cam WiFi", "dBm", "signal_strength", False),
    "base_station_on":   ("Base station", None, "running", True),
    "charging":          ("Sock charging", None, "battery_charging", True),
    "motion":            ("Motion", None, "motion", True),
    "sound":             ("Sound", None, "sound", True),
}


class _Mqtt:
    """Wraps a paho MQTT client; (re)connects when the UI/env settings change."""

    def __init__(self, log):
        self.log = log
        self.c = None
        self._announced: set[str] = set()
        self._key = None          # settings fingerprint we're connected with
        self.prefix = "homeassistant"

    def ensure(self, s: dict):
        """Connect/disconnect/reconnect to match settings dict `s`. Retries each
        poll while disconnected — `self._key` is only committed AFTER a successful
        connect, so a broker that's down at boot gets retried (instead of being
        stuck 'connected' to nothing forever)."""
        key = (s.get("enabled"), s.get("host"), s.get("port"),
               s.get("user"), s.get("password"), s.get("prefix"))
        # already connected with these exact settings -> nothing to do
        if key == self._key and self.c is not None:
            return
        # settings changed -> tear the old connection down
        if key != self._key:
            self._disconnect()
            self._announced.clear()
            self._key = key
        if not s.get("enabled") or not s.get("host"):
            return
        if self.c is not None:
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.log("[vitals] MQTT enabled but paho-mqtt isn't installed")
            return
        try:
            self.prefix = s.get("prefix") or "homeassistant"
            # paho 2.x requires a CallbackAPIVersion; 1.x has no such arg.
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1) \
                if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client()
            if s.get("user"):
                c.username_pw_set(s["user"], s.get("password") or "")
            c.will_set(f"{STATE_PREFIX}/bridge/status", "offline", retain=True)
            c.connect(s["host"], int(s.get("port") or 1883), 60)
            c.loop_start()
            c.publish(f"{STATE_PREFIX}/bridge/status", "online", retain=True)
            self.c = c
            self.log(f"[vitals] MQTT connected to {s['host']}:{s.get('port')}")
        except Exception as e:  # noqa: BLE001
            self.log(f"[vitals] MQTT connect failed ({e}); will retry")

    def _disconnect(self):
        if self.c:
            try:
                self.c.loop_stop()
                self.c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.c = None

    @staticmethod
    def _slug(s: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in str(s)).strip("_").lower()

    def publish_device(self, dev: dict):
        if not self.c:
            return
        dsn = dev.get("dsn") or dev.get("name") or "owlet"
        node = self._slug(dsn)
        kind = dev.get("kind", "device")
        sensors = _to_us(dev.get("sensors") or {})
        state_topic = f"{STATE_PREFIX}/{node}/state"
        dev_info = {
            "identifiers": [f"owlet_{node}"],
            "name": f"Owlet {kind.title()} {dev.get('name', dsn)}",
            "manufacturer": "Owlet",
            "model": dev.get("model") or kind,
        }
        for key, val in sensors.items():
            meta = HA_META.get(key)
            if not meta or val is None:
                continue
            friendly, unit, dclass, is_bin = meta
            comp = "binary_sensor" if is_bin else "sensor"
            uid = f"owlet_{node}_{key}"
            if uid not in self._announced:
                cfg = {
                    "name": friendly,
                    "state_topic": state_topic,
                    "value_template": "{{ value_json.%s }}" % key,
                    "unique_id": uid,
                    "object_id": uid,
                    "device": dev_info,
                    "availability_topic": f"{STATE_PREFIX}/bridge/status",
                }
                if unit:
                    cfg["unit_of_measurement"] = unit
                if dclass:
                    cfg["device_class"] = dclass
                if is_bin:
                    cfg["payload_on"] = 1
                    cfg["payload_off"] = 0
                self.c.publish(f"{self.prefix}/{comp}/{uid}/config",
                               json.dumps(cfg), retain=True)
                self._announced.add(uid)
        self.c.publish(state_topic, json.dumps(sensors), retain=True)


def _write_overlays(devices: list[dict], log):
    """Compose the burned-in HUD text per camera (cam env + a paired sock)."""
    cams = [d for d in devices if d.get("kind") == "cam"]
    socks = [d for d in devices if d.get("kind") == "sock"]
    sock = socks[0] if len(socks) == 1 else None  # only auto-pair a lone sock
    for cam in cams:
        s = _to_us(cam.get("sensors") or {})
        parts = []
        if s.get("temperature") is not None:
            parts.append(f"{s['temperature']}°F")
        if s.get("humidity") is not None:
            parts.append(f"{s['humidity']}% RH")
        if sock:
            sv = sock.get("sensors") or {}
            if sv.get("heart_rate") is not None:
                parts.append(f"HR {sv['heart_rate']}")
            if sv.get("oxygen") is not None:
                parts.append(f"O2 {sv['oxygen']}%")
        if not parts:
            continue
        line = "   ".join(parts)
        try:
            path = os.path.join(cs.VITALS_DIR, "overlay-%s.txt" % cam["name"])
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(line)
            os.replace(tmp, path)
        except OSError:
            pass


def _read_cam_sidecars() -> list[dict]:
    out = []
    for name in cs.camera_names():
        try:
            with open(cs.cam_sensors_path(name)) as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError):
            continue
        sensors = {k: v for k, v in data.items() if k != "ts"}
        if sensors:
            out.append({"dsn": name, "name": name, "model": "Owlet Cam",
                        "kind": "cam", "sensors": sensors, "ts": data.get("ts")})
    return out


def main() -> None:
    def log(m):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), m, flush=True)

    if os.environ.get("OWLET_VITALS_POLL", "1") == "0":
        return
    time.sleep(12)  # let webapp + first stream come up
    from owlet_api import OwletAPI, OwletError
    from owlet_vitals import OwletVitals

    mq = _Mqtt(log)
    api = None
    fails = 0   # consecutive cloud failures -> backoff (don't hammer login)
    while True:
        devices: list[dict] = []
        cfg = cs.load_config()
        mq.ensure(cs.mqtt_settings(cfg))   # pick up UI changes live
        if cfg.get("email") and cfg.get("password"):
            try:
                if api is None:
                    api = OwletAPI(cfg["region"], cfg["email"], cfg["password"])
                devices = OwletVitals(api).snapshot()
                fails = 0
            except OwletError as e:
                # auth/login failure -> drop the session and re-auth next time
                log(f"[vitals] login error: {e}")
                api = None
                fails += 1
            except Exception as e:  # noqa: BLE001
                # transient (network/JSON) -> KEEP the warm session, just back off.
                # Re-authing on every blip risks Owlet rate-limiting the account.
                log(f"[vitals] poll error: {e}")
                fails += 1

        # merge live cam sensors from tutk_client sidecars (cloud-independent)
        cam_devs = _read_cam_sidecars()
        seen = {d.get("dsn") for d in devices}
        devices += [c for c in cam_devs if c["dsn"] not in seen]

        # Always refresh the snapshot (even when empty) so /api/vitals can tell
        # "no data yet" from "stale": it carries ts + an ok flag every loop.
        try:
            os.makedirs(cs.VITALS_DIR, exist_ok=True)
            with open(SNAP + ".tmp", "w") as f:
                json.dump({"ts": time.time(), "ok": bool(devices), "devices": devices}, f)
            os.replace(SNAP + ".tmp", SNAP)
        except OSError as e:
            log(f"[vitals] snapshot write failed: {e}")
        if devices:
            _write_overlays(devices, log)
            for d in devices:
                mq.publish_device(d)

        # exponential backoff on sustained cloud failure (cap ~5 min)
        delay = POLL if fails == 0 else min(POLL * (2 ** min(fails, 5)), 300)
        time.sleep(delay)


if __name__ == "__main__":
    main()
