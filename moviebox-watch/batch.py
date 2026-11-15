#!/usr/bin/env python3
"""batch.py — MovieBox-TUI batch driver (REWRITE, 16 Nov 2026).

Rewritten from REBUILD-SPEC.md §5. Drives the UNMODIFIED MovieBox-TUI
v0.1.29 via tmux keys, verifying from DISK TRUTH before advancing.
Skips episodes already owned. State in batch-state.json, log in batch.log.

Flow (user-locked, §5.2):
    Hindi choose -> Episodes pane -> select next episode -> press download
    -> WAIT till that episode downloads -> next -> next.

Behaviour derived from the app's own source (v0.1.29, read at build time):
  * Home: typing any printable char enters search; Enter searches; Down
    selects the first result; Enter opens its Details screen. The Home
    screen shows the placeholder "Search movies, series & anime...".
  * Details panes, Tab order: Languages ("Audio") -> Seasons -> Episodes
    -> Streams -> cycle. Default pane is Streams. Tab only cycles panes
    that exist (Languages only when the title has dubs; Seasons/Episodes
    only for series).
  * Audio pane rows are the dub names ("Hindi", "English", ...). Enter on
    one (SelectLanguage) loads that dub's streams.
  * Episodes pane rows are "Episode NN" (+ title). Up/Down moves and
    triggers the episode's stream fetch; Enter also fetches. 'd' on the
    pane = Download Episode. Footer shows "[d] Download Episode" ONLY on
    this pane (full width; the driver widens to 200 cols during a batch).
  * Streams are sorted best-resolution-first and index 0 is auto-selected
    when they settle. 'd' downloads the selected stream.
  * 'd' -> the app fetches captions -> a "Subtitles" picker appears when
    more than one option exists (options are ["None"] + languages,
    selection starts at "None"); Enter confirms, Esc cancels the whole
    download queue. No popup = direct download, no subtitles.
  * Ctrl+P cycles providers globally (unless a modal is open), in order
    MovieBox, 4KHDHub, Dramachi, CircleFTP (BDIX), DhakaFlix (BDIX),
    and resets the app to the Home screen.
  * Esc on Details goes back Home. 'x' cancels an in-progress download
    (the driver does NOT use it: Stop stops the batch, not the TUI's own
    download — known gap, §17).
  * The selected list row is BOLD (capture-pane -e), which is how the
    driver reads the highlighted episode / language / subtitle.
  * Downloads land in ~/Downloads/MovieBox-TUI/Series/<Title>/Season <N>/
    <Title> - S<ss>E<ee>.<ext> (films: Movies/<Title>/<Title>.<ext>).

Provider rule (§5.5/§5.6): on a truncated episode, remove the cut file,
switch to the next provider, retry. Remember the working provider for the
rest of the batch. Cap tries at all providers once per episode, then mark
the episode FAILED with the reason shown live. Never silently accept a
cut file.

Window rule (§5.8): the Episodes pane does not render at narrow sizes, so
the driver widens to 200x50 DURING a batch and MUST hand the size back to
the phone afterwards — main() is wrapped in try/finally and the stop path
restores inside the stop handler itself (batchctl.py).
"""
import argparse
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

WATCH_DIR = Path(__file__).resolve().parent
HOME = Path.home()
APP_HOME = HOME / "Downloads" / "MovieBox-TUI"
MEDIA_DIR = WATCH_DIR / "media"
STATE_FILE = WATCH_DIR / "batch-state.json"
LOG_FILE = WATCH_DIR / "batch.log"
SYNC_SH = WATCH_DIR / "sync.sh"
SESSION = "moviebox"

PROVIDERS = ["MovieBox", "4KHDHub", "Dramachi",
             "CircleFTP (BDIX)", "DhakaFlix (BDIX)"]
FREE_GB_MIN = 2.0            # disk guard: stop if projection < 2 GB free
TRUNC_SHORT_S = 120          # >120 s short of reference...
TRUNC_FRAC = 0.80            # ...AND <80% of it -> truncated (§5.5)
WAIT_TIMEOUT_S = 1200        # per-episode download wait (20 min)
STALL_S = 300                # no file growth for 5 min -> stalled/missing
STABLE_S = 10                # size unchanged for 10 s -> download finished
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".m4v"}
EP_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
RES_RE = re.compile(r"\b(2160|1440|1080|720|480|360|240)p\b")
ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
EP_LINE_RE = re.compile(r"Episode\s+(\d+)", re.I)

