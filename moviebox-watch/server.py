#!/usr/bin/env python3
"""server.py — MovieBox Watch bridge server (REWRITE, 16 Nov 2026).

Rewritten from REBUILD-SPEC.md §4. Python 3 stdlib only.
Binds port 8000 and serves:
  * GET /                    -> the Watch page (player/terminal-with-js.html
                              when present, else the recovered
                              player/terminal.html)
  * GET /api/links           -> live tunnel URLs (Watch + App)
  * GET /api/downloads|list  -> Downloads shelf (films + series, depth-4
                              walk of ~/Downloads/MovieBox-TUI + media/,
                              deduped, series grouped)
  * GET /media/<file>        -> library files, Range support; a FRESH
                              request for a .f<digits>.mp4 302-redirects to
                              its .full.mp4; mid-file Range serves raw.
                              Self-heal: a no-audio file with a sibling
                              .m4a triggers the packer.
  * GET /live/<file>         -> error fallback only (same file serving)
  * POST /api/heartbeat      -> page-visible heartbeat (30 s, §4.4)
  * POST /api/playback       -> register a playback session (§4.5)
  * POST /api/key            -> forward a key to the tmux session (§12.2)
  * POST /api/delete         -> delete a file / a whole series
  * POST /api/shutdown       -> secret shutdown: gh codespace stop (§4.6)
  * POST /api/batch/start|stop, GET /api/batch/status|log (§4.7)

Activity (for watchdog.sh): POST /api/heartbeat, GET /media and
POST /api/playback update last-activity.txt. Requests carrying
User-Agent "moviebox-keepalive" are served but NOT counted as activity
(keepalive.sh probes the same endpoints without defeating the watchdog).

NOT the day-one skeleton (~/workspace/moviebox-pc/watch/server.py) — that
31-line file is unrelated.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WATCH_DIR = Path(__file__).resolve().parent
HOME = Path.home()
APP_HOME = HOME / "Downloads" / "MovieBox-TUI"
MEDIA_DIR = WATCH_DIR / "media"
PLAYER_DIR = WATCH_DIR / "player"
STATE_FILE = WATCH_DIR / "batch-state.json"
PLAYBACK_FILE = WATCH_DIR / "playback-session.json"
ACTIVITY_FILE = WATCH_DIR / "last-activity.txt"
POSTERS_FILE = WATCH_DIR / "posters.json"
BATCH_LOG = WATCH_DIR / "batch.log"
KEEPALIVE_UA = "moviebox-keepalive"

PORT = int(os.environ.get("MOVIEBOX_PORT", "8000"))
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".m4v"}
AUDIO_EXTS = {".m4a"}
EP_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
FRAG_RE = re.compile(r"\.f\d+\.mp4$", re.IGNORECASE)
FULL_SUFFIX = ".full.mp4"

# ---------------------------------------------------------------- helpers
def log(*a):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *a, flush=True)


def atomic_write(path, text):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def write_json(path, obj):
    atomic_write(path, json.dumps(obj, indent=2))


def record_activity():
    """Mark now as the last user activity (watchdog reads this file)."""
    try:
        atomic_write(ACTIVITY_FILE, "%.3f\n" % time.time())
    except Exception:
        pass


def is_keepalive(handler):
    return KEEPALIVE_UA in (handler.headers.get("User-Agent") or "")


def ffprobe(path, what):
    """what: 'duration' or 'has_audio'. Returns float/bool or None."""
    try:
        if what == "duration":
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=30).stdout.strip()
            return float(out) if out else None
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return bool(out)
    except Exception:
        return None


_probe_cache = {}
_probe_lock = threading.Lock()


def probe(path):
    """Cached (duration, has_audio) for a file, keyed by path+size+mtime."""
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    key = (str(path), st.st_size, st.st_mtime)
    with _probe_lock:
        if key in _probe_cache:
            return _probe_cache[key]
    dur = ffprobe(path, "duration")
    aud = ffprobe(path, "has_audio")
    with _probe_lock:
        _probe_cache[key] = (dur, aud)
        if len(_probe_cache) > 512:
            _probe_cache.clear()
    return dur, aud


def full_for(path):
    """The packed .full.mp4 for a fragment, if it exists (same dir)."""
    p = Path(path)
    if FRAG_RE.search(p.name):
        stem = re.sub(r"\.f\d+\.mp4$", "", p.name, flags=re.IGNORECASE)
        cand = p.with_name(stem + FULL_SUFFIX)
        return cand if cand.exists() else None
    return None


def sibling_audio(path):
    """Sibling .f3/.f1/.f0.m4a for a video file, if present."""
    p = Path(path)
    stem = re.sub(r"\.f\d+\.mp4$", "", p.name, flags=re.IGNORECASE)
    stem = re.sub(r"\.(mp4|mkv|webm|m4v)$", "", stem, flags=re.IGNORECASE)
    for cand in (stem + ".f3.m4a", stem + ".f1.m4a", stem + ".f0.m4a",
                 stem + ".m4a"):
        c = p.with_name(cand)
        if c.exists():
            return c
    return None


# ---------------------------------------------------------------- posters
# Poster rule (§12.5): year from folder/filename is authoritative; movies
# try Wikipedia "Title (year film)" BEFORE TVmaze. A no-poster tile shows
# the FULL NAME, not initials (the page handles that).
_posters = read_json(POSTERS_FILE, {}) or {}
_posters_lock = threading.Lock()
_poster_queue = []


def _save_posters():
    with _posters_lock:
        write_json(POSTERS_FILE, _posters)


def poster_lookup(title, year, kind):
    """Best-effort poster URL (None when unknown). Network failures -> None."""
    try:
        if kind == "film" and year:
            url = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
                   + urllib.parse.quote("%s (%s film)" % (title, year)))
            req = urllib.request.Request(url, headers={"User-Agent": "MovieBox-Watch/1.0"})
            with urllib.request.urlopen(req, timeout=4) as r:
                data = json.loads(r.read())
            thumb = (data.get("thumbnail") or {}).get("source")
            if thumb:
                return thumb
            orig = (data.get("originalimage") or {}).get("source")
            if orig:
                return orig
        # TVmaze (series, and film fallback)
        url = "https://api.tvmaze.com/search/shows?q=" + urllib.parse.quote(title)
        req = urllib.request.Request(url, headers={"User-Agent": "MovieBox-Watch/1.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read())
        if data:
            img = (data[0].get("show") or {}).get("image") or {}
            return img.get("original") or img.get("medium")
    except Exception:
        return None
    return None


def poster_worker():
    """Background thread: fill in missing posters for shelf titles."""
    while True:
        try:
            items, _ = build_shelf()
            for it in items:
                key = (it.get("title") or "").lower()
                if not key or key in _posters:
                    continue
                url = poster_lookup(it.get("title") or "", it.get("year"),
                                    it.get("kind") or "film")
                with _posters_lock:
                    _posters[key] = {"poster": url, "year": it.get("year")}
                _save_posters()
                time.sleep(1)
        except Exception as e:
            log("poster worker:", e)
        time.sleep(60)


# ---------------------------------------------------------------- shelf
def _walk_downloads():
    """Yield (path) of finished media files under APP_HOME (depth 4)."""
    if not APP_HOME.exists():
        return
    for root, dirs, files in os.walk(APP_HOME):
        rel = Path(root).relative_to(APP_HOME)
        if len(rel.parts) > 3:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.endswith(".temp") or ".part" in name:
                continue
            p = Path(root) / name
            if p.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS:
                yield p


def _series_key(title, season):
    return ("series", (title or "").strip().lower(), int(season or 1))


def build_shelf():
    """Returns (items, disk). Items match what player JS reads (§12.3)."""
    films = {}      # title-lower -> item
    series = {}     # (title-lower, season) -> item
    seen_ep = {}    # (series key, episode) -> episode entry dict

    def film_item(title, path):
        key = (title or "").strip().lower()
        it = films.get(key)
        if it is None:
            year = None
            m = YEAR_RE.search(title or "")
            if m:
                year = int(m.group(0))
            it = {"kind": "film", "title": title, "name": title,
                  "file": path.name, "size_bytes": 0, "duration": None,
                  "year": year, "poster": None, "full_file": None,
                  "no_audio": False}
            films[key] = it
        return it

    def series_item(title, season):
        key = _series_key(title, season)
        it = series.get(key)
        if it is None:
            year = None
            m = YEAR_RE.search(title or "")
            if m:
                year = int(m.group(0))
            it = {"kind": "series", "title": title, "name": title,
                  "season": int(season or 1), "episodes": [],
                  "year": year, "poster": None, "total_episodes": 0}
            series[key] = it
        return it

    def absorb_video(path, in_media):
        """Absorb one video file into the shelf. Returns nothing."""
        name = path.name
        m = EP_RE.search(name)
        # Series: folder Series/<Title>/Season <N>/ or SxxEyy in the name.
        parts = path.parts
        series_title = None
        season = 1
        if "Series" in parts:
            i = parts.index("Series")
            if i + 1 < len(parts):
                series_title = parts[i + 1]
            if i + 2 < len(parts):
                sm = re.search(r"Season\s*(\d+)", parts[i + 2], re.I)
                if sm:
                    season = int(sm.group(1))
        if series_title is None and m:
            # Bare file in media/: title is the stem before " - SxxEyy".
            stem = re.sub(r"\s*-\s*[Ss]\d+[Ee]\d+.*$", "", name)
            series_title = stem or name
            season = int(m.group(1))
        if series_title and m:
            ep = int(m.group(2))
            key = (_series_key(series_title, season), ep)
            if key in seen_ep:
                # Same episode seen twice (Downloads + media/): keep the
                # first entry, but take the packed file if this copy has one.
                full = full_for(path)
                if full is not None:
                    seen_ep[key]["full_file"] = full.name
                return
            it = series_item(series_title, season)
            dur, aud = probe(path)
            full = full_for(path)
            entry = {"file": name, "name": name,
                     "size_bytes": path.stat().st_size if path.exists() else 0,
                     "size": path.stat().st_size if path.exists() else 0,
                     "duration": dur, "episode": ep, "ep": ep,
                     "season": int(season),
                     "full_file": full.name if full else None,
                     "no_audio": (aud is False)}
            it["episodes"].append(entry)
            seen_ep[key] = entry
            return
        # Film (no SxxEyy): title from the Movies/<Title>/ folder or stem.
        title = None
        if "Movies" in parts:
            i = parts.index("Movies")
            if i + 1 < len(parts):
                title = parts[i + 1]
        if title is None:
            title = re.sub(r"\.f\d+\.mp4$", "", name, flags=re.I)
            title = re.sub(r"\.(mp4|mkv|webm|m4v)$", "", title, flags=re.I)
        it = film_item(title, path)
        dur, aud = probe(path)
        it["size_bytes"] = max(it["size_bytes"],
                               path.stat().st_size if path.exists() else 0)
        it["size"] = it["size_bytes"]
        if dur:
            it["duration"] = dur
        if aud is False:
            it["no_audio"] = True
        full = full_for(path)
        if full:
            it["full_file"] = full.name
            it["file"] = full.name
        elif not it.get("file") or FRAG_RE.search(it["file"] or ""):
            it["file"] = name

    for p in _walk_downloads():
        if p.suffix.lower() in VIDEO_EXTS:
            absorb_video(p, in_media=False)
    if MEDIA_DIR.exists():
        for p in sorted(MEDIA_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                absorb_video(p, in_media=True)

    items = []
    for it in films.values():
        with _posters_lock:
            hit = _posters.get((it["title"] or "").lower())
        if hit:
            it["poster"] = hit.get("poster")
            if hit.get("year"):
                it["year"] = hit["year"]
        items.append(it)
    for it in series.values():
        it["episodes"].sort(key=lambda e: e.get("episode") or 0)
        it["total_episodes"] = len(it["episodes"])
        with _posters_lock:
            hit = _posters.get((it["title"] or "").lower())
        if hit:
            it["poster"] = hit.get("poster")
            if hit.get("year"):
                it["year"] = hit["year"]
        items.append(it)
    items.sort(key=lambda i: (i.get("title") or "").lower())

    # Disk meter: films bytes (library, deduped by basename) + free space.
    total = 0
    seen_names = set()
    for base in (APP_HOME, MEDIA_DIR):
        if not base.exists():
            continue
        for root, _d, files in os.walk(base):
            for name in files:
                p = Path(root) / name
                if p.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS:
                    if p.name in seen_names:
                        continue
                    seen_names.add(p.name)
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
    try:
        du = shutil.disk_usage(APP_HOME if APP_HOME.exists() else WATCH_DIR)
        free_b, total_b = du.free, du.total
    except OSError:
        free_b, total_b = 0, 0
    disk = {"films_bytes": total, "free_bytes": free_b,
            "total_bytes": total_b, "used_bytes": max(0, total_b - free_b)}
    return items, disk


# ---------------------------------------------------------------- batch
def batch_state():
    st = read_json(STATE_FILE, {}) or {}
    # If the state claims running but the pid is dead, it really stopped.
    pid = st.get("pid")
    if st.get("running") and pid:
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, PermissionError, ValueError, OverflowError):
            st["running"] = False
            st["state"] = "stopped"
            write_json(STATE_FILE, st)
    return st


def batch_running():
    return bool(batch_state().get("running"))


def start_batch(title, season, count, lang="hindi"):
    if batch_running():
        return None, "a batch is already running"
    cmd = [sys.executable, str(WATCH_DIR / "batch.py"),
           "--title", str(title), "--season", str(season),
           "--count", str(count), "--lang", str(lang or "hindi")]
    logf = open(BATCH_LOG, "ab")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                            cwd=str(WATCH_DIR),
                            start_new_session=True)
    st = {"running": True, "state": "running", "title": title,
          "season": int(season or 1), "count": count, "lang": lang or "hindi",
          "done": 0, "total": 0, "current_episode": None,
          "current_provider": "MovieBox", "failures": [],
          "message": "Starting batch…", "pid": proc.pid,
          "started_at": time.time()}
    write_json(STATE_FILE, st)
    return proc.pid, None


def run_tmux(*args):
    try:
        subprocess.run(["tmux"] + list(args), capture_output=True, timeout=10)
    except Exception:
        pass


def restore_phone_size():
    """§5.8 stop-path restore: hand the window back to the phone, sync."""
    run_tmux("set-option", "-t", "moviebox", "window-size", "smallest")
    run_tmux("set-option", "-t", "moviebox", "aggressive-resize", "off")
    run_tmux("set-option", "-g", "window-size", "smallest")
    run_tmux("set-option", "-g", "aggressive-resize", "off")


# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- plumbing ------------------------------------------------------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, code=200, ctype="text/plain"):
        body = text.encode() if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # -- routing -------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._serve_page()
        elif path == "/api/links":
            self._send_json(self._links())
        elif path in ("/api/downloads", "/api/list"):
            items, disk = build_shelf()
            self._send_json({"items": items, "downloads": items, "disk": disk})
        elif path == "/api/batch/status":
            self._send_json(batch_state())
        elif path == "/api/batch/log":
            try:
                lines = BATCH_LOG.read_text(errors="replace").splitlines()
            except OSError:
                lines = []
            self._send_text("\n".join(lines[-200:]) + ("\n" if lines else ""))
        elif path.startswith("/media/"):
            self._serve_media(path[len("/media/"):])
        elif path.startswith("/live/"):
            # /live is the error fallback only (§4.3) — same file serving.
            self._serve_media(path[len("/live/"):])
        else:
            self._send_text("not found", 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/heartbeat":
            if not is_keepalive(self):
                record_activity()
            self._send_json({"ok": True})
        elif path == "/api/playback":
            data = self._read_json()
            try:
                dur = int(data.get("duration") or 0)
            except (TypeError, ValueError):
                dur = 0
            now = time.time()
            write_json(PLAYBACK_FILE, {
                "file": data.get("file") or "",
                "duration": dur, "started_at": now,
                "ends_at": now + dur})
            record_activity()
            self._send_json({"ok": True})
        elif path == "/api/key":
            data = self._read_json()
            self._handle_key(data.get("key") or "")
        elif path == "/api/delete":
            data = self._read_json()
            self._handle_delete(data)
        elif path == "/api/shutdown":
            self._send_json({"ok": True, "shutting_down": True})
            threading.Thread(target=self._do_shutdown, daemon=True).start()
        elif path == "/api/batch/start":
            data = self._read_json()
            pid, err = start_batch(data.get("title") or "",
                                   data.get("season") or 1,
                                   data.get("count") or "5",
                                   data.get("lang") or "hindi")
            if err:
                self._send_json({"ok": False, "error": err}, 409)
            else:
                self._send_json({"ok": True, "pid": pid})
        elif path == "/api/batch/stop":
            # Stop via batchctl, then restore phone size synchronously
            # BEFORE responding (§4.7 — proven 14 Nov 2026).
            try:
                subprocess.run([sys.executable, str(WATCH_DIR / "batchctl.py"),
                                "stop"], capture_output=True, timeout=15)
            except Exception as e:
                log("batch stop error:", e)
            restore_phone_size()
            self._send_json({"ok": True, "stopped": True})
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)

    # -- pages / links -------------------------------------------------
    def _serve_page(self):
        page = PLAYER_DIR / "terminal-with-js.html"
        if not page.exists():
            page = PLAYER_DIR / "terminal.html"
        try:
            body = page.read_bytes()
        except OSError:
            self._send_text("watch page not found", 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _links(self):
        def _read(name):
            try:
                return (WATCH_DIR / name).read_text().strip()
            except OSError:
                return ""
        watch = _read("tunnel-url.txt")
        app = _read("app-url.txt")
        return {"watch": watch, "watch_url": watch, "app": app,
                "app_url": app, "terminal": app, "terminal_url": app}

    # -- media ---------------------------------------------------------
    def _serve_media(self, raw_name):
        name = urllib.parse.unquote(raw_name)
        # Basenames only — no directory traversal.
        if not name or "/" in name or name in (".", ".."):
            self._send_text("not found", 404)
            return
        path = MEDIA_DIR / name
        if not path.exists() or not path.is_file():
            self._send_text("not found", 404)
            return
        # 302 rule (§4.3): a FRESH request for a .f<digits>.mp4 redirects
        # to its .full.mp4; a mid-file Range request serves the raw file.
        if FRAG_RE.search(name) and "Range" not in (self.headers or {}):
            full = full_for(path)
            if full is not None:
                self.send_response(302)
                self.send_header("Location", "/media/" + full.name)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if not is_keepalive(self):
            record_activity()
            # Self-heal (§4.3): no-audio file + sibling .m4a -> packer.
            _maybe_self_heal(path)
        self._send_file(path)

    def _send_file(self, path):
        try:
            size = path.stat().st_size
        except OSError:
            self._send_text("not found", 404)
            return
        ctype = "video/mp4"
        if path.suffix.lower() == ".mkv":
            ctype = "video/x-matroska"
        elif path.suffix.lower() == ".webm":
            ctype = "video/webm"
        elif path.suffix.lower() == ".m4a":
            ctype = "audio/mp4"
        range_hdr = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if range_hdr:
            m = re.match(r"bytes=(\d*)-(\d*)", range_hdr.strip() or "")
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                end = min(end, size - 1)
                if start <= end:
                    status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    # -- key / delete / shutdown ---------------------------------------
    def _handle_key(self, key):
        # Driver-control lock (§12.2): refuse keys while a batch runs.
        if batch_running():
            self._send_json({"ok": False, "error": "driver in control"}, 409)
            return
        mapping = {"Escape": "Escape", "esc": "Escape", "Up": "Up",
                   "Down": "Down", "Tab": "Tab", "Enter": "Enter",
                   "BSpace": "BSpace", "Space": "Space"}
        tmux_key = mapping.get(key)
        if tmux_key is None:
            # Single char / Ctrl combo (e.g. "C-p") passed through as-is.
            if isinstance(key, str) and 0 < len(key) <= 4:
                tmux_key = key
            else:
                self._send_json({"ok": False, "error": "bad key"}, 400)
                return
        try:
            r = subprocess.run(["tmux", "send-keys", "-t", "moviebox", tmux_key],
                               capture_output=True, timeout=10)
            if r.returncode != 0:
                # e.g. no such tmux session — surface it, don't pretend ok.
                self._send_json({"ok": False,
                                 "error": (r.stderr or b"tmux failed").decode(
                                     errors="replace").strip() or "tmux failed"},
                                500)
                return
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)
            return
        self._send_json({"ok": True})

    def _handle_delete(self, data):
        deleted = []
        if data.get("series"):
            title = (data.get("title") or "").strip()
            season = int(data.get("season") or 1)
            # Delete every episode of the series from media/ + Downloads.
            for base in (MEDIA_DIR, APP_HOME):
                if not base.exists():
                    continue
                for root, _d, files in os.walk(base):
                    for name in files:
                        p = Path(root) / name
                        m = EP_RE.search(name)
                        if not m or int(m.group(1)) != season:
                            continue
                        stem = re.sub(r"\s*-\s*[Ss]\d+[Ee]\d+.*$", "",
                                      name).strip().lower()
                        if stem and stem != title.lower():
                            continue
                        try:
                            p.unlink()
                            deleted.append(name)
                        except OSError:
                            pass
            self._send_json({"ok": True, "deleted": deleted})
            return
        name = data.get("file") or ""
        name = urllib.parse.unquote(str(name)).split("/")[-1]
        if not name:
            self._send_json({"ok": False, "error": "no file"}, 400)
            return
        stem = re.sub(r"\.f\d+\.mp4$", "", name, flags=re.I)
        stem = re.sub(r"\.(mp4|mkv|webm|m4v)$", "", stem, flags=re.I)
        for base in (MEDIA_DIR, APP_HOME):
            if not base.exists():
                continue
            for root, _d, files in os.walk(base):
                for fn in files:
                    p = Path(root) / fn
                    if fn == name or fn.startswith(stem + "."):
                        try:
                            p.unlink()
                            deleted.append(fn)
                        except OSError:
                            pass
        self._send_json({"ok": True, "deleted": deleted})

    def _do_shutdown(self):
        time.sleep(0.5)
        log("shutdown requested — gh codespace stop")
        try:
            subprocess.run(["gh", "codespace", "stop"],
                           capture_output=True, timeout=120)
        except Exception as e:
            log("shutdown failed:", e)


_heal_guard = {}
_heal_lock = threading.Lock()


def _maybe_self_heal(path):
    """§4.3: no-audio file + sibling .m4a -> trigger the packer (at most
    once per file per 60 s)."""
    now = time.time()
    with _heal_lock:
        if now - _heal_guard.get(str(path), 0) < 60:
            return
        _heal_guard[str(path)] = now
    _dur, aud = probe(path)
    if aud is not False:
        return
    if sibling_audio(path) is None:
        return
    try:
        subprocess.Popen(["bash", str(WATCH_DIR / "audio-repair.sh"), "sweep"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        log("self-heal: packer triggered for", path.name)
    except Exception as e:
        log("self-heal failed:", e)


def main():
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if not ACTIVITY_FILE.exists():
        record_activity()
    threading.Thread(target=poster_worker, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log("server listening on 0.0.0.0:%d (watch dir %s)" % (PORT, WATCH_DIR))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
