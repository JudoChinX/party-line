# >>> retrothink: rt4k-serial udev rule >>>
# Rename the RetroTINK 4K's FT232R tty to /dev/rt4k-serial so Zaparoo's reader
# auto-detect leaves it alone. Must run BEFORE Zaparoo starts, so the installer
# puts this block at the top. The loop covers an RT4K that enumerated before
# this script ran. Managed by retrothink's install.sh -- edits here are replaced.
if [ -f /media/fat/retrothink/rt4k-serial.rules ]; then
    mkdir -p /run/udev/rules.d
    cp /media/fat/retrothink/rt4k-serial.rules /run/udev/rules.d/99-rt4k-serial.rules
    udevadm control --reload
    for t in /sys/class/tty/ttyUSB*; do
        [ -e "$t" ] || continue
        if [ "$(cat "$t/device/../../idVendor" 2>/dev/null)" = "0403" ] && \
           [ "$(cat "$t/device/../../idProduct" 2>/dev/null)" = "6001" ]; then
            udevadm trigger --action=add "$t"
        fi
    done
fi
# <<< retrothink: rt4k-serial udev rule <<<
