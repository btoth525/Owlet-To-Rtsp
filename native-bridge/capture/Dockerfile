# owlet-capture — all-in-one one-time capture appliance for Unraid.
# Bundles mitmproxy (web UI) + adb + frida-tools + the capture scripts so you
# extract the 3 Owlet-specific values without installing anything on a PC.
#
# Build context is native-bridge/ (so it can pull in mitm/ and probe/).
#   docker build -f capture/Dockerfile -t owlet-capture native-bridge/
FROM python:3.12-slim

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        android-tools-adb \
        curl openssl xz-utils ca-certificates procps iproute2 less nano \
    && rm -rf /var/lib/apt/lists/*

# mitmproxy (proxy + web UI + the addon) and frida-tools (drive the app).
RUN pip install --no-cache-dir mitmproxy frida-tools

WORKDIR /app
COPY mitm/   /app/mitm/
COPY probe/  /app/probe/
COPY capture/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && find /app -name '*.sh' -exec chmod +x {} \;

ENV OWLET_CAP_DIR=/captures \
    FRIDA_HOST=127.0.0.1:27042
VOLUME ["/captures", "/root/.mitmproxy"]

EXPOSE 8080 8081
ENTRYPOINT ["/app/entrypoint.sh"]
