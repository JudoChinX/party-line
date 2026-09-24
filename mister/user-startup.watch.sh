# >>> retrothink: DV1 fallback watcher >>>
# Load a profile over serial for the cores DV1 cannot switch for (the menu, by
# default). Managed by retrothink's install.sh -- edits here are replaced.
[ -x /media/fat/retrothink/rt4k-dv1-watch.sh ] && (/media/fat/retrothink/rt4k-dv1-watch.sh >/dev/null 2>&1 &)
# <<< retrothink: DV1 fallback watcher <<<
