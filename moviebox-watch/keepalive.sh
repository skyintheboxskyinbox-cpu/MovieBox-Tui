#!/usr/bin/env bash
# keepalive.sh — companion prober (REWRITE, 16 Nov 2026).
# Rewritten from REBUILD-SPEC.md §8.
#
# Observes/touches the same activity surface as the watchdog
# (/api/heartbeat and /media) so the bridge can tell the server is up.
#
# DOCUMENTED FACT — READ THIS: keepalive pokes are USELESS for GitHub's
# idle timer. They do not extend the codespace lifetime. Do NOT present
# them as a guard; the watchdog (watchdog.sh) is the real guard.
#
# That is also why this prober identifies itself with User-Agent
# "moviebox-keepalive": server.py serves those requests but does NOT
# count them as activity. If it did, the watchdog would never see 15
# minutes of idleness and would never stop the codespace (the same bug
# as the 11 Nov 2026 polling bug, when every open tab kept the codespace
# alive forever).
set -u
WATCH_DIR="$HOME/moviebox-watch"
BASE="http://127.0.0.1:8000"
UA="moviebox-keepalive"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') keepalive: $*" >>"$WATCH_DIR/keepalive.log"; }

log "keepalive started (prober only — does NOT extend codespace lifetime)"
while true; do
    sleep 300
    # Observe: is the server answering?
    if curl -fsS -A "$UA" -m 10 "$BASE/api/links" >/dev/null 2>&1; then
        log "server up"
    else
        log "server NOT answering on $BASE"
    fi
    # Touch the heartbeat endpoint (server ignores this UA for activity).
    curl -fsS -A "$UA" -m 10 -X POST "$BASE/api/heartbeat" >/dev/null 2>&1 || true
    # Touch /media with a HEAD on the first library file, if any (also
    # ignored for activity when sent with this UA).
    first="$(ls "$WATCH_DIR/media"/*.mp4 2>/dev/null | head -1 || true)"
    if [ -n "$first" ]; then
        curl -fsS -A "$UA" -m 10 -I \
            "$BASE/media/$(basename "$first")" >/dev/null 2>&1 || true
    fi
done
