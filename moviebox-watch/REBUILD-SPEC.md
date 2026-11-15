# MovieBox bridge — REBUILD SPECIFICATION (prep only)

**Status: SPEC ONLY — no code has been written from this document.** The user
refused a rebuild from zero ("zero se ni banana"), so this file exists so the
rebuild can start instantly IF he ever says yes. Do not build from it without
his explicit yes.

**What this spec is:** a complete behavioural specification of the
`~/moviebox-watch` bridge as it existed on 14 Nov 2026 in the stopped
codespace 'vigilant umbrella' — reconstructed from the project's own records
(MEMORY.md, fix-round reports, the 10 Nov 2026 source research on MovieBox-TUI
v0.1.29, and the 5 surviving scripts in this folder). Where a detail was never
recorded, it is marked **UNRECORDED** — the builder must re-derive it from the
app at build time, not invent it silently.

**What already exists in this folder (do NOT rewrite):**
`audio-repair.sh` (packer, 118 lines), `sync.sh`, `run-app.sh`, `app.sh`,
`download.sh`, `player/` (empty). **Missing and specified below:** `server.py`,
`batch.py`, `batchctl.py`, `player/terminal.html`, `start.sh`, `watchdog.sh`,
`keepalive.sh`.

**NOT the base:** `~/workspace/moviebox-pc/watch/server.py` is a 31-line day-one
skeleton (password-protected static server on 127.0.0.1:8090). It is NOT the
real `server.py`. Do not use it as the base.

---

## 0. Platform & environment

- **Host:** GitHub Codespace, 2-core, 8 GB RAM, 32 GB disk, Ubuntu.
  Account `skyintheboxskyinbox-cpu`. Codespace 'vigilant umbrella'
  (`vigilant-umbrella-5v9rvppv99gjf4x94`). Free quota 120 core-hours/month.
  `idle_timeout_minutes` = 240 (verified; applies to new codespaces only).
  Port 8000 must be **Public** (restore it after any stop).
- **Tool versions (verified):** moviebox-tui 0.1.29, mpv 0.37.0,
  yt-dlp 2024.04.09, ffmpeg 6.1.1, ttyd 1.7.7, cloudflared 2026.10.7,
  tmux 3.4.
- **Paths:** bridge `~/moviebox-watch/`; app download home
  `~/Downloads/MovieBox-TUI/` (`Movies/<Title>/…`,
  `Series/<Title>/Season <N>/…`); library `~/moviebox-watch/media/`.
- **Processes / ports:** tmux session **`moviebox`** runs the app;
  **ttyd on 7681** (writable `-W`, served command is ONLY `moviebox-tui`,
  never a shell); **Python server on 8000** (Watch page + API + `/media`).
- **Access:** the phone's DNS cannot resolve `*.app.github.dev`, so the
  Watch page and the terminal reach the phone through **two Cloudflare
  Quick Tunnels** — one for port 8000 (Watch), one for port 7681
  (terminal/App). Tunnel URLs **change on every `start.sh` run**; always read
  the current ones from `tunnel-url.txt` / `app-url.txt` or `/api/links`.
- **Autostart does NOT work reliably** (`.devcontainer` postStartCommand
  never fired reliably). Recovery after any codespace restart =
  open Codespace + run `bash ~/moviebox-watch/start.sh` + hand over FRESH links.
- **The app exposes no API/plug.** Do not break into the app to extract its
  catalog/streams into a custom web UI. The bridge drives the UNMODIFIED app
  via tmux keys and reads disk truth.
- **All UI text ENGLISH only.**

## 1. `start.sh` (MISSING — spec)

The single entry point. Must be idempotent and safe to re-run. On run it:
1. Starts the app: `bash run-app.sh` (tmux session `moviebox` + app).
2. Starts the Python server (`server.py`, port 8000) if not already running.
3. Starts the sync loop (`sync.sh`, ~60 s sweep) if not already running.
4. **Re-arms the watchdog** (`watchdog.sh`) — the watchdog was killed on
   14 Nov 2026 after it stopped the codespace mid-download; `start.sh` is
   what brings it back. (See §7.)
