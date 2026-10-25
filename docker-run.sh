#!/bin/sh
# omadesk-in-docker one-command installer runner.
# Usage: ./docker-run.sh   (from the repo root)
# Ensures the runtime env is picked up from your session, prints the URL/QR
# from the container logs, keeps the token in ~/.config/omadesk.

set -e
SIG=${HYPRLAND_INSTANCE_SIGNATURE:?export HYPRLAND_INSTANCE_SIGNATURE from your desktop shell}

docker rm -f omadesk >/dev/null 2>&1 || true
docker run -d --name omadesk --net host \
  -u "$(id -u):$(id -g)" \
  -e HOME=/persist \
  -e WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-1} \
  -e HYPRLAND_INSTANCE_SIGNATURE="$SIG" \
  -v "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}:/run/user/$(id -u):ro" \
  -v "$HOME/.config/omadesk:/persist" \
  ghcr.io/pginotfound0/omadesk:latest

sleep 2
docker logs omadesk 2>&1 | sed -n '1,20p'
echo
echo "— attach to logs again anytime with: docker logs -f omadesk"
