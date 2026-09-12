#!/system/bin/sh
# owlet-bridge entrypoint (replaces the stock termux-docker /entrypoint.sh).
#
# The stock entrypoint re-execs through `su … env -i` with a FIXED whitelist of
# variables, which silently wiped every `docker -e` var the bridge understands
# (OWLET_*, PUBLIC_*, GO2RTC_*, TUTK_*, CONFIG_PATH, ENV_PATH) before the app ever
# saw them — so e.g. OWLET_WEBRTC_CANDIDATE / PUBLIC_RTSP_PORT from the Unraid
# template did nothing. This version forwards those through; everything else is
# the stock behaviour (drop to the `system` user, wipe the rest of the env, run
# the CMD). Each forwarded var is passed as its own argv word, so values with
# spaces survive.

if [ $# -lt 1 ]; then
	set -- login
fi

if [ "$(id -u)" != "0" ]; then
	exec "$@"
fi

while IFS= read -r _kv; do
	case "$_kv" in
		OWLET_*=*|PUBLIC_*=*|GO2RTC_*=*|TUTK_*=*|CONFIG_PATH=*|ENV_PATH=*)
			set -- "$_kv" "$@" ;;
	esac
done <<EOF
$(env)
EOF

exec /system/bin/su -s "$PREFIX/bin/env" system -- \
	-i \
	ANDROID_DATA="$ANDROID_DATA" \
	ANDROID_ROOT="$ANDROID_ROOT" \
	HOME="$HOME" \
	LANG="$LANG" \
	PATH="$PATH" \
	PREFIX="$PREFIX" \
	TMPDIR="$TMPDIR" \
	TZ="$TZ" \
	TERM="$TERM" \
	"$@"
