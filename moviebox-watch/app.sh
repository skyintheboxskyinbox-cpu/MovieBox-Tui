#!/usr/bin/env bash
# app.sh — runs the MovieBox-TUI binary inside the tmux session.
# Run by run-app.sh. If the binary is missing, try to install it from the
# public repo (cargo) or a release, then run it.
set -u
WATCH_DIR="$HOME/moviebox-watch"
BIN="$(command -v moviebox-tui || true)"

if [ -z "$BIN" ]; then
    echo "moviebox-tui not found, trying to install..."
    if command -v cargo >/dev/null 2>&1; then
        cargo install --git https://github.com/mesamirh/MovieBox-Tui --locked 2>/dev/null || \
        cargo install --git https://github.com/mesamirh/MovieBox-Tui 2>/dev/null || true
        BIN="$(command -v moviebox-tui || true)"
    fi
    if [ -z "$BIN" ] && [ -x "$HOME/.cargo/bin/moviebox-tui" ]; then
        BIN="$HOME/.cargo/bin/moviebox-tui"
    fi
fi

if [ -z "$BIN" ]; then
    echo "ERROR: moviebox-tui binary not found and install failed."
    echo "Install manually: cargo install --git https://github.com/mesamirh/MovieBox-Tui"
    echo "Then run: bash $WATCH_DIR/run-app.sh"
    exec bash
fi

# Downloads live here (the app's own setting; also used by sync.sh/server).
mkdir -p "$HOME/Downloads/MovieBox-TUI"
exec "$BIN"
