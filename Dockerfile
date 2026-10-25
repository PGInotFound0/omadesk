# omadesk — server container
#
# Quick install (one command, Linux desktop already runs Wayland):
#
#   docker run -d --name omadesk --net host \
#     -u 1000:1000 -e HOME=/persist \
#     -e WAYLAND_DISPLAY=wayland-1 -e HYPRLAND_INSTANCE_SIGNATURE=$HYPRLAND_INSTANCE_SIGNATURE \
#     -v /run/user/1000:/run/user/1000:ro \
#     -v ~/.config/omadesk:/persist \
#     ghcr.io/pginotfound0/omadesk:latest
#
# (uid must match your desktop user; token persists in ~/.config/omadesk on the host)
# See also docker-compose.yml.

FROM python:3.14-slim

LABEL org.opencontainers.image.title="omadesk" \
      org.opencontainers.image.description="AnyDesk-style remote viewer server for Wayland/Hyprland — WebRTC H.264 + Opus to phone browsers" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/PGInotFound0/omadesk"

# runtime helpers used as subprocesses:
#   pipewire-audio  → pw-record (desktop audio from the sink monitor)
#   pulseaudio-utils → pactl (default-sink discovery)
#   wl-clipboard    → wl-copy / wl-paste (clipboard sync)
# gcc/python3-dev only needed to compile pywayland's C extension — purged after.
RUN apt-get update && apt-get install -y --no-install-recommends \
        pipewire-audio \
        pulseaudio-utils \
        wl-clipboard \
        tini \
        gcc \
        python3-dev \
    && pip install --no-cache-dir pywayland aiortc aiohttp segno numpy \
    && apt-get purge -y --auto-remove gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

# app + protocol bindings (wayland.xml is vendored so no build-time pkg-config)
COPY omadesk/ /app/omadesk/
COPY web/ /app/web/
COPY scripts/ /app/scripts/
COPY extra/wayland.xml /usr/share/wayland/wayland.xml
RUN python /app/scripts/gen_protocols.py

RUN useradd -ms /bin/bash -u 1000 omadesk
ENV WAYLAND_DISPLAY=wayland-1 \
    XDG_RUNTIME_DIR=/run/user/1000 \
    PYTHONUNBUFFERED=1
EXPOSE 9983

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "omadesk.server_cli", "run", "--port", "9983", "--bind", "0.0.0.0"]
