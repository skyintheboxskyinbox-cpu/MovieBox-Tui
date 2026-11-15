#!/usr/bin/env bash
# watchdog.sh — Codespace idle guard (REWRITE, 16 Nov 2026).
# Rewritten from REBUILD-SPEC.md §7.
#
# Watches activity: /api/heartbeat (the page sends it every 30 s ONLY
# while visible) and /media requests — both recorded by server.py in
# last-activity.txt. If there is NO activity for a 15-minute idle window,
# the codespace is stopped (gh codespace stop).
#
# Guards (never stop while these hold):
#   * a batch is running (batch-state.json says running) — even if the
#     page is hidden;
#   * a playback session is live (playback-session.json: file + duration)
#     — it counts as activity until its duration passes, and only then
#     does the 15-minute window start.
#
# HISTORY: the watchdog was KILLED on 14 Nov 2026 after it stopped the
# codespace mid-download. Until start.sh re-runs (which re-arms it), the
# codespace will NOT auto-stop — the user must stop it himself (hold
# Online 4 s -> Shut down) or quota burns. start.sh is the only thing
# that arms this script; tell the user plainly whenever it is disarmed.
set -u
WATCH_DIR="$HOME/moviebox-watch"
ACT="$WATCH_DIR/last-activity.txt"
STATE="$WATCH_DIR/batch-state.json"
PLAYBACK="$WATCH_DIR/playback-session.json"
IDLE_SECS=900   # 15 minutes
POLL_SECS=60

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') watchdog: $*" >>"$WATCH_DIR/watchdog.log"; }

batch_running() {
    python3 -c "
import json, sys
try:
    st = json.load(open('$STATE'))
    sys.exit(0 if st.get('running') else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null
}

playback_live() {
    python3 -c "
import json, sys, time
try:
    pb = json.load(open('$PLAYBACK'))
    sys.exit(0 if time.time() < float(pb.get('ends_at', 0)) else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null
}

last_activity() {
    if [ -f "$ACT" ]; then
        cat "$ACT" 2>/dev/null || stat -c %Y "$ACT" 2>/dev/null || echo 0
    else
        # No activity file yet: give a full grace window from now.
        date +%s
    fi
}

log "watchdog armed (idle window ${IDLE_SECS}s)"
while true; do
    sleep "$POLL_SECS"
    if batch_running; then
        continue
    fi
    if playback_live; then
        continue
    fi
    now="$(date +%s)"
    last="$(last_activity)"
    # last may be a float epoch ("1699...123"); strip the fraction.
    last="${last%%.*}"
    idle=$((now - last))
    if [ "$idle" -ge "$IDLE_SECS" ]; then
        log "no activity for ${idle}s — stopping codespace"
        if command -v gh >/dev/null 2>&1; then
            gh codespace stop >>"$WATCH_DIR/watchdog.log" 2>&1 || \
                log "gh codespace stop failed"
        else
            log "gh not found — cannot stop the codespace"
        fi
        # After a stop attempt, wait a while before checking again so we
        # do not spam stop commands.
        sleep 600
    fi
done