5. Starts `keepalive.sh` if not already running.
6. Starts the two cloudflared Quick Tunnels (8000 and 7681) and writes the
   live URLs to `tunnel-url.txt` (Watch) and `app-url.txt` (terminal/App).
   Overwrite the files on every run — old URLs are dead after a restart.
7. Prints the fresh Watch + App URLs at the end.

## 2. `run-app.sh` (EXISTS — do not rewrite)

Launches MovieBox-TUI in the persistent tmux session `moviebox`. **Phone-size
window policy, persisted here:** `tmux set-option -g window-size smallest`
+ `aggressive-resize off`, applied globally and to the live session, so the
window always follows the smallest attached client (the user's phone,
~58 cols). **NEVER force the window wide in this file** (user order
14 Nov 2026: no widening at all — "chauda na kara karee"). Session is
created with `tmux new-session -d -s moviebox -x 80 -y 24 "bash
$WATCH_DIR/app.sh"` only if it does not already exist.

## 3. `app.sh` (EXISTS — do not rewrite)

Finds `moviebox-tui` on PATH; if missing, installs it with
`cargo install --git https://github.com/mesamirh/MovieBox-Tui` (first
`--locked`, then without), falls back to `~/.cargo/bin/moviebox-tui`;
if still missing, prints the manual install command and drops to a shell.
Creates `~/Downloads/MovieBox-TUI` and `exec`s the binary.

## 4. `server.py` (MISSING — spec)

Python 3, stdlib only (`http.server.ThreadingHTTPServer`-based, as in the
day-one skeleton). Binds port 8000. Serves the Watch page, the JSON API, and
the media files. Must survive restarts and concurrent requests (page polls
+ media streaming + batch control at once).

### 4.1 Pages & static
- `GET /` → `player/terminal.html` (the Watch page).
- `GET /api/links` → JSON with the live tunnel URLs (Watch + App), read
  from `tunnel-url.txt` / `app-url.txt`.

### 4.2 Library — `GET /api/downloads` and `GET /api/list`
- Walk `~/Downloads/MovieBox-TUI` **to depth 4** (`Movies/<Title>/…`,
  `Series/<Title>/Season <N>/…`) plus `media/`; **dedupe** (same title
  appearing in both places is one item).
- **Skip `.temp` files.** Include finished `mp4/mkv/webm/m4v` (+ `.m4a`
  only as a sibling-audio signal, never as a playable item).
- **Group series:** one card per series — `{kind: 'series', episodes: [...]}`,
  grouped by the app folder layout `Series/<Title>/Season <N>/` + `SxxEyy`
  filename patterns.
- Per item: title, kind (film/series), size, duration (ffprobe), packed
  `full_file` when a `.full.mp4` exists, `no_audio` flag when the served
  file has no audio track (see §4.3), poster URL when known (see §12).
- `/api/list` returns the same shelf items (last verified: 20 items for
  Dragon Ball Super S1).

### 4.3 Media — `GET /media/<path>`
- Serve files from `media/` (the synced library). **Range support is
  mandatory** (seeking must work).
- **302-redirect rule:** a FRESH request for a `.f<digits>.mp4` redirects
  to its `.full.mp4` (packed) file when one exists. A mid-file Range
  request serves the raw file (do not redirect mid-stream).
- **Self-heal:** if the file being served has no audio and a sibling
  `.f3.m4a`/`.f1.m4a` exists, trigger the packer and flag `no_audio` on
  the card.
- `GET /live` exists **only as an error fallback** for playback. Play is
  the file itself via `/media` — the user rejected any forced `/live`
  chunk route (10 Nov 2026).

### 4.4 Heartbeat — `POST /api/heartbeat`
- The page sends this **every 30 s ONLY while the page is visible**
  (visibility-gated). The watchdog/keepalive treat it as activity.
