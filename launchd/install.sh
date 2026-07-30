#!/bin/bash
# Install or update the launchd agents. Idempotent.
#
#   ./launchd/install.sh                 # all agents
#   ./launchd/install.sh menubar         # just one, by short name
#
# An agent whose rendered plist is unchanged is left ALONE rather than being
# bootout/bootstrapped. Reinstalling to pick up a menu bar tweak used to drop a
# healthy MQTT session with most of an hour left on its token, and the feed is
# forward-only so that gap is permanent.
#
# __ROOT__ in the templates is replaced with this checkout's path, so the repo
# stays portable rather than hard-coding one machine's home directory.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
ALL=(stream prune menubar)

# Short names on the command line, full labels internally.
if [ $# -gt 0 ]; then
    WANTED=("$@")
else
    WANTED=("${ALL[@]}")
fi

mkdir -p "$AGENTS" "$ROOT/data/logs"

for short in "${WANTED[@]}"; do
    label="nl.koczan.bmw-cardata.${short#nl.koczan.bmw-cardata.}"
    src="$ROOT/launchd/$label.plist"
    dst="$AGENTS/$label.plist"

    if [ ! -f "$src" ]; then
        echo "no such agent: $short (expected $src)" >&2
        exit 1
    fi

    # `&` in a sed replacement expands to the whole match, so a checkout path
    # containing one would silently produce a valid plist pointing at paths that
    # do not exist -- and the script would report success.
    ROOT_ESC="${ROOT//&/\\&}"
    rendered="$(mktemp)"
    sed "s|__ROOT__|$ROOT_ESC|g" "$src" > "$rendered"

    if cmp -s "$rendered" "$dst" && launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
        rm -f "$rendered"
        echo "unchanged, left running: $label"
        continue
    fi

    mv "$rendered" "$dst"
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true

    # bootout can return while SIGTERM is still in flight. Bootstrapping into
    # that window spawns a process that loses the flock race, exits, and then
    # waits out ThrottleInterval -- a 30s outage instead of a 2s one.
    for _ in $(seq 1 20); do
        launchctl print "gui/$UID/$label" >/dev/null 2>&1 || break
        sleep 0.25
    done

    launchctl bootstrap "gui/$UID" "$dst"
    echo "loaded $label"
done

launchctl list | grep bmw-cardata || true
