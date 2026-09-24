#!/bin/sh
# install.sh -- install retrothink onto a MiSTer, or remove it.
#
#   ./install.sh root@<mister>              from a workstation, over ssh
#   ./install.sh --uninstall root@<mister>
#   sh install.sh --local [--uninstall]     on the MiSTer itself
#
# Everything lands in /media/fat/retrothink/. The only file outside it that is
# touched is /media/fat/linux/user-startup.sh, which gets two marked blocks:
# the udev rename at the TOP (it must run before Zaparoo starts) and the DV1
# watcher at the bottom. The script is idempotent: re-running it replaces the
# blocks rather than adding more, and an existing rt4k-dv1-map.conf is kept.
# user-startup.sh is backed up to user-startup.sh.retrothink-bak first.
set -eu

FAT="${RETROTHINK_FAT:-/media/fat}"
DEST="$FAT/retrothink"
STARTUP="$FAT/linux/user-startup.sh"
HERE="$(cd "$(dirname "$0")" && pwd)"

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

local_mode=0; uninstall=0; host=""
for a in "$@"; do
    case "$a" in
        --local) local_mode=1;;
        --uninstall) uninstall=1;;
        -h|--help) usage;;
        -*) echo "unknown option: $a" >&2; usage;;
        *) host="$a";;
    esac
done

if [ "$local_mode" -eq 0 ]; then
    [ -n "$host" ] || usage
    flag=""; [ "$uninstall" -eq 1 ] && flag="--uninstall"
    echo "copying installer to $host ..."
    (cd "$HERE" && tar cf - install.sh scripts/rt4k-serial.py mister) |
        ssh -o BatchMode=yes "$host" \
            "rm -rf /tmp/retrothink-install && mkdir -p /tmp/retrothink-install && \
             tar xf - -C /tmp/retrothink-install && \
             sh /tmp/retrothink-install/install.sh --local $flag; rc=\$?; \
             rm -rf /tmp/retrothink-install; exit \$rc"
    exit $?
fi

# Print FILE without the block named NAME.
strip_block() {
    awk -v n="$2" '
        $0 == "# >>> retrothink: " n " >>>" { skip = 1; next }
        skip && $0 == "# <<< retrothink: " n " <<<" { skip = 0; next }
        !skip' "$1"
}

strip_all() {
    tmp="$STARTUP.retrothink-tmp"
    strip_block "$STARTUP" "rt4k-serial udev rule" > "$tmp"
    strip_block "$tmp" "DV1 fallback watcher" > "$STARTUP.retrothink-tmp2"
    mv "$STARTUP.retrothink-tmp2" "$STARTUP"; rm -f "$tmp"
}

stop_watcher() {
    # The watcher is a shell loop with an inotifywait child; stop both.
    pkill -f "$DEST/rt4k-dv1-watch.sh" 2>/dev/null || true
    pkill -f "inotifywait -q -m -e close_write --format %f /tmp" 2>/dev/null || true
}

if [ "$uninstall" -eq 1 ]; then
    [ -f "$STARTUP" ] && { cp "$STARTUP" "$STARTUP.retrothink-bak"; strip_all; }
    [ -z "${RETROTHINK_FAT:-}" ] && stop_watcher
    rm -f /run/udev/rules.d/99-rt4k-serial.rules 2>/dev/null || true
    rm -rf "$DEST"
    echo "retrothink removed. Reboot to restore the RT4K's /dev/ttyUSB name."
    exit 0
fi

[ -d "$FAT" ] || { echo "$FAT not found -- is this a MiSTer?" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not found" >&2; exit 1; }
command -v inotifywait >/dev/null 2>&1 ||
    echo "WARNING: inotifywait not found -- the DV1 watcher will refuse to start." >&2

mkdir -p "$DEST" "$FAT/linux"
cp "$HERE/scripts/rt4k-serial.py" "$HERE/mister/rt4k-dv1-watch.sh" "$HERE/mister/rt4k-serial.rules" "$DEST/"
chmod +x "$DEST/rt4k-serial.py" "$DEST/rt4k-dv1-watch.sh"
if [ -f "$DEST/rt4k-dv1-map.conf" ]; then
    echo "keeping existing $DEST/rt4k-dv1-map.conf"
else
    cp "$HERE/mister/rt4k-dv1-map.conf" "$DEST/"
fi

if [ -f "$STARTUP" ]; then
    cp "$STARTUP" "$STARTUP.retrothink-bak"
elif [ -f "$FAT/linux/_user-startup.sh" ]; then
    cp "$FAT/linux/_user-startup.sh" "$STARTUP"
else
    printf '#!/bin/sh\n' > "$STARTUP"
fi
strip_all
# udev block goes right after the shebang, ahead of anything that starts Zaparoo.
awk -v blk="$HERE/mister/user-startup.udev.sh" '
    function emit() { while ((getline l < blk) > 0) print l; close(blk); done = 1 }
    NR == 1 && /^#!/ { print; emit(); next }
    !done { emit() }
    { print }
    END { if (!done) emit() }' "$STARTUP" > "$STARTUP.retrothink-tmp"
mv "$STARTUP.retrothink-tmp" "$STARTUP"
cat "$HERE/mister/user-startup.watch.sh" >> "$STARTUP"

echo "installed to $DEST"
echo "user-startup.sh updated (backup: $STARTUP.retrothink-bak)"

# On a real MiSTer, apply both blocks now so no reboot is needed.
if [ -z "${RETROTHINK_FAT:-}" ]; then
    sh "$HERE/mister/user-startup.udev.sh" || true
    stop_watcher
    sh "$HERE/mister/user-startup.watch.sh" || true
    if [ -e /dev/rt4k-serial ]; then
        echo "RT4K found at /dev/rt4k-serial. Try: python3 $DEST/rt4k-serial.py ver"
    else
        echo "RT4K not detected yet -- check it is powered and its USB data leg is in the MiSTer."
    fi
fi
