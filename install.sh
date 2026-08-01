#!/bin/bash
# One-shot setup for a fresh Mac.
#
#   curl -fsSL https://raw.githubusercontent.com/peterkoczan/bmw-cardata/main/install.sh | bash
#
# or, from an existing clone:  ./install.sh
#
# Clones (or updates) the repo, builds the venv, sets up PostgreSQL, installs
# the launchd agents, and leaves you at the menu bar item to finish the BMW
# portal steps. Nothing here needs your BMW credentials -- authorisation happens
# later, in the browser, through the menu bar.
set -euo pipefail

REPO_URL="https://github.com/peterkoczan/bmw-cardata.git"
TARGET="${BMWCD_DIR:-$HOME/Developer/bmw-cardata}"
PG_FORMULA="postgresql@17"
# Overridable so the installer can be exercised end to end without going
# anywhere near a real one. BMWCD_DB=bmwcardata_test ./install.sh
DB_NAME="${BMWCD_DB:-bmwcardata}"
# Set to skip loading the launchd agents -- a test run should not take over the
# agents that are currently capturing.
SKIP_AGENTS="${BMWCD_SKIP_AGENTS:-}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

ask() {  # ask "question" -> 0 if yes. Defaults to no when non-interactive.
    if [ ! -t 0 ]; then info "(non-interactive: assuming no) $1"; return 1; fi
    read -r -p "    $1 [y/N] " reply
    [[ "$reply" =~ ^[Yy] ]]
}

# --- preflight ---------------------------------------------------------------

[ "$(uname -s)" = "Darwin" ] || die "macOS only: this uses launchd and a macOS menu bar app."

command -v git >/dev/null || die "git not found. Install the Xcode command line tools: xcode-select --install"

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 not found. Install Python 3.11+ (brew install python)."
"$PYTHON" - <<'EOF' || die "Python 3.11+ required (tomllib). Try: brew install python"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
EOF
info "python: $("$PYTHON" --version)"

# --- clone or update ---------------------------------------------------------

# An explicit BMWCD_DIR wins over "you ran me from inside a checkout". Without
# this, running the script by path from a real clone silently ignored the
# directory asked for and operated on the clone instead -- which, while testing
# the installer, pointed a scratch run straight at the live database. The schema
# is idempotent so nothing was lost, but nothing about that was by design.
if [ -z "${BMWCD_DIR:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/bmwcd/__init__.py" ]; then
    TARGET="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    say "Using this clone: $TARGET"
elif [ -d "$TARGET/.git" ]; then
    say "Updating existing clone at $TARGET"
    git -C "$TARGET" pull --ff-only
else
    say "Cloning into $TARGET"
    mkdir -p "$(dirname "$TARGET")"
    git clone --depth 1 "$REPO_URL" "$TARGET"
fi
cd "$TARGET"

# --- python environment ------------------------------------------------------

say "Python environment"
[ -d .venv ] || "$PYTHON" -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
info "installed into $TARGET/.venv"

# --- postgres ----------------------------------------------------------------

say "PostgreSQL"
if command -v brew >/dev/null; then
    BREW_PREFIX="$(brew --prefix)"
    export PATH="$BREW_PREFIX/opt/$PG_FORMULA/bin:$PATH"
fi

if ! command -v psql >/dev/null; then
    if command -v brew >/dev/null && ask "PostgreSQL not found. Install $PG_FORMULA with Homebrew?"; then
        brew install "$PG_FORMULA"
        export PATH="$(brew --prefix)/opt/$PG_FORMULA/bin:$PATH"
    else
        die "PostgreSQL required. Install it, then re-run this script."
    fi
fi

if command -v brew >/dev/null && ! pg_isready -q 2>/dev/null; then
    info "starting $PG_FORMULA"
    brew services start "$PG_FORMULA" >/dev/null || true
    for _ in $(seq 1 20); do pg_isready -q 2>/dev/null && break; sleep 1; done
fi
pg_isready -q 2>/dev/null || die "PostgreSQL is not accepting connections."

if psql -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw "$DB_NAME"; then
    info "database $DB_NAME already exists"
else
    createdb "$DB_NAME"
    info "created database $DB_NAME"
fi

# --- config and schema -------------------------------------------------------

say "Configuration"
if [ -f config.toml ]; then
    info "config.toml already present, leaving it alone"
else
    cp config.example.toml config.toml
    if [ "$DB_NAME" != "bmwcardata" ]; then
        # Keep config.toml pointing at the same database the installer made.
        sed -i.bak "s|postgresql:///bmwcardata|postgresql:///$DB_NAME|" config.toml
        rm -f config.toml.bak
        info "config.toml points at $DB_NAME"
    fi
    info "config.toml created from the example (Client ID is filled in later)"
fi

./.venv/bin/python -m bmwcd initdb
./.venv/bin/python -m bmwcd catalogue || info "catalogue fetch failed; retry later with: bmwcd catalogue"

# --- launchd -----------------------------------------------------------------

say "Background agents"
if [ -n "$SKIP_AGENTS" ]; then
    info "BMWCD_SKIP_AGENTS set, leaving launchd alone"
elif [ "$(uname -s)" = "Darwin" ]; then
    ./launchd/install.sh
else
    ./systemd/install.sh
fi

# --- done --------------------------------------------------------------------

say "Done"
cat <<EOF

    Look for the ⚙️ icon in your menu bar, then choose
    "Set up / re-authorise…". It shows exactly what to do in the
    My BMW portal, takes the Client ID, and runs the sign-in for you.

    The icon then tells you the state at a glance:
      🟢 streaming, heard from the car recently
      🟡 quiet for 6h+ (usually just parked)
      🟠 database unreachable
      🔴 subscriber not running

    Nothing streams until the portal steps are done, because the
    data selection is configured per vehicle on BMW's side.

    Installed at: $TARGET

EOF