# ---------------------------------------------------------------- logging
_log_fh = None
_log_stdout_is_file = None


def log(msg):
    """Timestamped line to stdout AND batch.log — unless stdout IS
    batch.log already (server.py spawns the driver with stdout redirected
    there), in which case writing to the file too would duplicate lines."""
    global _log_fh, _log_stdout_is_file
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        if _log_stdout_is_file is None:
            try:
                out_st = os.fstat(1)
                log_st = os.stat(LOG_FILE)
                _log_stdout_is_file = (out_st.st_dev == log_st.st_dev
                                       and out_st.st_ino == log_st.st_ino)
            except OSError:
                _log_stdout_is_file = False
        if not _log_stdout_is_file:
            if _log_fh is None:
                _log_fh = open(LOG_FILE, "a")
            _log_fh.write(line + "\n")
            _log_fh.flush()
    except OSError:
        pass


# ---------------------------------------------------------------- state
_state = {}


def write_state(**kw):
    _state.update(kw)
    tmp = Path(str(STATE_FILE) + ".tmp")
    try:
        tmp.write_text(json.dumps(_state, indent=2))
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log("state write failed: %s" % e)


def read_state_file():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def stop_requested():
    st = read_state_file()
    return st.get("running") is False


# ---------------------------------------------------------------- tmux
def tmux(*args, timeout=10):
    try:
        return subprocess.run(["tmux"] + list(args), capture_output=True,
                              text=True, timeout=timeout)
    except Exception as e:
        log("tmux error %s: %s" % (args, e))
        return None


def send_key(key):
    tmux("send-keys", "-t", SESSION, key)


def send_text(text):
    tmux("send-keys", "-t", SESSION, "-l", text)


def capture(ansi=False):
    args = ["capture-pane", "-t", SESSION, "-p", "-J"]
    if ansi:
        args.append("-e")
    r = tmux(*args, timeout=10)
    return r.stdout if r else ""


def plain_lines():
    return capture().splitlines()


def has_text(needle, text=None):
    return needle in (capture() if text is None else text)


# ---------------------------------------------------------------- window
def ensure_window_size(w=200, h=50):
    """Widen DURING the batch (§5.8) — the Episodes pane needs the width."""
    tmux("set-option", "-t", SESSION, "window-size", "manual")
    tmux("resize-window", "-t", SESSION, "-x", str(w), "-y", str(h))
    time.sleep(0.5)


def restore_phone_size():
    """Hand the window back to the phone (§5.8). Runs in finally + stop."""
    tmux("set-option", "-t", SESSION, "window-size", "smallest")
    tmux("set-option", "-t", SESSION, "aggressive-resize", "off")
    tmux("set-option", "-g", "window-size", "smallest")
    tmux("set-option", "-g", "aggressive-resize", "off")
    log("tmux window size restored to smallest client (phone)")


# ---------------------------------------------------------------- ANSI
def bold_segments(line):
    """Split a capture-pane -e line into (text, bold_or_reverse) pieces."""
    out = []
    cur_bold = False
    pos = 0
    for m in ANSI_RE.finditer(line):
        if m.start() > pos:
            out.append((line[pos:m.start()], cur_bold))
        codes = m.group(1)
        if codes in ("", "0"):
            cur_bold = False
        else:
            for c in codes.split(";"):
                if c in ("1", "7"):
                    cur_bold = True
                elif c == "22":
                    cur_bold = False
        pos = m.end()
    if pos < len(line):
        out.append((line[pos:], cur_bold))
    return out


def highlighted_match(pattern, text=None):
    """Episode/language/etc. number or text of the BOLD row matching a
    regex (group 1), or None when no styled row matches."""
    raw = capture(ansi=True) if text is None else text
    for line in raw.splitlines():
        plain = ANSI_RE.sub("", line)
        m = re.search(pattern, plain)
        if not m:
            continue
        # Is the matched text inside a bold/reverse segment?
        for seg_text, bold in bold_segments(line):
            if bold and re.search(pattern, seg_text):
                return m.group(1) if m.groups() else m.group(0)
    return None


def highlighted_episode():
    v = highlighted_match(r"Episode\s+(\d+)")
    return int(v) if v else None


