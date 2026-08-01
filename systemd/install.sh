#!/bin/bash
# Install the user units on Linux. The macOS equivalent is launchd/install.sh.
#
#   ./systemd/install.sh            # all units
#   ./systemd/install.sh stream     # just one
#
# User units, not system ones: this runs as you, reads your config.toml and
# writes into your checkout. Nothing here needs root.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNITS=(bmw-cardata-stream.service bmw-cardata-prune.service
       bmw-cardata-prune.timer bmw-cardata-map.service)

command -v systemctl >/dev/null || { echo "systemd not found. On macOS use launchd/install.sh." >&2; exit 1; }
[ -x "$ROOT/.venv/bin/python" ] || { echo "No venv at $ROOT/.venv -- run ./install.sh first." >&2; exit 1; }

mkdir -p "$DEST"
want=("${UNITS[@]}")
[ $# -gt 0 ] && want=(bmw-cardata-"$1".service)

for unit in "${want[@]}"; do
    src="$ROOT/systemd/$unit"
    [ -f "$src" ] || { echo "no such unit: $unit" >&2; exit 1; }
    # Only rewrite when the content actually differs, so a re-run does not
    # restart services that were working perfectly.
    tmp="$(mktemp)"
    sed "s|__ROOT__|$ROOT|g" "$src" > "$tmp"
    if cmp -s "$tmp" "$DEST/$unit"; then
        echo "  unchanged: $unit"
        rm -f "$tmp"
        continue
    fi
    mv "$tmp" "$DEST/$unit"
    echo "  installed: $unit"
done

systemctl --user daemon-reload
systemctl --user enable --now bmw-cardata-stream.service bmw-cardata-map.service bmw-cardata-prune.timer

# Without this the units stop the moment you log out, which is not what anyone
# wants from something whose whole job is to keep capturing.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    echo
    echo "  Tip: enable lingering so capture survives logout:"
    echo "      sudo loginctl enable-linger $USER"
fi

echo
systemctl --user --no-pager --lines=0 status bmw-cardata-stream.service || true
