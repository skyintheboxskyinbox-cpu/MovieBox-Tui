#!/usr/bin/env bash
# download.sh — yt-dlp fallback for one episode, used by batch.py when the
# app's own download path fails. Usage:
#   bash download.sh "<title>" <season> <episode> [query]
# NOTE: the yt-dlp fallback branch has never been triggered live; keep it as
# a last resort only. Downloads land in the app's download directory.
set -u
TITLE="${1:-}"; SEASON="${2:-1}"; EPISODE="${3:-1}"; QUERY="${4:-}"
OUT="$HOME/Downloads/MovieBox-TUI/Series/$TITLE/Season $SEASON"
mkdir -p "$OUT"
SEARCH="${QUERY:-$TITLE S$(printf '%02d' "$SEASON")E$(printf '%02d' "$EPISODE")}"
command -v yt-dlp >/dev/null 2>&1 || { echo "yt-dlp not installed"; exit 1; }
yt-dlp -f "bv*[height<=1080]+ba/b[height<=1080]" --merge-output-format mp4 \
    --no-playlist -o "$OUT/$TITLE - S$(printf '%02d' "$SEASON")E$(printf '%02d' "$EPISODE").%(ext)s" \
    "ytsearch1:$SEARCH"
