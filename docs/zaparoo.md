# Zaparoo and the RT4K's serial port

**Symptom:** the RT4K answers the first command after you open the port, then
goes silent or answers `Bad Command: U`, or `Bad Command:` with nothing after it.
Re-running `stty` sometimes revives it for one exchange. This looks exactly
like a bad cable or a weak power supply. It is neither.

**Cause:** with `auto_detect = true` (the default, and what makes tag scanning
work), Zaparoo lists `/dev/ttyUSB*` and `/dev/ttyACM*` **once a second**. Each
port it does not already hold, and that is not on a short hard-coded vid:pid
skip list, gets probed for a PN532 NFC reader:

1. open it
2. `tcsetattr` to **115200 8N1, no flow control**
3. write the PN532 wake-up frame (`55 55 00 00 …`)
4. read for about 400 ms
5. close it

termios settings belong to the tty, not the descriptor, so the probe
re-programs the RT4K's FT232R to 115200 **under your open descriptor**. That
produces every symptom above:

- Anything sent after the probe goes out at the wrong rate.
- `U` is `0x55`, the wake-up preamble, arriving in the RT4K's command parser.
- The probe's close can be the port's last close, which drops DTR.

Measured with Zaparoo 2.17.2 by polling `tcgetattr` and `/proc/*/fd` at 50 ms
while holding the port open.

**Fix:** take the node out of Zaparoo's name search. `mister/rt4k-serial.rules`
renames the RT4K's tty to `/dev/rt4k-serial`. With no other change, the probe
went from 0 of 40 good exchanges to 30 of 30.

- udev can't `NAME=` a tty, so the rule is
  `RUN+="/bin/mv /dev/%k /dev/rt4k-serial"`. It matches `0403:6001` only.
- The MiSTer's `/` is read-only. The rule therefore lives on the SD card, and
  `user-startup.sh` copies it into `/run/udev/rules.d/` (tmpfs), reloads udev
  and re-triggers the FT232R at every boot. This happens **before** Zaparoo
  starts. `install.sh` sets that up.
- `rt4k-serial.py` finds the RT4K by vid:pid through sysfs. If it finds the
  kernel name instead of `/dev/rt4k-serial`, it prints a warning that the rule
  is not active. That warning is the first thing to check if the link goes
  flaky.

**Why not pin the reader and turn auto-detect off?** It would work, but it
fails the wrong way round. If the pin ever misses, tag scanning dies, and on
many setups tag scanning is the main way games are launched. If the rename
misses, the RT4K link gets flaky and the tool says so.

**The same applies to any other USB serial device** on a Zaparoo MiSTer. Hide
it from the name scan, or expect it to be reset to 115200 once a second.

**Upstream:** newer Zaparoo remembers ports that failed a probe, but the cache
is keyed on the device node's mtime, and writing to a tty bumps its mtime.
Every command sent to the RT4K would re-arm the probe, so the rename is still
needed.