def all_episode_numbers(text=None):
    raw = capture() if text is None else text
    return sorted({int(n) for n in EP_LINE_RE.findall(raw)})


# ---------------------------------------------------------------- screens
def on_home(text=None):
    return "Search movies, series & anime" in (capture() if text is None else text)


def on_details(text=None):
    t = capture() if text is None else text
    return (re.search(r"Episodes\s*\(\d+\)", t) is not None
            or "[d] Download Episode" in t
            or "[Enter] Play" in t)


def go_home():
    """Esc back to Home (closes any open modal first)."""
    for _ in range(4):
        t = capture()
        if on_home(t):
            return True
        send_key("Escape")
        time.sleep(0.8)
    return on_home()


def search_and_open(title):
    """Search the title on Home and open its Details screen."""
    if not go_home():
        log("search: could not reach Home screen")
        return False
    # Clear any leftover query ('c' clears the search in Normal mode).
    send_key("c")
    time.sleep(0.3)
    send_text(title)
    time.sleep(0.6)
    send_key("Enter")
    # Wait for results to settle.
    deadline = time.time() + 60
    settled = False
    while time.time() < deadline:
        if stop_requested():
            return False
        t = capture()
        if title.lower() in t.lower() and "Loading" not in t:
            settled = True
            break
        if "Search request failed" in t or "No results" in t:
            break
        time.sleep(2)
    if not settled:
        log("search: no results settled for %r" % title)
        return False
    # Try up to 3 results: Down to select, Enter to open.
    for attempt in range(3):
        send_key("Down")
        time.sleep(0.6)
        send_key("Enter")
        deadline = time.time() + 60
        while time.time() < deadline:
            if stop_requested():
                return False
            t = capture()
            if on_details(t):
                if title.lower() in t.lower():
                    return True
                break
            time.sleep(2)
        if not go_home():
            return False
        send_key("c")
        time.sleep(0.3)
        send_text(title)
        time.sleep(0.6)
        send_key("Enter")
        time.sleep(3)
    log("search: could not open details for %r" % title)
    return False


# ---------------------------------------------------------------- panes
def footer_has(text, needle):
    return needle in text


def goto_audio_pane():
    """Tab until the Audio (Languages) pane is focused. The Audio pane's
    footer has NO [d] button and its title reads 'Audio (N)'."""
    for _ in range(4):
        t = capture()
        if (re.search(r"Audio\s*\(\d+\)", t)
                and "[d] Download" not in t
                and "[Enter] Play" not in t):
            return True
        send_key("Tab")
        time.sleep(0.6)
    t = capture()
    return (re.search(r"Audio\s*\(\d+\)", t) is not None
            and "[d] Download" not in t)


def goto_episodes():
    """4x Tab cycle + Down probe (§5.4). The old bug returned True on the
    header 'E{n}' label alone — we also require the key bar to show
    '[d] Download Episode'."""
    for _ in range(4):
        t = capture()
        if "[d] Download Episode" in t:
            break
        send_key("Tab")
        time.sleep(0.6)
    t = capture()
    if "[d] Download Episode" not in t:
        return False
    # Down probe: the Episodes pane must respond to Down by moving the
    # highlighted episode (or at least show a bold episode row).
    before = highlighted_episode()
    send_key("Down")
    time.sleep(0.5)
    after = highlighted_episode()
    if after is not None and before is not None and after != before:
        send_key("Up")
        time.sleep(0.3)
        return True
    if after is not None or before is not None:
        return True
    return False


def goto_streams_pane():
    """Tab until the Streams pane is focused (footer '[Enter] Play')."""
    for _ in range(4):
        t = capture()
        if "[Enter] Play" in t:
            return True
        send_key("Tab")
        time.sleep(0.6)
    return "[Enter] Play" in capture()


