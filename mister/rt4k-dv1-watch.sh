#!/bin/sh
# rt4k-dv1-watch.sh — give the RT4K's DV1 the fallback its firmware refuses to have.
#
# The problem, observable at any time: DV1 switches profiles from the metadata
# the MiSTer tags its output with, and it works -- load AcornAtom and the RT4K
# loads /profile/DV1/AcornAtom.rt4 by itself. But a menu core running with
# `[menu] direct_video=0` (the usual fix for the menu rendering garbled under
# direct video) emits UNTAGGED 1080p, so no DV1 rule can match it, and the RT4K
# simply keeps whatever the last game left loaded. The menu ends up drawn
# through a profile built for something else. /profile/DV1/MENU.rt4 can exist
# and DV1 will never select it. Some cores also miss DV1 for their own reasons;
# list them in the map too.
#
# The fix is `prof load`, which the 1.75.0 serial interface added: watch
# /tmp/CORENAME and load a profile by name for the cores DV1 cannot see.
#
# DELIBERATELY CONSERVATIVE: it acts only on core names listed in the map, and
# the shipped map contains MENU alone. Every real core is left to DV1, which
# already gets them right -- a watcher that fired on everything would race DV1
# and override its correct choice.
#
#   rt4k-dv1-watch.sh                 # daemon: watch and act (used at boot)
#   rt4k-dv1-watch.sh --once          # act on the current CORENAME, then exit
#   rt4k-dv1-watch.sh --lookup MENU   # print the mapped profile, no device needed
#
# Cost on the MiSTer, measured 2026-09-08: inotifywait blocks in the kernel at
# ~930 KB RSS and 0 % CPU (no polling), and each core switch spends one ~0.9 s
# python3 invocation.
set -u

CORENAME="${RT4K_CORENAME_FILE:-/tmp/CORENAME}"
HERE="$(cd "$(dirname "$0")" && pwd)"
MAP="${RT4K_DV1_MAP:-$HERE/rt4k-dv1-map.conf}"
TOOL="${RT4K_SERIAL_TOOL:-$HERE/rt4k-serial.py}"
# NOT directly in /tmp: that is the directory being watched, and a log written
# there is itself an inotify event. This watcher filters on the filename so it
# would not actually loop -- but an earlier ad-hoc `inotifywait -e modify /tmp >
# /tmp/x.log` did exactly that and filled the 247 MB tmpfs. inotify is not
# recursive, so a subdirectory is invisible to the /tmp watch.
LOG="${RT4K_DV1_LOG:-/tmp/rt4k-dv1/watch.log}"
# Advisory lock taken by rt4k-serial.py for every command. Two
# processes on one tty interleave: measured 2026-09-08, this watcher's
# `prof load` reached the RT4K but its reply was consumed by the other reader,
# so it logged a failure that had not happened.
LOCK="${RT4K_SERIAL_LOCK:-/tmp/rt4k-serial.lock}"
LOG_MAX=${RT4K_DV1_LOG_MAX:-262144}

# Built-in default, used when no map file exists. MENU is the whole point.
default_map() {
    printf 'MENU\tDV1/MENU.rt4\n'
}

log() {
    mkdir -p "$(dirname "$LOG")" 2>/dev/null
    # /tmp is tmpfs: / has ~13 MB free and the card should not take a write per
    # core switch. Capped so a flapping core cannot fill tmpfs either.
    [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt "$LOG_MAX" ] && : > "$LOG"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
}

lookup() {
    # A line is  <core name><TAB><profile path>  with an optional trailing
    # " # comment". Both halves can contain spaces -- core names do ("Game and
    # Watch", "Sord M5", "Amstrad PCW") and so do profile paths ("_CRT
    # Emulation/..."), so the split is on the FIRST TAB, not on whitespace.
    # Whitespace is still accepted as a separator for hand-written lines that
    # have no spaces in the core name.
    core="$1"
    if [ -f "$MAP" ]; then cat "$MAP"; else default_map; fi | awk -v core="$core" '
        /^[[:space:]]*#/  { next }
        /^[[:space:]]*$/  { next }
        {
            ti = index($0, "\t")
            if (ti > 0) { name = substr($0, 1, ti - 1); rest = substr($0, ti + 1) }
            else        { name = $1; rest = $0
                          sub(/^[[:space:]]*[^[:space:]]+[[:space:]]+/, "", rest) }
            sub(/^[[:space:]]+/, "", name); sub(/[[:space:]]+$/, "", name)
            if (name != core) next
            sub(/[[:space:]]+#.*$/, "", rest)      # drop a trailing comment
            sub(/^[[:space:]]+/, "", rest); sub(/[[:space:]]+$/, "", rest)
            if (rest != "") print rest
            exit
        }'
}

apply() {
    core="$1"
    profile=$(lookup "$core")
    if [ -z "$profile" ]; then
        log "core=$core no mapping -- leaving it to DV1"
        return 0
    fi
    if [ -d "$LOCK" ]; then
        pid=$(cat "$LOCK/pid" 2>/dev/null)
        if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
            log "core=$core WANTED '$profile' but the serial port is held by pid $pid -- deferring"
            return 0
        fi
        rm -rf "$LOCK"                 # stale: the holder is gone
    fi
    if [ ! -f "$TOOL" ]; then
        log "core=$core WANTED '$profile' but $TOOL is missing"
        return 1
    fi
    out=$(RT4K_LOCAL=1 python3 "$TOOL" "prof load $profile" 2>&1)
    case "$out" in
        *"prof load ok"*) log "core=$core -> $profile OK";;
        # The RT4K being off, or a sync/deploy holding the port, is normal and
        # must not kill the watcher -- the next core switch tries again.
        *) log "core=$core -> $profile FAILED: $(echo "$out" | tr '\n' ' ')"; return 1;;
    esac
}

case "${1:-}" in
    --lookup) lookup "${2:-}"; exit 0;;
    --once)   apply "$(cat "$CORENAME" 2>/dev/null)"; exit $?;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
esac

command -v inotifywait >/dev/null 2>&1 || { log "inotifywait not found; refusing to busy-poll"; exit 2; }

log "watching $CORENAME (map=$MAP)"

# Apply what is loaded RIGHT NOW before waiting for an event. At boot the
# MiSTer writes CORENAME=MENU before this script is watching, so a purely
# event-driven watcher leaves the menu on whatever profile the last session
# ended with -- observed after a reboot 2026-09-08, menu showing DV1/Chip8.rt4.
last=$(cat "$CORENAME" 2>/dev/null)
[ -n "$last" ] && apply "$last"
# MiSTer writes CORENAME twice per core load (MODIFY + CLOSE_WRITE, twice --
# measured), so act on the CONTENT changing, not on the event.
inotifywait -q -m -e close_write --format '%f' "$(dirname "$CORENAME")" 2>/dev/null | while IFS= read -r f; do
    [ "$f" = "$(basename "$CORENAME")" ] || continue
    core=$(cat "$CORENAME" 2>/dev/null)
    [ -n "$core" ] || continue
    [ "$core" = "$last" ] && continue
    last="$core"
    apply "$core"
done
