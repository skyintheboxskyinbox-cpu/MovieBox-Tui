#!/usr/bin/env bash
# sync.sh — sweep the app's download directory into the web library.
# Runs as a loop (~every 60 s) from start.sh. Moves finished video/audio
# files into media/ (fragments + sibling audio stay together so the packer
# can merge them), dedupes, and calls the packer.
set -u
WATCH_DIR="$HOME/moviebox-watch"
SRC="$HOME/Downloads/MovieBox-TUI"
DEST="$WATCH_DIR/media"
mkdir -p "$DEST"

sweep_once() {
    # Depth-4 walk (Movies/<Title>/..., Series/<Title>/Season N/...).
    find "$SRC" -maxdepth 4 -type f \( -name '*.mp4' -o -name '*.mkv' -o -name '*.webm' -o -name '*.m4v' -o -name '*.m4a' \) -print0 2>/dev/null |
    while IFS= read -r -d '' f; do
        case "$f" in *.temp|*.part|*.part.json) continue;; esac
        base="$(basename "$f")"
        if [ ! -e "$DEST/$base" ]; then
            mv "$f" "$DEST/$base" 2>/dev/null || true
        fi
    done
    # Pack anything that arrived: fragments (.f<digits>.mp4) get a .full.mp4.
    bash "$WATCH_DIR/audio-repair.sh" sweep 2>/dev/null || true
}

if [ "${1:-}" = "once" ]; then
    sweep_once
    exit 0
fi

while true; do
    sweep_once
    sleep 60
done