def wait_streams(timeout=60):
    """Wait until the Streams table has rows (best-first, index 0 is
    auto-selected by the app when they settle)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_requested():
            return 0
        t = capture()
        rows = RES_RE.findall(t)
        if rows and "Loading streams" not in t:
            return len(rows)
        time.sleep(2)
    return 0


def choose_best_stream():
    """Streams are sorted best-first and index 0 is auto-selected; verify
    the highlighted stream really is the highest resolution, else move
    to the top row (Up clamps at index 0)."""
    if not goto_streams_pane():
        log("streams: could not focus Streams pane")
        return False
    t = capture()
    res = [int(r) for r in RES_RE.findall(t)]
    if not res:
        return False
    best = max(res)
    for _ in range(5):
        send_key("Up")
        time.sleep(0.3)
    raw = capture(ansi=True)
    hl = None
    for line in raw.splitlines():
        plain = ANSI_RE.sub("", line)
        m = RES_RE.search(plain)
        if not m:
            continue
        for seg_text, bold in bold_segments(line):
            if bold and RES_RE.search(seg_text):
                hl = int(RES_RE.search(seg_text).group(1))
                break
        if hl is not None:
            break
    if hl is not None and hl != best:
        log("streams: highlighted %dp but best is %dp" % (hl, best))
        return False
    log("streams: best stream selected (%dp)" % best)
    return True


# ---------------------------------------------------------------- prelude
def prelude(lang):
    """§5.3: explicitly select the audio track + Enter, wait for streams.
    Returns the number of streams loaded, or 0 on failure."""
    if not goto_audio_pane():
        log("prelude: could not reach the Audio pane")
        return 0
    target = lang.strip().lower()
    chosen = None
    # The Audio pane lists dub names; the selected row is bold. Walk down
    # (selection starts at index 0) until the target is highlighted.
    for _ in range(12):
        raw = capture(ansi=True)
        for line in raw.splitlines():
            plain = ANSI_RE.sub("", line).strip()
            if plain.lower() == target:
                for seg_text, bold in bold_segments(line):
                    if bold and seg_text.strip().lower() == target:
                        chosen = target
                        break
            if chosen:
                break
        if chosen:
            break
        send_key("Down")
        time.sleep(0.4)
    if not chosen:
        log("prelude: audio track %r not offered" % lang)
        return 0
    send_key("Enter")
    n = wait_streams(timeout=60)
    log("prelude: audio set to %s - %d streams loaded" % (lang, n))
    return n


# ---------------------------------------------------------------- subtitles
def handle_subtitle_popup(timeout=30):
    """After 'd', a 'Subtitles' picker appears when >1 option exists
    (options: 'None' first, then languages; selection starts at 'None').
    Pick English when offered, else 'None' (first option) — the popup
    must never be left open to block a download (§5.3)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_requested():
            return False
        t = capture()
        if "Subtitles" in t:
            break
        time.sleep(1)
    else:
        return True  # no popup: direct download, no subtitles
    # Popup open: look for an English option.
    raw = capture()
    has_english = bool(re.search(r"\benglish\b", raw, re.I))
    if not has_english:
        send_key("Enter")  # selects "None" (index 0)
        time.sleep(0.5)
        return True
    # Walk down until English is the bold (highlighted) row, then Enter.
    for _ in range(12):
        raw = capture(ansi=True)
        found = False
        for line in raw.splitlines():
            plain = ANSI_RE.sub("", line)
            if not re.search(r"\benglish\b", plain, re.I):
                continue
            for seg_text, bold in bold_segments(line):
                if bold and re.search(r"\benglish\b", seg_text, re.I):
                    found = True
                    break
            if found:
                break
        if found:
            break
        send_key("Down")
        time.sleep(0.4)
    send_key("Enter")
    time.sleep(0.5)
    return True


# ---------------------------------------------------------------- disk truth
def series_dir(title, season):
    return APP_HOME / "Series" / title / ("Season %d" % season)


def media_files_for(title, season, ep):
    """All on-disk video files for one episode (Downloads + media/)."""
    out = []
    pat = re.compile(r"[Ss]%02d[Ee]%02d" % (season, ep))
    for base in (series_dir(title, season), MEDIA_DIR):
        if not base.exists():
            continue
        for p in base.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            if p.name.endswith(".temp") or ".part" in p.name:
                continue
            if re.search(r"\.f\d+$", p.name):
                continue  # bare .f0 partial from a stall: NOT owned (§5.6)
            if pat.search(p.name):
                out.append(p)
    return out


def is_episode_file(path):
    """True for a finished episode file; False for stall partials."""
    name = Path(path).name
    if name.endswith(".temp") or ".part" in name:
        return False
    if re.search(r"\.f\d+$", name):
        return False
    if EP_RE.search(name) and Path(path).suffix.lower() in VIDEO_EXTS:
        return True
    return False


