# owlet-native-bridge — the wyze-bridge pattern for Owlet/Kalay cameras.
# TUTK client (ctypes over the app's own libs) -> go2rtc -> RTSP/WebRTC/HLS + UI.
#
# The TUTK .so libs are NOT shipped here (they're proprietary, pulled from the
# Owlet APK you own). Mount them at /app/libs or COPY them in before building:
#   native-bridge/tutk/libs/<abi>/libIOTCAPIs.so, libAVAPIs.so, ...
FROM python:3.12-slim

ARG GO2RTC_VERSION=1.9.4

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl ffmpeg libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_amd64" \
    && chmod +x /usr/local/bin/go2rtc

RUN pip install --no-cache-dir requests

WORKDIR /app
COPY owlet_tutk_client.py owlet_auth.py go2rtc.yaml /app/
# Provide the SDK libs at build (COPY libs/ /app/libs/) or mount at runtime.
COPY libs /app/libs

ENV TUTK_LIB_DIR=/app/libs/x86_64 \
    LD_LIBRARY_PATH=/app/libs/x86_64

EXPOSE 1984 8554 8555
CMD ["go2rtc", "-config", "/app/go2rtc.yaml"]
