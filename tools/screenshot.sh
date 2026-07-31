#!/bin/bash
# Regenerate docs/screenshot.png from docs/demo.html using headless Chrome.
#
# Served over HTTP rather than opened as file:// -- Chrome's headless renderer
# applies stricter local-file rules and the map tiles do not load reliably.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT="${PORT:-8901}"

[ -f "$ROOT/docs/demo.html" ] || python3 "$ROOT/tools/make_demo.py"

python3 -m http.server "$PORT" --directory "$ROOT/docs" >/dev/null 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
sleep 1

"$CHROME" --headless --disable-gpu --hide-scrollbars \
    --window-size=1600,1100 \
    --virtual-time-budget=30000 \
    --screenshot="$ROOT/docs/screenshot.png" \
    "http://localhost:$PORT/demo.html" >/dev/null 2>&1

echo "wrote $ROOT/docs/screenshot.png"
