# retrothink

Control a **RetroTINK 4K** from the **MiSTer** it is scaling, over the RT4K's
USB serial port. You don't need a microcontroller, a Raspberry Pi or any other
extra computer, because the MiSTer is the host.

> Not affiliated with RetroTINK LLC. The RT4K's extended serial interface is
> undocumented upstream ("documentation pending"). Everything here was measured
> on one unit. See [What it has been tested on](#what-it-has-been-tested-on).

## What it does

- **Gives DV1 a fallback.** DV1 picks an RT4K profile from the core name the
  MiSTer tags its video with. The menu core, when run with
  `[menu] direct_video=0` (the usual fix for the menu rendering garbled under
  direct video), sends *untagged* 1080p. The RT4K then keeps whatever profile
  the last game left loaded, and the menu is drawn through it.
  `rt4k-dv1-watch.sh` watches `/tmp/CORENAME` and runs `prof load` on the
  profile you mapped for the menu. It does the same for any other core DV1
  misses on your setup.
- **Fixes the Zaparoo "flaky serial" trap.** Zaparoo's reader auto-detect
  re-programs every `/dev/ttyUSB*` to 115200 once a second. On the RT4K's port
  that looks exactly like a bad cable. A udev rule renames the RT4K to
  `/dev/rt4k-serial`, out of Zaparoo's reach. See [docs/zaparoo.md](docs/zaparoo.md).
- **A serial CLI, `rt4k-serial.py`.** It sends any command, reads the OSD as
  text, and does `get` / `put` / `ls` / `stat` of files on the RT4K's SD card,
  plus `prof load` / `prof get`. It uses only the Python standard library and
  runs on the MiSTer. From a workstation it re-runs itself on the MiSTer over
  ssh, so nothing needs deploying for that.
- **Deploys profiles over serial, with no SD-card pull.**
  `rt4k-serial-deploy.py` pushes a local folder of profiles to the card. It
  classifies every file card-vs-local-vs-last-deploy first, so **a profile you
  tuned on the unit is never overwritten**. Every overwritten copy is kept, and
  every write is read back and hashed.

## Hardware

The RT4K's USB-C port carries both its **power** and its serial link, and the
RT4K needs about 5 V 2 A. A MiSTer USB port can't supply that. So:

- **A USB power/data splitter.** The kind sold for powering a Raspberry Pi
  while using its data port. The RT4K keeps its own 2 A supply on the power
  side, and only D+/D-/GND run to a MiSTer USB port. The ConsoleMods RT4K
  wiki lists this as one of the supported ways to connect.
  **Meter it before it touches the RT4K.** The host's 5 V must *not* reach
  the RT4K side, and the RT4K's jack should read at least 4.75 V under load.
- Nothing else. The MiSTer kernel has the FTDI driver built in, and the RT4K
  appears as an FT232R (`0403:6001`).

## Install

From a workstation that can ssh to the MiSTer as root:

```sh
git clone https://github.com/JudoChinX/retrothink.git && cd retrothink
./install.sh root@<your-mister>
```

That puts everything in `/media/fat/retrothink/` and adds two marked blocks to
`/media/fat/linux/user-startup.sh`: the udev rename at the top, so it runs
before Zaparoo, and the watcher at the bottom. The file is backed up first.
Re-running is safe. `./install.sh --uninstall root@<your-mister>` removes both.
Your `rt4k-dv1-map.conf` is kept across re-installs.

Then check the link:

```sh
export RT4K_HOST=root@<your-mister>
scripts/rt4k-serial.py ver        # RT4KPRO, FW Version: ...
scripts/rt4k-serial.py status
scripts/rt4k-serial.py osd        # the RT4K's on-screen menu, as text
```

The RT4K ships with serial over USB at **2,000,000 baud** (firmware 1.75.0 and
later), and that is what the tool expects.

## The DV1 watcher

Edit `/media/fat/retrothink/rt4k-dv1-map.conf`: one `CORENAME<TAB>profile path`
per line, with the path relative to `/profile` on the RT4K's card. The shipped
map is `MENU → DV1/MENU.rt4` only. **List only cores DV1 genuinely misses.** A
watcher that answered for every core would race DV1 and override its correct
choice. The file explains how to spot a miss.

```sh
/media/fat/retrothink/rt4k-dv1-watch.sh --lookup MENU   # what would load
cat /tmp/rt4k-dv1/watch.log                               # what did
```

It needs `inotifywait`, costs about 1 MB of RAM while idle, and spends about
0.9 s of Python per core switch.

## Deploying profiles

Keep a local folder laid out like the root of the RT4K's card, containing
`profile/...`:

```sh
export RT4K_HOST=root@<your-mister> RT4K_MIRROR=~/rt4k-card
scripts/rt4k-serial-deploy.py launch --dry-run   # what would be sent
scripts/rt4k-serial-deploy.py launch             # runs detached on the MiSTer
scripts/rt4k-serial-deploy.py status
scripts/rt4k-serial-deploy.py collect            # results, backups, baseline
```

A deploy takes about 1 s per 23 KB profile. **On a first deploy there is no
baseline**, so any file already on the card that differs from yours is a
*conflict* and is left alone. Only files the card lacks are written. After
`collect`, `MIRROR/.deployed-manifest.txt` records what was verified, so the
next deploy can tell "you changed it locally" (it is sent) from "someone tuned
it on the unit" (it is left alone).

Paths containing spaces are refused, because the firmware's `get`/`put`
tokenise on spaces.

## Safety

- One process at a time on the port. Every tool takes `/tmp/rt4k-serial.lock`,
  because two readers on one tty interleave and corrupt binary transfers.
- `rt4k-serial.py rm` exists and does what it says.
- If a transfer is interrupted and the RT4K stops answering text commands,
  **wait about a minute**. It times out of binary mode on its own (`put timeout`).
  Don't power-cycle it.
- There is deliberately **no firmware-flashing tool** here yet. It has only been
  exercised on one RT4K Pro.

## What it has been tested on

| | |
|---|---|
| RT4K | **one RT4K Pro**, firmware 1.75.0 through 1.86.0 |
| RT4K CE | **untested** |
| MiSTer | DE10-Nano, MiSTer Linux 5.15, running Zaparoo in place of the stock menu |
| Host link | USB power/data splitter to a MiSTer USB port |

Reports from other setups, especially a CE or a stock-menu MiSTer, are the most
useful contribution right now.

## Tests

```sh
cd scripts && python3 -m unittest discover -s tests -v
for t in scripts/tests/test-*.sh; do bash "$t"; done   # shell tests, device-free
```

## Protocol notes

[docs/protocol.md](docs/protocol.md) covers the command set as measured, the
RTL1 binary framing, and why transfers run at 1 Mbaud.

## Credits

- **PIPe**, whose [RT4K web remote](https://rt4k-remote.pipe.hr/) established
  the USB transport contract (baud, flow control, reply prefixes, timings)
  this client follows.
- **Guspaz** ([rt4k_pi](https://github.com/Guspaz/rt4k_pi)), and **donutswdad**
  ([DonutDongle](https://github.com/svirant/DonutDongle),
  [DonutShop](https://github.com/svirant/DonutShop)), for mapping the RT4K
  serial world first.
- **RetroTINK LLC** for opening the serial interface at all.

This client was written against the protocol those projects speak. The
timing constants marked `PIPe:` in `rt4k-serial.py` come from PIPe's client.
The file-layer commands were found by probing, and everything was re-measured
on hardware.

## License

MIT. See [LICENSE](LICENSE).
