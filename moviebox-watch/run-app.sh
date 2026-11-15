#!/usr/bin/env bash
# run-app.sh — launch MovieBox-TUI in the persistent tmux session "moviebox".
# The phone view must NEVER be forced wide: the window always follows the
# smallest attached client (the user's phone, ~58 cols). No widening anywhere.
set -euo pipefail
WATCH_DIR="$HOME/moviebox-watch"
cd "$WATCH_DIR"

# Phone-size window policy (persisted here so every restart restores it).
# window-size smallest = follow the smallest client; aggressive-resize off.
tmux set-option -g window-size smallest 2>/dev/null || true
tmux set-option -g aggressive-resize off 2>/dev/null || true

if ! tmux has-session -t moviebox 2>/dev/null; then
    tmux new-session -d -s moviebox -x 80 -y 24 "bash $WATCH_DIR/app.sh"
    sleep 1
fi
# Apply the phone-size policy to the live session too.
tmux set-option -t moviebox window-size smallest 2>/dev/null || true
tmux set-option -t moviebox aggressive-resize off 2>/dev/null || true
echo "moviebox session running"
tmux display-message -t moviebox -p '#{window_width}x#{window_height}' 2>/dev/null || true
