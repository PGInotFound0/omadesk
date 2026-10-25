#!/bin/sh
# omadesk installer: venv + deps + launcher
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip wheel >/dev/null
.venv/bin/pip install pywayland aiortc aiohttp segno numpy pillow >/dev/null
.venv/bin/python scripts/gen_protocols.py

BIN="$HOME/.local/bin"
mkdir -p "$BIN"
cat > "$BIN/omadesk" <<LAUNCH
#!/bin/sh
exec "$(pwd)/.venv/bin/python" -m omadesk.server_cli "\$@"
LAUNCH
chmod +x "$BIN/omadesk"

echo "installed: $BIN/omadesk"
echo "run it:    $BIN/omadesk run"
