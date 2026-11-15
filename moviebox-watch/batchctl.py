#!/usr/bin/env python3
"""batchctl.py — batch stop control (REWRITE, 16 Nov 2026).

Rewritten from REBUILD-SPEC.md §6.

  batchctl.py stop     kill the running batch (and yt-dlp) and hand the
                       tmux window back to the phone, within ~1 s.

Rules (§6, proven 14 Nov 2026 — 4 stop cycles, ~1 s each):
  * killnow() kills the batch process AND yt-dlp, and calls
    restore_phone_size() synchronously just before its return.
  * The main stop branch calls restore_phone_size() AGAIN after killnow(),
    and it must run even if killnow() raises.
  * batch.py's finally restore stays as a fallback.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

WATCH_DIR = Path(__file__).resolve().parent
STATE_FILE = WATCH_DIR / "batch-state.json"
SESSION = "moviebox"


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] batchctl: ") + msg, flush=True)


def restore_phone_size():
    """Hand the tmux window back to the phone, synchronously (§5.8)."""
    for args in (
            ("set-option", "-t", SESSION, "window-size", "smallest"),
            ("set-option", "-t", SESSION, "aggressive-resize", "off"),
            ("set-option", "-g", "window-size", "smallest"),
            ("set-option", "-g", "aggressive-resize", "off")):
        try:
            subprocess.run(["tmux"] + list(args), capture_output=True,
                           timeout=10)
        except Exception:
            pass
    log("tmux window size restored to smallest client (phone)")


def _pids_matching(pattern):
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            pass
    return [p for p in pids if p != os.getpid()]


def _kill_pids(pids, sig=signal.SIGTERM, wait=1.0):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    deadline = time.time() + wait
    for pid in pids:
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)


def killnow():
    """Kill the batch process and yt-dlp, then restore the phone size
    synchronously just before returning."""
    # The batch runs as `python3 .../batch.py ...`; batchctl.py's own
    # cmdline is `batchctl.py`, which does NOT match the pattern.
    pids = _pids_matching("batch.py")
    if pids:
        _kill_pids(pids, signal.SIGTERM, wait=1.0)
        still = [p for p in pids if p in _pids_matching("batch.py")]
        if still:
            _kill_pids(still, signal.SIGKILL, wait=0.5)
        log("killed batch process(es): %s" % pids)
    # yt-dlp fallback (download.sh) — only the batch uses it here.
    dl = _pids_matching("yt-dlp")
    dl += [p for p in _pids_matching("download.sh") if p not in dl]
    if dl:
        _kill_pids(dl, signal.SIGTERM, wait=1.0)
        log("killed yt-dlp process(es): %s" % dl)
    restore_phone_size()


def stop():
    """Main stop branch: killnow() + restore AGAIN afterwards, even if
    killnow() raises."""
    try:
        killnow()
    finally:
        restore_phone_size()
    st = {}
    try:
        st = json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    done = st.get("done", 0)
    total = st.get("total", 0)
    msg = ("Stopped by user - %d of %d new episodes done" % (done, total))
    st.update({"running": False, "state": "stopped", "message": msg})
    try:
        tmp = Path(str(STATE_FILE) + ".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log("state write failed: %s" % e)
    log(msg)
    return 0


def main():
    if len(sys.argv) < 2:
        print("usage: batchctl.py stop|restore", file=sys.stderr)
        return 2
    if sys.argv[1] == "stop":
        return stop()
    if sys.argv[1] == "restore":
        restore_phone_size()
        return 0
    print("unknown subcommand: %s" % sys.argv[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
