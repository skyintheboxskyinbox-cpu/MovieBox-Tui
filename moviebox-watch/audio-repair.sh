#!/usr/bin/env bash
# audio-repair.sh — the packer. The app's .f<digits>.mp4 downloads are
# fragmented MP4s; some have NO audio, the audio living in a sibling
# .f3.m4a / .f1.m4a. Packing = COPY-remux into a standard faststart MP4
# (.full.mp4), merging sibling audio when missing. Originals never touched.
#
# Hardened behaviour (from live fixes):
#   - post-pack VERIFY (audio + faststart) before install
#   - never delete a good packed file before a verified replacement exists
#   - a .m4a arrival triggers packing (sweep finds the orphan video)
#   - per-file lock, growth check (file must be stable before packing)
#
# Usage: audio-repair.sh sweep          — pack everything packable in media/
#        audio-repair.sh <file>         — pack one file
set -u
WATCH_DIR="$HOME/moviebox-watch"
MEDIA="$WATCH_DIR/media"
LOCKDIR="$WATCH_DIR/.pack-locks"
mkdir -p "$MEDIA" "$LOCKDIR"

ff_has_audio() { ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$1" 2>/dev/null | grep -q .; }
ff_is_faststart() { ffprobe -v trace "$1" 2>&1 | grep -q "faststart" || python3 -c "
import sys
data = open(sys.argv[1],'rb').read(1024*1024)
# moov before mdat in the first MB means faststart
sys.exit(0 if data.find(b'moov') != -1 and data.find(b'moov') < data.find(b'mdat') else 1)
" "$1"; }
ff_duration() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null; }

# sibling audio: <base>.f1.m4a / .f3.m4a / .f0.m4a next to the video, or any
# .m4a sharing the same stem before the .f<digits> part.
sibling_audio() {
    local vid="$1" dir stem cand
    dir="$(dirname "$vid")"
    stem="$(basename "$vid" | sed -E 's/\.f[0-9]+\.mp4$//; s/\.(mp4|mkv|webm|m4v)$//')"
    for cand in "$dir/$stem.f3.m4a" "$dir/$stem.f1.m4a" "$dir/$stem.f0.m4a" "$dir/$stem.m4a"; do
        [ -f "$cand" ] && { echo "$cand"; return 0; }
    done
    # Fallback: any .m4a in the same dir whose name starts with the stem.
    local m
    m="$(ls "$dir/$stem"*.m4a 2>/dev/null | head -1 || true)"
    [ -n "$m" ] && echo "$m" && return 0
    return 1
}

pack_one() {
    local src="$1"
    [ -f "$src" ] || return 0
    local base dir full lock
    base="$(basename "$src")"
    dir="$(dirname "$src")"
    # Only fragments need packing; skip files that are already .full.mp4.
    case "$base" in *.full.mp4) return 0;; *.f[0-9]*.mp4) ;; *) return 0;; esac
    full="${src%.f*.mp4}.full.mp4"
    # If a good packed file already exists, leave it (re-pack only if broken).
    if [ -f "$full" ] && ff_has_audio "$full" 2>/dev/null; then
        return 0
    fi
    # Per-file lock.
    lock="$LOCKDIR/$(echo -n "$src" | md5sum | cut -d' ' -f1).lock"
    exec 9>"$lock" 2>/dev/null || return 0
    flock -n 9 || return 0
    # Growth check: file must be stable (not still downloading).
    local s1 s2
    s1="$(stat -c%s "$src" 2>/dev/null || echo 0)"
    sleep 5
    s2="$(stat -c%s "$src" 2>/dev/null || echo 0)"
    if [ "$s1" != "$s2" ]; then
        echo "packer: $base still growing ($s1 -> $s2), skip"
        return 0
    fi
    local tmp="$full.packing.mp4" audio_args=()
    local sib; sib="$(sibling_audio "$src" || true)"
    echo "packer: packing $base (audio sibling: ${sib:-none})"
    if ff_has_audio "$src" 2>/dev/null; then
        # Video already has audio: plain copy-remux to faststart.
        ffmpeg -y -v error -i "$src" -map 0 -c copy -movflags +faststart "$tmp" || { rm -f "$tmp"; return 0; }
    elif [ -n "$sib" ]; then
        # Merge sibling audio.
        ffmpeg -y -v error -i "$src" -i "$sib" -map 0:v:0 -map 1:a:0 -c copy -movflags +faststart "$tmp" || { rm -f "$tmp"; return 0; }
    else
        echo "packer: $base has no audio and no sibling yet, deferring"
        return 0
    fi
    # VERIFY before install: audio present + faststart + sane duration.
    if ! ff_has_audio "$tmp" 2>/dev/null; then
        echo "packer: VERIFY FAILED (no audio) for $base, discarding"
        rm -f "$tmp"; return 0
    fi
    local d0 d1
    d0="$(ff_duration "$src" || echo 0)"; d1="$(ff_duration "$tmp" || echo 0)"
    # Packed duration must not be shorter than the source by more than 2 s.
    if python3 -c "import sys; sys.exit(0 if float('$d1') >= float('$d0') - 2 else 1)" 2>/dev/null; then
        mv "$tmp" "$full"
        echo "packer: installed $full (${d1}s)"
    else
        echo "packer: VERIFY FAILED (duration $d0 -> $d1) for $base, discarding"
        rm -f "$tmp"; return 0
    fi
}

if [ "${1:-}" = "sweep" ]; then
    for f in "$MEDIA"/*.f[0-9]*.mp4; do
        [ -e "$f" ] || continue
        pack_one "$f"
    done
    # Orphan audio arrival: a .m4a whose video was deferred may now pack.
    for m in "$MEDIA"/*.m4a; do
        [ -e "$m" ] || continue
        stem="$(basename "$m" .m4a | sed -E 's/\.f[0-9]+$//')"
        for f in "$MEDIA/$stem".f[0-9]*.mp4; do
            [ -e "$f" ] && pack_one "$f"
        done
    done
    exit 0
fi

[ -n "${1:-}" ] && pack_one "$1"
