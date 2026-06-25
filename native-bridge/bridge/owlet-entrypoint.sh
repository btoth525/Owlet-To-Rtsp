#!/bin/bash
# owlet-entrypoint.sh — runs as root (PID 1) BEFORE Termux's /entrypoint.sh
# su's to uid 1000 and wipes the environment to a tiny allowlist. We snapshot the
# docker `-e` vars we care about to a file that survives the su, so
# start-bionic.sh can restore them (without this, OWLET_*/PUBLIC_*/GO2RTC_* env
# vars silently never reach the app).
#
# Fail-safe: the snapshot is best-effort; we ALWAYS hand off to the real Termux
# entrypoint so the container boots exactly as before even if the snapshot fails.
env 2>/dev/null | grep -E '^(OWLET_|PUBLIC_|GO2RTC_)' > /owlet-docker-env.raw 2>/dev/null || true
chmod 0644 /owlet-docker-env.raw 2>/dev/null || true
exec /entrypoint.sh /app/start-bionic.sh