def owned_episodes(title, season):
    owned = set()
    for base in (series_dir(title, season), MEDIA_DIR):
        if not base.exists():
            continue
        for p in base.iterdir():
            if not p.is_file() or not is_episode_file(p):
                continue
            m = EP_RE.search(p.name)
            if m and int(m.group(1)) == season:
                owned.add(int(m.group(2)))
    return owned


def ffprobe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out) if out else None
    except Exception:
        return None


def subtitle_end(title, season, ep):
    """Reference = the episode's own subtitle end, when present (§5.5)."""
    stem = re.compile(r"[Ss]%02d[Ee]%02d" % (season, ep))
    for base in (series_dir(title, season), MEDIA_DIR):
        if not base.exists():
            continue
        for p in base.iterdir():
            if p.suffix.lower() != ".srt" or not stem.search(p.name):
                continue
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            ends = []
            for m in re.finditer(
                    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*"
                    r"(\d+):(\d+):(\d+)[,.](\d+)", text):
                h, mi, s, ms = (int(m.group(5)), int(m.group(6)),
                                int(m.group(7)), int(m.group(8)))
                ends.append(h * 3600 + mi * 60 + s + ms / 1000.0)
            if ends:
                return max(ends)
    return None


def reference_duration(title, season, ep):
    ref = subtitle_end(title, season, ep)
    if ref:
        return ref
    durs = []
    for other in sorted(owned_episodes(title, season)):
        if other == ep:
            continue
        for p in media_files_for(title, season, other):
            d = ffprobe_duration(p)
            if d:
                durs.append(d)
    if durs:
        return statistics.median(durs)
    return None


def free_gb():
    try:
        return shutil.disk_usage(APP_HOME).free / (1024 ** 3)
    except OSError:
        return shutil.disk_usage(WATCH_DIR).free / (1024 ** 3)


def delete_cut_files(title, season, ep):
    """Remove a truncated episode everywhere (fragment + packed + audio
    + subtitle) so the retry starts clean (§5.5)."""
    pat = re.compile(r"[Ss]%02d[Ee]%02d" % (season, ep))
    for base in (series_dir(title, season), MEDIA_DIR):
        if not base.exists():
            continue
        for p in list(base.iterdir()):
            if not p.is_file() or not pat.search(p.name):
                continue
            try:
                p.unlink()
                log("removed cut file %s" % p.name)
            except OSError:
                pass


def wait_for_media(title, season, ep):
    """-> 'ok' / 'truncated' / 'missing' / 'stopped' (§5.5)."""
    ref = reference_duration(title, season, ep)
    start = time.time()
    last_size = -1
    last_growth = time.time()
    stable_since = None
    while time.time() - start < WAIT_TIMEOUT_S:
        if stop_requested():
            return "stopped"
        files = media_files_for(title, season, ep)
        if not files:
            time.sleep(5)
            continue
        f = max(files, key=lambda p: p.stat().st_size)
        try:
            size = f.stat().st_size
        except OSError:
            time.sleep(5)
            continue
        if size != last_size:
            last_size = size
            last_growth = time.time()
            stable_since = None
            time.sleep(5)
            continue
        if stable_since is None:
            stable_since = time.time()
        if time.time() - stable_since >= STABLE_S:
            dur = ffprobe_duration(f)
            if dur is None:
                if time.time() - last_growth > STALL_S:
                    return "missing"
                time.sleep(5)
                continue
            if ref:
                if ref - dur > TRUNC_SHORT_S and dur < TRUNC_FRAC * ref:
                    log("episode %d truncated: %.1fs vs ref %.1fs"
                        % (ep, dur, ref))
                    return "truncated"
            log("episode %d ready: %.1fs" % (ep, dur))
            return "ok"
        if time.time() - last_growth > STALL_S and size == 0:
            return "missing"
        time.sleep(5)
    return "missing"


# ---------------------------------------------------------------- episodes
def select_episode(ep, timeout=120):
    """Go to the Episodes pane and move the highlight to episode `ep`.
    Returns True when the episode is highlighted (and its streams are
    fetched), False when the pane has no such episode."""
    if not goto_episodes():
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_requested():
            return False
        cur = highlighted_episode()
        if cur == ep:
            send_key("Enter")  # trigger_episode_fetch for this episode
            return wait_streams(timeout=60) > 0
        if cur is None:
            send_key("Down")
            time.sleep(0.4)
            continue
        send_key("Down" if cur < ep else "Up")
        time.sleep(0.35)
    return False


