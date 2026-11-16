#!/usr/bin/env bash
# start.sh — MovieBox bridge entry point (REWRITE, 16 Nov 2026).
# Rewritten from REBUILD-SPEC.md §1. Idempotent; safe to re-run.
# On run it:
#   1. Starts the app (run-app.sh: tmux session "moviebox").
#   2. Starts ttyd (port 7681) attached to the tmux session, so the
#      terminal is served for the page iframe and the App tunnel.
#      (Fix 16 Nov 2026: the first rewrite omitted this step and ttyd
#      had to be started by hand on CodeSandbox.)
#   3. Starts the Python server (server.py, port 8000) if not running.
#   4. Starts the sync loop (sync.sh, ~60 s sweep) if not running.
#   5. Re-arms the watchdog (watchdog.sh) — start.sh is what brings it
#      back after it was killed on 14 Nov 2026 (§7).
#   6. Starts keepalive.sh if not running.
#   7. Starts the two cloudflared Quick Tunnels (8000 Watch, 7681 App)
#      and writes the live URLs to tunnel-url.txt / app-url.txt.
#      Old URLs are dead after a restart — the files are overwritten.
#   8. Prints the fresh Watch + App URLs at the end.
#
# NOTE (CodeSandbox): the tunnels are only needed where the host has no
# public IP / the phone cannot resolve the host's preview domain. On a
# host with a direct public IP (e.g. Oracle), skip step 7 and use the
# public IP instead. The server itself is tunnel-agnostic.
set -u
WATCH_DIR="$HOME/moviebox-watch"
cd "$WATCH_DIR" || { echo "cannot cd to $WATCH_DIR"; exit 1; }
mkdir -p "$WATCH_DIR/media"

# 1. The app (tmux session "moviebox").
bash "$WATCH_DIR/run-app.sh"

# 2. ttyd (port 7681) — serves the tmux session for the page iframe
#    and the App tunnel. Must run after run-app.sh (the session must
#    exist first). Idempotent: skipped if already running.
if ! pgrep -f "ttyd -p 7681" >/dev/null 2>&1; then
    nohup ttyd -p 7681 -W -t fontSize=12 tmux attach-session -t moviebox >>"$WATCH_DIR/ttyd.log" 2>&1 &
    echo "ttyd started (pid $!)"
else
    echo "ttyd already running"
fi

# 3. The Python server (port 8000).
if ! pgrep -f "server.py" >/dev/null 2>&1; then
    nohup python3 "$WATCH_DIR/server.py" >>"$WATCH_DIR/server.log" 2>&1 &
    echo "server.py started (pid $!)"
else
    echo "server.py already running"
fi
# Seed activity so the watchdog gives a full 15-minute grace window.
date +%s.%N > "$WATCH_DIR/last-activity.txt" 2>/dev/null || true

# 4. The sync loop (~60 s sweep).
if ! pgrep -f "sync.sh" >/dev/null 2>&1; then
    nohup bash "$WATCH_DIR/sync.sh" >>"$WATCH_DIR/sync.log" 2>&1 &
    echo "sync.sh started (pid $!)"
else
    echo "sync.sh already running"
fi

# 5. Re-arm the watchdog (kills any stale copy first).
pkill -f "watchdog.sh" 2>/dev/null || true
sleep 1
nohup bash "$WATCH_DIR/watchdog.sh" >>"$WATCH_DIR/watchdog.log" 2>&1 &
echo "watchdog.sh re-armed (pid $!)"

# 6. keepalive.sh (companion prober; NOT a guard — see its header).
if ! pgrep -f "keepalive.sh" >/dev/null 2>&1; then
    nohup bash "$WATCH_DIR/keepalive.sh" >>"$WATCH_DIR/keepalive.log" 2>&1 &
    echo "keepalive.sh started (pid $!)"
else
    echo "keepalive.sh already running"
fi

# 7. Cloudflare Quick Tunnels (skip with SKIP_TUNNELS=1 on a host that
#    has a direct public IP).
if [ "${SKIP_TUNNELS:-0}" = "1" ]; then
    echo "SKIP_TUNNELS=1: no tunnels started (use the host's public IP)"
else
    CF="$WATCH_DIR/cloudflared"
    [ -x "$CF" ] || CF="$(command -v cloudflared || true)"
    if [ -z "$CF" ]; then
        echo "WARNING: cloudflared not found — tunnels not started"
    else
        pkill -f "cloudflared tunnel" 2>/dev/null || true
        sleep 1
        nohup "$CF" tunnel --url http://localhost:8000 \
            >>"$WATCH_DIR/tunnel-8000.log" 2>&1 &
        nohup "$CF" tunnel --url http://localhost:7681 \
            >>"$WATCH_DIR/tunnel-7681.log" 2>&1 &
        echo "tunnels starting…"
        # Wait up to 60 s for both trycloudflare.com URLs to appear.
        for i in $(seq 1 60); do
            WATCH_URL="$(grep -ao 'https://[a-z0-9-]*\.trycloudflare\.com' \
                "$WATCH_DIR/tunnel-8000.log" 2>/dev/null | head -1 || true)"
            APP_URL="$(grep -ao 'https://[a-z0-9-]*\.trycloudflare\.com' \
                "$WATCH_DIR/tunnel-7681.log" 2>/dev/null | head -1 || true)"
            if [ -n "$WATCH_URL" ] && [ -n "$APP_URL" ]; then
                break
            fi
            sleep 1
        done
        [ -n "${WATCH_URL:-}" ] && echo "$WATCH_URL" > "$WATCH_DIR/tunnel-url.txt"
        [ -n "${APP_URL:-}" ] && echo "$APP_URL" > "$WATCH_DIR/app-url.txt"
        # Make port 8000 Public on a Codespace (best effort).
        if command -v gh >/dev/null 2>&1 && [ -n "${CODESPACE_NAME:-}" ]; then
            gh codespace ports visibility 8000:public \
                -c "$CODESPACE_NAME" >/dev/null 2>&1 || true
        fi
    fi
fi

# 8. Fresh URLs.
echo "---"
echo "Watch: $(cat "$WATCH_DIR/tunnel-url.txt" 2>/dev/null || echo '(no tunnel — use http://<host>:8000/)')"
echo "App:   $(cat "$WATCH_DIR/app-url.txt" 2>/dev/null || echo '(no tunnel — use http://<host>:7681/)')"
echo "Local: http://localhost:8000/"
