#!/usr/bin/env bash
# Device-free tests for install.sh, against a fake /media/fat.
# Not picked up by `python3 -m unittest discover` -- run by hand, see README.md.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fails=0
check() {  # check <label> <expected> <actual>
    if [ "$2" = "$3" ]; then echo "ok   - $1"
    else echo "FAIL - $1"; echo "       expected: [$2]"; echo "       actual:   [$3]"; fails=$((fails+1)); fi
}
run() { RETROTHINK_FAT="$TMP/fat" sh "$ROOT/install.sh" --local "$@" >/dev/null 2>&1; }

mkdir -p "$TMP/fat/linux"
printf '#!/bin/sh\n# stock\n[[ -e /media/fat/Scripts/zaparoo.sh ]] && /media/fat/Scripts/zaparoo.sh -service $1\n' \
    > "$TMP/fat/linux/user-startup.sh"
cp "$TMP/fat/linux/user-startup.sh" "$TMP/orig"

run
S="$TMP/fat/linux/user-startup.sh"
check "files installed" "rt4k-dv1-map.conf rt4k-dv1-watch.sh rt4k-serial.py rt4k-serial.rules" \
    "$(ls "$TMP/fat/retrothink" | tr '\n' ' ' | sed 's/ $//')"
check "shebang stays first" "#!/bin/sh" "$(head -1 "$S")"
udev=$(grep -n '>>> retrothink: rt4k-serial udev rule' "$S" | cut -d: -f1)
zap=$(grep -n 'zaparoo.sh -service' "$S" | cut -d: -f1)
check "udev block runs before Zaparoo" "yes" "$([ "$udev" -lt "$zap" ] && echo yes || echo no)"
check "watcher block is last" "# <<< retrothink: DV1 fallback watcher <<<" "$(tail -1 "$S")"
check "backup written" "$(cat "$TMP/orig")" "$(cat "$S.retrothink-bak")"

cp "$S" "$TMP/after1"
echo "MENU	DV1/Custom.rt4" > "$TMP/fat/retrothink/rt4k-dv1-map.conf"
run
check "re-install is idempotent" "$(cat "$TMP/after1")" "$(cat "$S")"
check "custom map is kept" "MENU	DV1/Custom.rt4" "$(cat "$TMP/fat/retrothink/rt4k-dv1-map.conf")"

run --uninstall
check "uninstall restores user-startup.sh" "$(cat "$TMP/orig")" "$(cat "$S")"
check "uninstall removes the folder" "no" "$([ -d "$TMP/fat/retrothink" ] && echo yes || echo no)"

rm "$S"
run
check "creates user-startup.sh when absent" "#!/bin/sh" "$(head -1 "$S")"

[ "$fails" -eq 0 ] && echo && echo "all passed" || { echo; echo "$fails failed"; exit 1; }
