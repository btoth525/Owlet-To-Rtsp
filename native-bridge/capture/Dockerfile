# owlet-capture — one-shot TUTK/Kalay AuthKey capture appliance (throwaway).
# mitmproxy (web UI + addon) + adb + frida-tools + the capture scripts.
# Build context is native-bridge/ so it can bundle mitm/ and probe/.
FROM python:3.12-slim

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        android-tools-adb \
        curl openssl xz-utils ca-certificates procps iproute2 less nano \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir mitmproxy frida-tools

COPY mitm/ /app/mitm/
COPY probe/ /app/probe/
COPY capture/entrypoint.sh /app/entrypoint.sh
RUN find /app -name '*.sh' -exec chmod +x {} \;

WORKDIR /app
ENV OWLET_CAP_DIR=/captures \
    FRIDA_HOST=127.0.0.1:27042
VOLUME ["/captures", "/root/.mitmproxy"]
EXPOSE 8080 8081
ENTRYPOINT ["/app/entrypoint.sh"]
