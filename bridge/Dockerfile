# owlet-bridge: go2rtc + adb + scrcpy + ffmpeg(NVENC) + watchdog
# Captures the redroid Android display and re-serves it as RTSP for Frigate.
FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG GO2RTC_VERSION=1.9.4
ARG SCRCPY_VERSION=2.7

# Core deps: adb (capture/control), ffmpeg (encode), imagemagick (optional
# frozen-frame detection), SDL/USB libs for the prebuilt scrcpy binary.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl bash tzdata procps \
        android-tools-adb \
        ffmpeg \
        imagemagick \
        libsdl2-2.0-0 libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# go2rtc (single static binary)
RUN curl -fsSL -o /usr/local/bin/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_amd64" \
    && chmod +x /usr/local/bin/go2rtc

# scrcpy (prebuilt) — optional low-latency capture path. Non-fatal if it fails;
# the bridge falls back to `adb screenrecord` which needs only android-tools.
RUN set -e; \
    if curl -fsSL -o /tmp/scrcpy.tar.gz \
        "https://github.com/Genymobile/scrcpy/releases/download/v${SCRCPY_VERSION}/scrcpy-linux-x86_64-v${SCRCPY_VERSION}.tar.gz"; then \
        mkdir -p /opt/scrcpy && tar -xzf /tmp/scrcpy.tar.gz -C /opt/scrcpy && rm /tmp/scrcpy.tar.gz; \
        SC="$(find /opt/scrcpy -maxdepth 2 -type f -name scrcpy | head -n1)"; \
        [ -n "$SC" ] && ln -sf "$SC" /usr/local/bin/scrcpy || true; \
    else \
        echo "WARN: scrcpy download skipped; screenrecord fallback only"; \
    fi

# scrcpy uses the system adb and bundled server jar
ENV ADB=/usr/bin/adb \
    SCRCPY_SERVER_PATH=/opt/scrcpy/scrcpy-server

COPY entrypoint.sh capture.sh watchdog.sh go2rtc.yaml /app/
RUN chmod +x /app/entrypoint.sh /app/capture.sh /app/watchdog.sh

WORKDIR /app

# Sensible defaults; override via env / compose / Unraid template.
ENV ADB_DEVICE=redroid:5555 \
    OWLET_PACKAGE=com.owletcare.owletcare \
    SCREEN_SIZE=1280x720 \
    SCREEN_DENSITY=240 \
    FPS=15 \
    CAPTURE_METHOD=auto \
    ENCODER=auto \
    BITRATE=4000000 \
    CROP= \
    WATCHDOG_INTERVAL=120 \
    WATCHDOG_APP_GUARD=true \
    TAP_X=640 \
    TAP_Y=360

# 8554 RTSP, 1984 go2rtc API/web UI, 8555 WebRTC
EXPOSE 1984 8554 8555

ENTRYPOINT ["/app/entrypoint.sh"]
