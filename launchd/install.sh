#!/bin/bash
# Install the launchd agents. Idempotent -- re-run after editing a plist.
#
# __ROOT__ in the templates is replaced with this checkout's path, so the repo
# stays portable rather than hard-coding one machine's home directory.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(
    nl.koczan.bmw-cardata.stream
    nl.koczan.bmw-cardata.prune
    nl.koczan.bmw-cardata.menubar
)

mkdir -p "$AGENTS" "$ROOT/data/logs"

for label in "${LABELS[@]}"; do
    src="$ROOT/launchd/$label.plist"
    dst="$AGENTS/$label.plist"
    sed "s|__ROOT__|$ROOT|g" "$src" > "$dst"

    # bootout first so an edited plist actually takes effect; ignore the error
    # when it was not loaded to begin with.
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$dst"
    echo "loaded $label"
done

launchctl list | grep bmw-cardata || true
