# Bridge hardening — full-audit fix pass

A multi-agent audit (find → adversarially verify → synthesize) produced 92
code-verified findings. This pass implements the critical/high/medium items and
many quick-win lows. Summary of what changed and why.

## Runtime / Docker
- **Docker `-e` env vars now actually reach the app.** The Termux entrypoint
  `su`'d to uid 1000 and wiped the environment, so every `OWLET_*`/`PUBLIC_*`/
  `GO2RTC_*` var was silently dropped at boot. New `owlet-entrypoint.sh`
  snapshots them (before the wipe) to `/owlet-docker-env.raw`; `start-bionic.sh`
  restores them with `export "$k=$v"` (literal, no eval — injection-safe).
  Fail-safe: always hands off to the real entrypoint.
- `procps` installed (run-cam.sh's reaping uses `ps`/`fuser`).
- `paho-mqtt` pinned `<2.0` (2.x `Client()` needs a `CallbackAPIVersion`); code
  is also version-aware.
- `EXPOSE` now includes `8555/udp`; Unraid template adds `--init`.

## Stream reliability (single-session camera)
- **`#killsignal=15&killtimeout=5`** on the exec source so go2rtc SIGTERMs
  (not SIGKILLs) — run-cam.sh's reaping trap can now actually run.
- **run-cam.sh** rewritten: the two stages run as separate PIDs joined by a video
  FIFO and `wait -n`, so a dead first stage can't deadlock the wrapper; cleanup
  kills both + `fuser -k`s the FIFOs (works even if PGID lookup fails); reaps a
  leaked prior holder on start; falls back to `/config/owlet.env`.
- **Config saves only restart go2rtc when the generated config changed** (hash
  gate) — a UI/MQTT/password edit no longer bounces every camera's P2P session.
- keepalive: `-rw_timeout` + per-camera exponential backoff (no 5s respawn storm).
- Baked-in fallback `go2rtc.yaml` delegates to run-cam.sh too (no orphan pipe).

## Config integrity & injection
- **Atomic writes** (tmp + fsync + `os.replace`) for the config, env files and the
  generated go2rtc config — a crash/SIGKILL mid-write can't wipe the config.
- Corrupt JSON is backed up to `owlet.yaml.bad` instead of silently parsing to an
  empty config that the next save would persist.
- Per-camera `.env` values are single-quote-escaped (kills `$(...)` injection via
  any saved field).
- `webrtc_candidate` validated + YAML-quoted; camera names re-slugified before
  interpolation.

## TUTK / talk path (the "no audible beep")
- Speaker `FRAMEINFO_t` rebuilt to the standard layout (codec@0, flags@2, ts@8 —
  was ts@12); codec/flags now taken from the live audio probe; env overrides
  (`OWLET_SPEAKER_CODEC_ID/FLAGS`, `OWLET_TALK_RATE`).
- `avSendAudioData` result + per-burst sent/rejected counts are logged (the path
  was failing silently). *Note: confirming the exact speaker codec/IOTYPEs still
  needs on-device validation — this makes it correct-by-construction + diagnosable.*
- **AV-call lock** serializes `avSendIOCtrl`/`avRecvIOCtrl`/`avSendAudioData`
  across the talk + sensor threads (ctypes releases the GIL → was a real
  segfault/queue-corruption risk).
- Teardown reordered: `avClientStop` before thread joins (was a use-after-free on
  reconnect).
- `wifi_rssi` only read when the IOCTL response actually carries the byte;
  optional `OWLET_TEMP_SCALE` knob.

## Backend / security
- UI Basic-auth is now **configurable in the UI** (Account → Advanced) and read
  from config (env-strip-proof), compared with `hmac.compare_digest`.
- **Same-origin guard** on mutating requests (blocks browser CSRF; native app
  clients and curl are unaffected — they send no `Origin`).
- `play`/`talk`/`talk_stop`/snapshot now reject unknown camera names (path-
  traversal / `?src=` injection).
- Talk temp-file cleanup binds to the exact ffmpeg proc; single-writer FIFO
  (`old.wait()` before the new writer); go2rtc `/api/streams` cached ~1s so polls
  don't stall during restarts; cam-diagnose test-and-set is atomic.
- The raw whole-device `json.dumps` dump (could carry tokens) is replaced with a
  field-name summary.

## GUI
- HTML-escape untrusted device/cloud strings before `innerHTML` (XSS).
- Talk/play error toasts read the real `message`; account password re-masks after
  save; "Test login" works for an already-saved account; vitals show a stale
  badge and use the newest device timestamp.

See the session transcript for the full 92-item plan; remaining items are
low-severity polish.
