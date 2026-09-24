#!/usr/bin/env bash
# Device-free tests for mister/rt4k-dv1-watch.sh.
# Not picked up by `python3 -m unittest discover` -- run by hand, see README.md.
set -u
W="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/mister/rt4k-dv1-watch.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fails=0

check() {  # check <label> <expected> <actual>
    if [ "$2" = "$3" ]; then
        echo "ok   - $1"
    else
        echo "FAIL - $1"; echo "       expected: [$2]"; echo "       actual:   [$3]"; fails=$((fails+1))
    fi
}

# The shipped default has to map MENU, because MENU is the entire point: it is
# the core DV1 cannot match (untagged 1080p).
check "built-in default maps MENU" "DV1/MENU.rt4" \
  "$(RT4K_DV1_MAP=$TMP/absent "$W" --lookup MENU)"

# Anything else must come back empty so DV1 keeps control. A watcher that
# answered for every core would race DV1 and override its correct choice.
check "unmapped core yields nothing" "" \
  "$(RT4K_DV1_MAP=$TMP/absent "$W" --lookup AcornAtom)"

printf 'MENU\t_CRT Emulation/CRT TV and PVM Emulation by Kuro Houou/JVC D-Series-D200 - 4K HDR.rt4\n' > "$TMP/map.conf"
check "profile paths keep their spaces" \
  "_CRT Emulation/CRT TV and PVM Emulation by Kuro Houou/JVC D-Series-D200 - 4K HDR.rt4" \
  "$(RT4K_DV1_MAP=$TMP/map.conf "$W" --lookup MENU)"

printf '# comment\n\n   \nPSX   DV1/PSX.rt4\n' > "$TMP/map2.conf"
check "comments and blank lines ignored; spaces separate" "DV1/PSX.rt4" \
  "$(RT4K_DV1_MAP=$TMP/map2.conf "$W" --lookup PSX)"
check "a map file replaces the default entirely" "" \
  "$(RT4K_DV1_MAP=$TMP/map2.conf "$W" --lookup MENU)"

# Core names contain spaces ("Game and Watch", "Sord M5", "Amstrad PCW"), so the
# separator must be the first TAB, not any whitespace. This regressed once.
printf 'Game and Watch\tDV1/GameAndWatch.rt4\nSord M5\tDV1/SordM5.rt4\n' > "$TMP/spaces.conf"
check "core name with spaces resolves" "DV1/GameAndWatch.rt4" \
  "$(RT4K_DV1_MAP=$TMP/spaces.conf "$W" --lookup "Game and Watch")"
check "and does not match on its first word" "" \
  "$(RT4K_DV1_MAP=$TMP/spaces.conf "$W" --lookup Game)"

# Trailing comments annotate uncertain mappings; they must not reach the path.
printf 'SPMX\tDV1/Specialist.rt4  # best match, unconfirmed\n' > "$TMP/cmt.conf"
check "trailing comment stripped from the path" "DV1/Specialist.rt4" \
  "$(RT4K_DV1_MAP=$TMP/cmt.conf "$W" --lookup SPMX)"

# A path with spaces AND a comment: only the comment goes.
printf 'X\t_CRT Emulation/Kuro Houou/JVC D200.rt4  # note\n' > "$TMP/both.conf"
check "path keeps spaces, loses only the comment" "_CRT Emulation/Kuro Houou/JVC D200.rt4" \
  "$(RT4K_DV1_MAP=$TMP/both.conf "$W" --lookup X)"

# A core name that is a prefix of another must not match it.
printf 'MEN\tDV1/Wrong.rt4\nMENU\tDV1/Right.rt4\n' > "$TMP/map3.conf"
check "exact core-name match, not prefix" "DV1/Right.rt4" \
  "$(RT4K_DV1_MAP=$TMP/map3.conf "$W" --lookup MENU)"

# --once must survive a missing serial tool rather than taking the boot down.
echo MENU > "$TMP/CORENAME"
out=$(RT4K_CORENAME_FILE=$TMP/CORENAME RT4K_DV1_MAP=$TMP/absent \
      RT4K_SERIAL_TOOL=$TMP/nosuch.py RT4K_DV1_LOG=$TMP/log "$W" --once; echo "rc=$?")
check "missing serial tool fails soft" "rc=1" "$out"
grep -q "is missing" "$TMP/log" && echo "ok   - and says why in the log" \
  || { echo "FAIL - log does not explain the failure"; fails=$((fails+1)); }

# An unmapped core is a no-op, not an error -- it is the normal path.
echo AcornAtom > "$TMP/CORENAME"
out=$(RT4K_CORENAME_FILE=$TMP/CORENAME RT4K_DV1_MAP=$TMP/absent \
      RT4K_SERIAL_TOOL=$TMP/nosuch.py RT4K_DV1_LOG=$TMP/log2 "$W" --once; echo "rc=$?")
check "unmapped core is a no-op, exit 0" "rc=0" "$out"

# The serial lock: a live holder must make the watcher defer, a stale one must not.
mkdir -p "$TMP/lock"; echo $$ > "$TMP/lock/pid"        # $$ is alive: this shell
echo MENU > "$TMP/CORENAME"
out=$(RT4K_CORENAME_FILE=$TMP/CORENAME RT4K_DV1_MAP=$TMP/absent RT4K_SERIAL_LOCK=$TMP/lock \
      RT4K_SERIAL_TOOL=$TMP/nosuch.py RT4K_DV1_LOG=$TMP/log3 "$W" --once; echo "rc=$?")
check "defers while the port is held" "rc=0" "$out"
grep -q "deferring" "$TMP/log3" && echo "ok   - and says it deferred" \
  || { echo "FAIL - did not log a deferral"; fails=$((fails+1)); }

mkdir -p "$TMP/stale"; echo 999999 > "$TMP/stale/pid"  # a pid that cannot exist
out=$(RT4K_CORENAME_FILE=$TMP/CORENAME RT4K_DV1_MAP=$TMP/absent RT4K_SERIAL_LOCK=$TMP/stale \
      RT4K_SERIAL_TOOL=$TMP/nosuch.py RT4K_DV1_LOG=$TMP/log4 "$W" --once; echo "rc=$?")
check "stale lock is broken, not obeyed" "rc=1" "$out"
[ -d "$TMP/stale" ] && { echo "FAIL - stale lock not removed"; fails=$((fails+1)); } \
  || echo "ok   - stale lock removed"

echo; [ "$fails" -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