def pane_max_episode():
    """Walk Down to the last row to learn how many episodes the pane has.
    Returns the highest episode number seen (0 when unreadable)."""
    if not goto_episodes():
        return 0
    seen_max = highlighted_episode() or 0
    still = 0
    for _ in range(120):
        send_key("Down")
        time.sleep(0.3)
        cur = highlighted_episode()
        if cur is None:
            continue
        if cur > seen_max:
            seen_max = cur
            still = 0
        else:
            # Highlight stopped advancing: we are on the last row.
            still += 1
            if still >= 2:
                break
    return seen_max


# ---------------------------------------------------------------- providers
_provider_index = 0
_working_provider = 0


def current_provider_name():
    return PROVIDERS[_provider_index]


def switch_provider():
    """Ctrl+P cycles to the next provider and resets the app to Home
    (§5.6). The driver must re-open the series afterwards."""
    global _provider_index
    send_key("C-p")
    time.sleep(1.5)
    _provider_index = (_provider_index + 1) % len(PROVIDERS)
    name = current_provider_name()
    write_state(current_provider=name)
    log("provider switched to %s" % name)
    return name


def reopen_series(title, lang):
    """After a provider switch: search, open details, redo the prelude."""
    if not search_and_open(title):
        return False
    return prelude(lang) > 0