- The `/api/downloads` poll is visibility-gated the same way.
- Background: the original bug (11 Nov 2026) was the page polling
  `/api/downloads` every 3 s, which the watchdog counted as activity, so
  any open/background tab kept the codespace alive forever.

### 4.5 Playback sessions
- Launching a film via the page **registers a playback session**
  (`playback-session.json`: file + duration). The watchdog treats a live
  session as activity **until the duration passes**, then the 15-minute
  idle window starts. (Added 12 Nov 2026.)

### 4.6 Secret shutdown — `POST /api/shutdown`
- Runs `gh codespace stop`. No confirmation page, no auth beyond the
  tunnel URL itself. (User's own design; phone-confirmed 11 Nov 2026.)

### 4.7 Batch control
- `POST /api/batch/start` with JSON `{title, season, count}` — starts
  `batch.py` for that series/season (see §5). Reject if a batch is
  already running.
- `POST /api/batch/stop` — stops the batch via `batchctl.py` **and**
  runs `tmux set-option -t moviebox window-size smallest` +
  `aggressive-resize off` **synchronously before the response returns**
  (this is the stop-path phone-size restore, proven 14 Nov 2026).
- `GET /api/batch/status` — running/stopped, progress (done/total),
  current episode, current provider, failures with reasons.
- `GET /api/batch/log` — the batch log (tail).

## 5. `batch.py` (MISSING — spec)

Drives the UNMODIFIED MovieBox-TUI per episode via tmux keys, verifying
from **disk truth** before advancing. Skips episodes already owned.
Pulses `/api/downloads` every ~5 min while running (so the shelf stays
fresh). State in `batch-state.json`.

### 5.1 The app's own season batch (know it — the driver is NOT the only path)
MovieBox-TUI v0.1.29 has a **built-in season download** (verified from the
app's own source, 10 Nov 2026): on a series' Details screen, with the
**Seasons pane** focused, pressing **`d`** queues the whole season and
downloads episodes one after another in the background, skipping episodes
already on disk (footer literally reads `[d] Download Season`). One season
per press; no all-seasons shortcut. Quality is not chosen per episode (top /
highest-resolution stream). Subtitle choice on the first episode applies
season-wide; **Esc on the subtitle popup cancels the whole queue**.
Downloads only advance while the app stays on the Details screen.
**The driver below exists because the user needs per-episode verification
(truncation + provider switching) and count-limited batches — the built-in
path cannot do either.** The rebuild should keep both: built-in `d` for a
plain whole season, the driver when checks are required.

### 5.2 The user's locked driver flow
**Hindi choose → Episodes pane → select next episode → press download →
WAIT till that episode downloads → next → next.** No extra steps.

### 5.3 Prelude (the fix that made it work, 14 Nov 2026)
Before `goto_episodes`, the driver MUST:
1. Explicitly select the **Hindi** audio track + press Enter, and **wait
   for streams to load** before proceeding. (Earlier versions never
   confirmed the audio track, and the batch stalled on the Audio pane
   with "Choose an audio track to load streams.")
2. Handle the subtitle popup: pick **English** when offered; if no English
   is offered, pick **"No subtitles"** (or the first option) — the popup
   must never be left open to block a download.
3. Log `prelude: audio set to Hindi - N streams loaded`.

### 5.4 Focus (the other fix, 14 Nov 2026)
- `goto_episodes()`: **4× Tab cycle + Down probe** proving the Episodes
  pane has keyboard focus. It must ALSO require the key bar to show
  **`[d] Download Episode`** — the old bug was returning True on the
  header `E{n}` label alone, so Up/Down keys went to the wrong pane.
- `select_episode()`: read the highlighted row with
  `highlighted_episode()` (reads the **bold** selected row).
  `current_episode()` always reading `None` was the old bug (it marched
  to the last row).
- Streams pane: some providers (4KHDHub) open a Streams pane first —
  `wait_streams` + `choose_best_stream` steps are required before
  download.

### 5.5 Waiting & truncation (the provider-switch rule, 14 Nov 2026)
- `wait_for_media()` returns **`ok` / `truncated` / `missing` / `stopped`**.
- **Truncation rule:** refuse a file that is **>120 s short of its
  reference AND <80% of it**. Reference = the episode's own subtitle end
  if present, else the median duration of sibling episodes.
  (Background: E12–E20 came back exactly ~900 s against a normal ~1387 s
  — provider-side cut in video AND audio while subtitles ran full length.)
- On `truncated`: **remove the cut file**, switch provider, retry.
- **Never silently accept a cut file as DONE.**

### 5.6 Providers
Cycle with **Ctrl+P** on the home screen, in this order:
`MovieBox, 4KHDHub, Dramachi, CircleFTP (BDIX), DhakaFlix (BDIX)`.
- On a truncated episode: remove the cut file, switch to the next
  provider, retry. **Remember the working provider** for the rest of the
  batch. **Cap tries at all providers once per episode**, then mark the
  episode **FAILED with the reason shown live**.
- **BDIX providers (CircleFTP, DhakaFlix) are effectively unreachable
  from this network** (the TUI says they require a BDIX network);
  DhakaFlix stalls are stream stalls, not driver faults. Partial `.f0`
  files from stalls remain on disk (harmless — `is_episode_file` excludes
  `.f0` from "already have").

### 5.7 Subtitle & audio rules (settled by the user)
- Subtitles chosen **AUTOMATICALLY as ENGLISH, every time**.
- The dub/audio track is only ever **Hindi or English**.
- Episode files carry **no language tag** (`TAG:language=und`) — Hindi
  confirmation rests on the driver's selection logs, not file metadata.

### 5.8 Window size — widen DURING, restore AFTER (settled compromise)
- The Episodes pane does not render at narrow sizes, so `batch.py` calls
  `ensure_window_size(200×50)` during a batch.
- **`restore_phone_size()` MUST run afterwards, every time:**
  `main()` wrapped in `try/finally` so every batch hands the size back to
  the phone on normal exit; the stop path restores inside the stop handler
  itself (see §6). Log: `tmux window size restored to smallest client
  (phone)`.
- **NEVER leave the window wide as a permanent state** (user order).

### 5.9 Disk guard, stop, logs
- **Disk guard:** stop the batch if the projection would leave **< 2 GB
  free**.
- **Stop must clear the batch within ~1 second** ("Stopped by user -
  X of Y new episodes done"). Stop does NOT cancel the TUI's own
  in-progress download (only the batch) — known gap.
- Log lines the operator/user recognises: `prelude: audio set to Hindi -
  N streams loaded`, `tmux window size restored to smallest client
  (phone)`, `Stopped by user - X of Y new episodes done`.
- **yt-dlp fallback** (`download.sh`, §11) exists for when the app's own
  download path fails. **It has never been triggered live** — keep it as
  a last resort only.

## 6. `batchctl.py` (MISSING — spec)

- `killnow()`: kills the batch process **and** yt-dlp, and calls
  `restore_phone_size()` **synchronously just before its return**.
- The main `stop` branch calls `restore_phone_size()` **again after
  `killnow()`**, and it must run **even if `killnow()` raises**.
- `batch.py`'s `finally` restore stays as a fallback.
- (Proven 14 Nov 2026: 4 stop cycles, ~1 s each, window 200×50 → 58×40.)

## 7. `watchdog.sh` (MISSING — spec)

- Watches activity: `/api/heartbeat` (page visible, every 30 s) and
  `/media` requests.
- **If no activity for a 15-minute idle window → stop the codespace**
  (`gh codespace stop`).
- **Batch guard:** while a batch is running (`batch-state.json`), do NOT
  stop — even if the page is hidden.
- **Playback-session guard:** a live playback session
  (`playback-session.json`) counts as activity until its duration passes;
  only then does the 15-minute window start.
- **History:** the watchdog was **killed** on 14 Nov 2026 (by the finish
  worker) after it stopped the codespace mid-download at ~12:52 UTC.
  Until `start.sh` re-runs (which re-arms it), the codespace will NOT
  auto-stop — the user must stop it himself (hold Online 4 s →
  Shut down) or quota burns. The rebuild must keep this behaviour:
  watchdog armed by `start.sh`, and the user must be told plainly whenever
  it is disarmed.

## 8. `keepalive.sh` (MISSING — spec)

- Companion prober that observes/touches `/api/heartbeat` and `/media`
  (same activity surface as the watchdog).
- **Documented fact: keepalive pokes are USELESS for GitHub's idle
  timer.** They do not extend the codespace lifetime. Do not present
  them as a guard; the watchdog (§7) is the real guard.

## 9. `sync.sh` (EXISTS — do not rewrite)

Loop (~every 60 s): depth-4 walk of `~/Downloads/MovieBox-TUI`, moves
finished `mp4/mkv/webm/m4v` + `.m4a` into `media/` (fragments + sibling
audio stay together so the packer can merge them), dedupes by basename,
skips `.temp` files, then calls `audio-repair.sh sweep`.
`sync.sh once` does a single sweep (for manual use).

## 10. `audio-repair.sh` — the packer (EXISTS — do not rewrite)

- The app's `.f<digits>.mp4` downloads are fragmented MP4s; some have NO
  audio, the audio living in a sibling `.f3.m4a` / `.f1.m4a`.
- **Packing = COPY-remux into a standard faststart MP4 (`.full.mp4`),
  merging sibling audio when missing. Originals are never touched.**
- Hardened behaviour (from live fixes): post-pack VERIFY (audio +
  faststart) before install; never delete a good packed file before a
  verified replacement exists; a `.m4a` arrival triggers packing (sweep
  finds the orphan video); per-file lock; growth check (file must be
  stable before packing); packed duration must not be shorter than the
  source by more than 2 s.
- **H.264 conversion is CANCELLED by the user ("time lagta h") — no
  `.play.mp4`, no auto-convert, ever.**

## 11. `download.sh` (EXISTS — do not rewrite)

yt-dlp fallback for one episode: `download.sh "<title>" <season>
<episode> [query]`; searches `ytsearch1:` for
`<Title> S<ss>E<ee>`; format `bv*[height<=1080]+ba/b[height<=1080]`,
merged to mp4; lands in
`~/Downloads/MovieBox-TUI/Series/<Title>/Season <N>/`.
**Never triggered live — last resort only.**

## 12. `player/terminal.html` (MISSING — spec)

The Watch page. Single file, no build step. All UI text ENGLISH only.

### 12.1 Header
- Deep-navy bar (**#0d1b30**), cyan rounded-square play logo +
  "MovieBox" wordmark on the left, **Online pill** on the right.
- **Colour picker lives IN the logo** (tap the logo → 5 colours).
  The accent recolours the logo + primary buttons (Play, Back·Esc,
  Download). Persists via `localStorage` key `mb_accent`.
  **Default accent is CYAN #22d3ee.** (The user's own browser shows
  purple — his saved pick. The exact other 4 colours are UNRECORDED;
  re-choose at build time, must include cyan #22d3ee and purple.)
- **Secret shutdown:** press-and-HOLD the Online pill **4 s** → a hidden
  "Shut down" button appears → tapping it shows "Shutting down…" and
  POSTs `/api/shutdown`. (User's own design; phone-confirmed 11 Nov 2026.)

### 12.2 Terminal + key bar
- The terminal is the real MovieBox-TUI in the tmux session, embedded
  via the ttyd tunnel URL (from `/api/links`).
- Key bar (original, 5 buttons): **Back·Esc, ↑, ↓, Tab, Enter**.
- **Driver-control lock (user-named feature, proven 14 Nov 2026):**
  while a batch runs, the terminal box shows a **pulsing purple glow** +
  a **"Driver in control — terminal locked until Stop"** pill, all 5
  key-bar buttons are disabled, locked-key clicks are refused; **Stop
  stays clickable** and clears the batch within seconds; the lock releases
  when a batch finishes; it **survives page reload**.
- While a batch runs the user must NOT touch the app himself (same tmux
  session — keys fight/desync).

### 12.3 Downloads shelf
- **One CARD per downloaded film**, with loading/progress; **Play when
  done, Delete**. Downloads stay on the cloud machine, NOT the phone.
- **Series cards:** one card per series, expanding to episode rows with
  **Play / Open in VLC / Delete** (packed `full_file` used).
  Each series card also gets a **count select + Download button + live
  progress + Stop** (batch control).
- **Disk meter** + **filter box + sort**.
- **Flicker-free keyed-DOM card rendering.**
- **Play = the file itself via `/media`** (no forced `/live`; `/live`
  only as error fallback). The phone browser plays HEVC directly.
- **VLC:** NO "Open in VLC" button on film cards (removed 12 Nov 2026,
  user order). VLC stays **inside the Play popup** and on **series
  episode rows**. Desktop VLC builds `vlc://<origin>/media/<file>`; it
  does NOT auto-open on the user's computer (no registered handler) —
  the **copy-link popup (dark)** is the working route.
- **Player popup:** "Start from beginning" button kept; **resume is
  AUTOMATIC on player open — there is NO "Resume from X:XX" button**
  (removed 14 Nov 2026, user order). Buttons in a **2-column
  equal-width grid**. **Next-episode auto:** episode end → "Next episode
  in 5 s" bar with "Play next now" + Cancel (verified E1→E5
  auto-advance, 12 Nov 2026).
- **In-player subtitles and picture-in-picture are DECLINED** (user,
  12 Nov 2026) — do not build. **In-page Convert is SHELVED.** No
  watched-progress, no dates, no 3-step guide. No remote control, no
  custom catalog.

### 12.4 Layout
- **Desktop:** `@media ≥1200px` two-column grid (440px Downloads side
  panel, **520px at ≥1600px**); terminal `calc(100vh−270px)`; panel
  scrolls independently. **Phone rules untouched.**
- **Heartbeat:** POST `/api/heartbeat` every 30 s **only while visible**;
  `/api/downloads` poll visibility-gated the same way (§4.4).

### 12.5 Posters / metadata
- **Poster rule:** year from folder/filename is authoritative. Movies try
  Wikipedia `Title (year film)` **before** TVmaze. A no-poster tile shows
  the **FULL NAME**, not initials.
- Metadata sources: TVmaze healthy, iTunes RSS healthy, Jikan
  unreachable/flaky, iTunes Search API returns `resultCount` 0.
  Fallbacks include AniList GraphQL and Wikipedia.

## 13. Data files & state (in `~/moviebox-watch/`)

| File | Purpose |
|---|---|
| `server.py`, `batch.py`, `batchctl.py` | The missing heart — see §4–6 |
| `start.sh`, `watchdog.sh`, `keepalive.sh` | Missing — see §1, §7, §8 |
| `run-app.sh`, `app.sh`, `sync.sh`, `audio-repair.sh`, `download.sh` | Exist in this folder — do not rewrite |
| `player/terminal.html` | Missing — see §12 |
| `tunnel-url.txt`, `app-url.txt` | Current tunnel URLs (rewritten every `start.sh` run) |
| `batch-state.json` | Batch status/progress (read by the watchdog guard + `/api/batch/status`) |
| `playback-session.json` | Playback session: file + duration (read by the watchdog guard) |
| `media/` | Synced library (packed `.full.mp4` + fragments + sibling audio) |
| `cloudflared` | Cloudflared binary |

## 14. Library & verification targets (state on 14 Nov 2026)

- **Dragon Ball Super S1 — E01–E24 ALL FULL and ready (24 of 24):**
  E01 Japanese original 360p; E02–E11 Hindi 1080p packed (~1387 s);
  E12 Hindi full from MovieBox (1387.2 s); E13/E14 Hindi full from
  MovieBox; E15–E20 re-downloaded from MovieBox at 1387.302583 s
  (replacing the 4KHDHub ~1402 s copies the user deleted as
  English/oversized); E21, E22 MovieBox (1387.302583 s); E23
  (1387.302583 s), E24 (1387.260875 s). All Hindi per the driver's
  selection logs (files carry no language tag).
- **Films:** the app-side `Movies/` folders were deleted 14 Nov 2026
  (they held only audio-only `.f3.m4a` tracks). Packed film copies in
  `media/` were present as of 14 Nov 18:55 IST (Backrooms, HTTYD,
  Resident Evil, Avatar) — survival after the cleanup is UNVERIFIED.
- **Chernobyl S1:** no video file anywhere on disk; only a subtitle file
  existed (also deleted). Re-download is the only route back.
- **Disk:** ~1.7 GB free (95% used) at last check before the fix-round
  re-downloads; exact current free space UNVERIFIED (codespace stopped).

## 15. Acceptance tests (the proven checks — re-run all of them)

1. Batch counts 1, 2, 3, 5 run live end-to-end (10/20/"All remaining"
   were **never proven live** — test them before claiming).
2. All 24 DBS episodes land full-length (~1387 s), Hindi, packed.
3. Truncated episode (~900 s) triggers provider switch and lands full.
4. Stop clears a batch in ~1 s AND the terminal returns to phone size
   (58×41) — on a real phone, not just a simulated client.
5. Phone view: compact logo, full search box, Resume/Favorites, [T][?][q]
   fitting at ~58 cols; no cut-off logo/search.
6. Watch page healthy: header, terminal iframe rendering (not black),
   shelf == batch agreement (24 of 24).
7. Playback session keeps the codespace alive through a full film;
   secret shutdown works (hold Online 4 s → Shut down).
8. Next-episode auto-advance works (E1→E5 verified pattern).
9. Watchdog: no activity 15 min → codespace stops; batch running →
   no stop; playback session live → no stop until duration passes.

## 16. DO NOT BUILD (rejected — do not re-add unless the user orders by name)

- Ani-CLI + MovieCLI + 3-button switcher + Type box + Ctrl+C button
  (built 10 Nov, rejected, removed 11 Nov 2026).
- H.264 conversion / auto-convert (cancelled by user).
- The "new design" Space UI as a whole (rejected 11 Nov 2026 — "the
  NORMAL UI is better; we want to watch films, not UI"). Only the header
  was taken from it.
- In-player subtitles and picture-in-picture (declined 12 Nov 2026).
- The "Resume from X:XX" popup button (removed 14 Nov 2026).
- Any PERMANENT widening of the tmux window / leaving the phone view wide.
- Preview-only ideas from the rejected design (wide header art on the real
  page, film posters in the small cards, panel simplification).
- Remote control / custom catalog / watched-progress / dates / 3-step guide.
- A second free GitHub account for quota (ToS §B.3 — one free account per
  person). Faking student status for the Student Pack.

## 17. Known gaps (state plainly if asked)

- BDIX providers (CircleFTP, DhakaFlix) effectively unreachable from this
  network.
- Partial `.f0` files from DhakaFlix stalls remain on disk (harmless).
- Episode files carry no language tag (`und`) — Hindi rests on driver logs.
- Stop does not cancel the TUI's own in-progress download (only the batch).
- The yt-dlp fallback branch was never triggered live.
- Desktop `vlc://` launching VLC on the user's own computer is unproven
  (no registered handler there); the popup Dismiss on his own devices is
  unproven.
- The Watch page terminal iframe rendered black in the test browser while
  capture-pane showed the UI (14 Nov) — phone confirmation came later;
  re-verify on any rebuild.

## 18. Backup discipline (so this never happens again)

On the FIRST successful start of any rebuild, before anything else:
1. Commit `~/moviebox-watch` into a git repo and push it (the fork
   `skyintheboxskyinbox-cpu/MovieBox-Tui` on a new branch, or a new
   private repo).
2. Copy the media out (`~/Downloads/MovieBox-TUI`) — tar + transfer;
   verify file count + sizes before stopping anything.
3. Only then resume watching. **The code must never again exist in exactly
   one place.**