# ---------------------------------------------------------------- main
def main():
    global _provider_index, _working_provider
    ap = argparse.ArgumentParser(description="MovieBox-TUI batch driver")
    ap.add_argument("--title", required=True)
    ap.add_argument("--season", type=int, default=1)
    ap.add_argument("--count", default="5",
                    help="number of episodes, or 'all'")
    ap.add_argument("--lang", default="hindi",
                    help="audio track: hindi or english")
    args = ap.parse_args()

    title, season, lang = args.title, args.season, args.lang
    write_state(running=True, state="running", title=title, season=season,
                count=args.count, lang=lang, done=0, total=0,
                current_episode=None, current_provider=PROVIDERS[0],
                failures=[], message="Starting batch…",
                pid=os.getpid(), started_at=time.time())

    def on_term(signum, frame):
        log("batch interrupted by signal %d" % signum)
        restore_phone_size()
        st = read_state_file()
        write_state(running=False, state="stopped",
                    message="Stopped by user - %s of %s new episodes done"
                            % (st.get("done", 0), st.get("total", 0)))
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    done = 0
    failures = []
    total = 0
    last_pulse = 0.0
    try:
        ensure_window_size(200, 50)
        log("batch start: %r season %d count=%s lang=%s"
            % (title, season, args.count, lang))

        if not search_and_open(title):
            write_state(message="Could not open %r in the app." % title)
            return 2
        # Prelude: try each provider in turn until one offers the audio
        # track and streams (§5.3/§5.6).
        opened = False
        for _ in range(len(PROVIDERS)):
            if prelude(lang) > 0:
                opened = True
                break
            log("prelude failed on %s — trying next provider"
                % current_provider_name())
            switch_provider()
            if not search_and_open(title):
                break
        if not opened:
            write_state(message="Could not reach the Episodes pane "
                                "in the app.")
            return 2

        # Learn how many episodes the season has, then pick targets.
        pane_max = pane_max_episode()
        owned = owned_episodes(title, season)
        if args.count == "all":
            targets = [e for e in range(1, (pane_max or 100) + 1)
                       if e not in owned]
        else:
            try:
                want = int(args.count)
            except ValueError:
                want = 5
            targets = []
            ep = 1
            hi = pane_max or 100
            while len(targets) < want and ep <= hi:
                if ep not in owned:
                    targets.append(ep)
                ep += 1
        total = len(targets)
        write_state(total=total)
        log("batch targets: %s (owned already: %s)"
            % (targets, sorted(owned)))

        for ep in targets:
            if stop_requested():
                break
            # Disk guard (§5.9): stop if the projection leaves < 2 GB free.
            remaining = total - done
            avg_gb = 0.15
            owned_sizes = []
            for o in owned_episodes(title, season):
                for p in media_files_for(title, season, o):
                    try:
                        owned_sizes.append(p.stat().st_size)
                    except OSError:
                        pass
            if owned_sizes:
                avg_gb = statistics.median(owned_sizes) / (1024 ** 3)
            if free_gb() - remaining * avg_gb < FREE_GB_MIN:
                msg = ("Disk guard: stopping, projected free space would "
                       "drop below 2 GB")
                log(msg)
                write_state(message=msg)
                break
            # Pulse the shelf every ~5 min while running (§5).
            if time.time() - last_pulse > 300:
                last_pulse = time.time()
                try:
                    subprocess.run(["bash", str(SYNC_SH), "once"],
                                   capture_output=True, timeout=120)
                except Exception:
                    pass

            write_state(current_episode="Episode %d" % ep,
                        message="Downloading Episode %d…" % ep)
            tries = 0
            ok = False
            res = None
            while tries < len(PROVIDERS):
                if stop_requested():
                    break
                if not select_episode(ep):
                    failures.append("Episode %d: could not reach it in "
                                    "the app" % ep)
                    write_state(failures=failures)
                    break
                if not choose_best_stream():
                    log("episode %d: no usable stream on %s"
                        % (ep, current_provider_name()))
                send_key("d")
                time.sleep(1.0)
                handle_subtitle_popup()
                res = wait_for_media(title, season, ep)
                if res == "ok":
                    ok = True
                    _working_provider = _provider_index
                    break
                if res == "stopped":
                    break
                if res == "truncated":
                    delete_cut_files(title, season, ep)
                    reason = ("Episode %d cut short on %s"
                              % (ep, current_provider_name()))
                else:  # missing / stalled
                    reason = ("Episode %d did not arrive on %s"
                              % (ep, current_provider_name()))
                log("%s — switching provider" % reason)
                tries += 1
                if tries >= len(PROVIDERS):
                    failures.append(reason + " (all providers tried)")
                    write_state(failures=failures)
                    break
                if not switch_provider():
                    failures.append(reason + " (provider switch failed)")
                    write_state(failures=failures)
                    break
                if not reopen_series(title, lang):
                    failures.append(reason + " (could not reopen series)")
                    write_state(failures=failures)
                    break
                # Stay on the working provider for the next episode when
                # one was found; the retry loop above already advanced.
            if stop_requested():
                break
            if ok:
                done += 1
                owned = owned_episodes(title, season)
                write_state(done=done,
                            message="%d of %d done" % (done, total))
                log("episode %d done (%d of %d)" % (ep, done, total))
            elif res == "stopped":
                break
            else:
                # Last resort only (§5.9): the yt-dlp fallback. It was
                # never triggered live; keep it behind the provider loop.
                log("episode %d: trying yt-dlp fallback (download.sh)" % ep)
                try:
                    subprocess.run(
                        ["bash", str(WATCH_DIR / "download.sh"), title,
                         str(season), str(ep)],
                        capture_output=True, timeout=WAIT_TIMEOUT_S)
                except Exception as e:
                    log("yt-dlp fallback error: %s" % e)
                res = wait_for_media(title, season, ep)
                if res == "ok":
                    done += 1
                    owned = owned_episodes(title, season)
                    write_state(done=done,
                                message="%d of %d done" % (done, total))
                    log("episode %d done via yt-dlp fallback "
                        "(%d of %d)" % (ep, done, total))
                elif res == "stopped":
                    break
                else:
                    log("episode %d FAILED" % ep)
                    # Next episode starts from the last working provider.
                    _provider_index = _working_provider
        if stop_requested():
            msg = ("Stopped by user - %d of %d new episodes done"
                   % (done, total))
            log(msg)
            write_state(running=False, state="stopped", done=done,
                        total=total, message=msg, failures=failures)
            return 0
        msg = ("%d of %d new episodes done" % (done, total))
        if failures:
            msg += "; failed: " + "; ".join(failures)
        log("batch finished: %s" % msg)
        write_state(running=False, state="finished", done=done, total=total,
                    message=msg, failures=failures,
                    current_episode=None)
        return 0 if done == total else 1
    except Exception as e:
        log("batch error: %r" % (e,))
        write_state(running=False, state="error",
                    message="Batch error: %s" % e, failures=failures)
        return 1
    finally:
        restore_phone_size()
        st = read_state_file()
        if st.get("running"):
            write_state(running=False, state="stopped",
                        message=st.get("message") or "Batch stopped.")


if __name__ == "__main__":
    sys.exit(main())
