#!/usr/bin/env python3
"""Talk to the RetroTINK 4K over its USB serial link.

The link is a USB cable from a MiSTer port to the RT4K (through a power/data
splitter), so the tty lives on the MiSTer. This script runs there. From the
workstation it re-executes itself on the MiSTer over ssh (`python3 -` with the
file on stdin), so nothing has to be deployed to the card.

    scripts/rt4k-serial.py status                 # ver, baud, banner, osd2 state
    scripts/rt4k-serial.py ver
    scripts/rt4k-serial.py remote menu            # any documented command
    scripts/rt4k-serial.py "SVS NEW INPUT=5"      # spaces are fine, quoted or not
    scripts/rt4k-serial.py osd                    # print the OSD as text
    scripts/rt4k-serial.py get /profile/DV1/MENU.rt4 > MENU.rt4
    scripts/rt4k-serial.py put MENU.rt4 /profile/DV1/MENU.rt4
    scripts/rt4k-serial.py ls /profile/DV1        # ent t=F sz=23004 mt=... nm=...
    scripts/rt4k-serial.py stat /profile/DV1/MENU.rt4
    scripts/rt4k-serial.py prof get               # loaded profile, relative to /profile
    scripts/rt4k-serial.py prof load DV1/MENU.rt4 # load a profile BY NAME
    scripts/rt4k-serial.py rm /some/file          # exists; be careful

Protocol contract (from PIPe's serial-remote.js, verified on firmware 1.75.0):
2,000,000 baud 8N1, hardware flow control, commands end in "\\r", replies are
lines prefixed "[COM] ". Hold ONE descriptor open for the whole session and do
not toggle DTR between commands. The RT4K echoes unknown input back as
"Bad Command: <text>", which is the best link diagnostic there is.

Binary transfers (osd, osd2, font, get) use RTL1 frames:
    A5 5A | nonce lo hi | len lo hi | type | seq | payload | crc16 lo hi
CRC-16/CCITT (poly 0x1021, init 0xFFFF) over nonce..payload. Data frames carry
the bytes; a type-2 response frame carries the SHA-256 of the whole transfer.
Transfers run at 1,000,000 baud, negotiated with `baud <n>` / `baud ok` and
switched back afterwards: at 2 Mbaud the FT232R inside the RT4K overruns its
256-byte receive FIFO on every 2 KB frame (measured with TIOCGICOUNT), because
the RT4K does not honour RTS/CTS and the MiSTer's USB host cannot drain it fast
enough. At 1 Mbaud there are zero overruns. Text commands are fine at 2 Mbaud.

Never opens /dev/ttyUSB* blindly: it resolves the RT4K's FT232R (0403:6001)
through sysfs. On a MiSTer running Zaparoo, an NFC reader is often a ttyUSB as
well, and Zaparoo owns it. See docs/zaparoo.md for the auto-detect hazard.

Run from a workstation, set RT4K_HOST=root@<your-mister> first.
"""
import binascii
import hashlib
import os
import re
import select
import shlex
import stat
import sys
import termios
import time

HOST = os.environ.get("RT4K_HOST", "")
RT4K_VID_PID = ("0403", "6001")
RENAMED_NODE = "/dev/rt4k-serial"
# One tty, several would-be writers: this CLI, rt4k-dv1-watch.sh (which shells
# out to this CLI on every core switch) and any long-running sweep. Two
# readers on one port interleave -- measured 2026-09-08, the watcher's reply was
# swallowed by a concurrent reader and it logged a FAILED that had not happened.
# Text commands survive that. A BINARY TRANSFER DOES NOT, and the 2026-09-19
# firmware 1.82.1 flash pushed a 4.6 MB bitstream over a 52 s window during which any
# Zaparoo tag scan would have switched cores and fired the watcher straight into
# the same tty. Nothing prevented it; the flash was clean by luck. So the lock
# lives here, in the one module every serial user goes through, rather than only
# in the sweep tool. Directory + pid file because mkdir is atomic everywhere.
LOCK = os.environ.get("RT4K_SERIAL_LOCK", "/tmp/rt4k-serial.lock")
LOCK_WAIT_S = float(os.environ.get("RT4K_LOCK_WAIT", "90"))
RUN_BAUD = 2000000       # firmware >= 1.75.0 default on USB
TRANSFER_BAUD = 1000000  # highest rate with zero RX overruns on a DE10-Nano (measured)
BAUDS = {2000000: termios.B2000000, 1000000: termios.B1000000, 500000: termios.B500000}
GET_READY_RE = r"^get ready off=(?P<off>\d+) len=(?P<len>\d+) total=(?P<total>\d+) nonce=0x(?P<nonce>[0-9a-f]+)"
BAUD_SWITCH_RE = re.compile(r"^baud switching to (\d+)", re.I)
BAUD_CONFIRM_RE = re.compile(r"^baud confirmed (\d+)", re.I)
BAUD_REVERT_S = 5.3      # firmware: 'send baud ok within 5000 ms', plus margin
REPLY_FIRST_S = 0.6      # PIPe: usbBaudDetectionTimeoutMs
REPLY_IDLE_S = 0.18      # PIPe: command idle timer
PACING_S = 0.03          # PIPe: remoteCommandIntervalMs
BINARY_IDLE_S = 7.0      # PIPe: binarySessionIdleTimeoutMs
RTL1_MAX_PAYLOAD = 2048
RTL1_NAK = {1: "CRC", 2: "NONCE", 3: "SEQ", 4: "LEN", 5: "BUSY", 6: "STATE"}


# --- pure helpers (unit tested) -------------------------------------------

def crc16(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE, which is exactly binascii.crc_hqx.

    This was a pure-Python per-byte loop, and that loop is the whole reason the
    2026-09-06 "put has a size cap" conclusion happened: it cost 4.34 s for
    819,200 B on the DE10-Nano's ARM, spent inside the window the firmware waits
    in. crc_hqx is the same function in C -- verified equal on empty, 1 B,
    2048 B, 2054 B and random buffers, and on the CCITT check value -- at roughly
    120x the speed on that ARM (12 ms -> 0.1 ms per 2 KB frame).

    The tests pin it to the published CCITT check value.
    """
    return binascii.crc_hqx(bytes(data), crc)


def encode_frame(nonce, ftype, seq, payload=b""):
    if len(payload) > RTL1_MAX_PAYLOAD:
        raise ValueError("a %d-byte payload does not fit in one frame (limit %d)" % (len(payload), RTL1_MAX_PAYLOAD))
    body = bytes([nonce & 0xFF, nonce >> 8 & 0xFF, len(payload) & 0xFF,
                  len(payload) >> 8 & 0xFF, ftype & 0xFF, seq & 0xFF]) + bytes(payload)
    crc = crc16(body)
    return b"\xa5\x5a" + body + bytes([crc & 0xFF, crc >> 8])


class Rtl1Decoder:
    """Byte-at-a-time RTL1 frame decoder. feed() returns completed frames."""

    def __init__(self):
        self.buf = b""

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            start = self.buf.find(b"\xa5\x5a")
            if start < 0:
                self.buf = self.buf[-1:] if self.buf.endswith(b"\xa5") else b""
                return frames
            if start:
                self.buf = self.buf[start:]
            if len(self.buf) < 8:
                return frames
            nonce = self.buf[2] | self.buf[3] << 8
            length = self.buf[4] | self.buf[5] << 8
            if length > RTL1_MAX_PAYLOAD:
                self.buf = self.buf[2:]
                continue
            end = 8 + length + 2
            if len(self.buf) < end:
                return frames
            payload = self.buf[8:8 + length]
            got = self.buf[end - 2] | self.buf[end - 1] << 8
            frames.append({
                "nonce": nonce, "type": self.buf[6], "seq": self.buf[7],
                "payload": payload, "crc_ok": got == crc16(self.buf[2:8 + length]),
            })
            self.buf = self.buf[end:]


def frame_fault(frame, nonce, expected):
    """What is wrong with one download frame, as (message, abort); None if it is sound.

    abort is True when the RT4K should be told to stop streaming: a damaged or
    missing frame means the rest of the stream is useless.
    """
    fault = None
    if not frame["crc_ok"] or frame["seq"] != expected & 0xFF:
        fault = ("frame %d arrived damaged or out of order" % expected, True)
    elif frame["nonce"] != nonce:
        fault = ("frame %d belongs to a different transfer" % expected, False)
    elif frame["type"] == 5:
        reason = RTL1_NAK.get(frame["payload"][0], "unknown") if frame["payload"] else "unknown"
        fault = ("the RT4K refused the transfer (%s)" % reason, False)
    elif frame["type"] not in (2, 3):
        fault = ("frame %d has type %s, which a download never sends" % (expected, frame["type"]), False)
    return fault


def iter_put_frames(nonce, data):
    """The frame sequence that uploads `data`: 2 KB data frames, then an empty one.

    The empty data frame is what makes the firmware close and verify the file
    ('put done'). A response frame or a ping instead of it wedges the parser.

    A GENERATOR on purpose. crc16() is a pure-Python per-byte loop, so building
    every frame up front costs, on the DE10-Nano's ARM: 4.34 s for 819,200 B,
    4.57 s for 866,848 B, 21.71 s for a 4.6 MB firmware image (measured
    2026-09-08). That work used to happen AFTER `put ready`, inside the window
    the firmware waits in -- so past ~4.5 s of it the firmware gave up before
    frame 0 ever went out and answered `put timeout`. Because the cost is linear
    in file size, that read exactly like a size cap, and was written up as one
    ("the 4.6 MB firmware image cannot be uploaded over serial", 2026-09-06).
    It is not a cap: generating lazily puts frame 0 on the wire in ~10 ms and
    hides each later frame behind the transmission of the one before it, and
    4,610,184 B then uploads in 88 s at 1 Mbaud.
    """
    seq = 0
    for off in range(0, len(data), RTL1_MAX_PAYLOAD):
        yield encode_frame(nonce, 3, seq, data[off:off + RTL1_MAX_PAYLOAD])
        seq += 1
    yield encode_frame(nonce, 3, seq, b"")


def put_frames(nonce, data):
    """iter_put_frames() as a list. Fine for profile-sized files; see the note there."""
    return list(iter_put_frames(nonce, data))


def put_frame_count(size):
    full, rem = divmod(size, RTL1_MAX_PAYLOAD)
    return full + (1 if rem else 0) + 1


def decide(card_md5, repo_md5, base_md5):
    """Three-way rule for one path, identical to scripts/rt4k/reconcile.py.

    card_md5 is None when the card has no such file. Only the repo-driven
    outcomes are relevant to a deploy: push, push-new, in-sync, and the three
    that must NOT be written over (adopt, conflict, deleted-on-card).
    """
    if card_md5 is None:
        return "deleted-on-card" if base_md5 is not None else "push-new"
    if card_md5 == repo_md5:
        return "in-sync"
    if base_md5 is None:
        return "conflict"
    if repo_md5 == base_md5:
        return "adopt"
    if card_md5 == base_md5:
        return "push"
    return "conflict"


def read_plan(path):
    """plan.tsv: card-relative path, repo md5, baseline md5 ('-' if none)."""
    plan = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            rel, repo_md5, base_md5 = line.split("\t")
            plan.append((rel, repo_md5, None if base_md5 == "-" else base_md5))
    return plan


def split_lines(buf):
    """Split a raw receive buffer into complete lines and the unterminated rest."""
    lines = []
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        line = line.replace(b"\r", b"")
        if line:
            lines.append(line.decode("latin-1"))
    return lines, buf


def strip_prefix(line):
    """'[COM] x' -> ('device', 'x'); '[MCU] x' -> ('mcu', 'x'); else ('system', line)."""
    for prefix, kind in (("[COM] ", "device"), ("[MCU] ", "mcu")):
        if line.startswith(prefix):
            return kind, line[len(prefix):]
    return "system", line


def osd_text(cells, rows, width, stride):
    """Render the text half of an OSD plane dump as printable rows."""
    out = []
    for row in range(rows):
        chunk = cells[row * stride: row * stride + width]
        out.append("".join(chr(b) if 32 <= b < 127 else ("." if b else " ") for b in chunk).rstrip())
    return out


def find_rt4k_tty(sys_tty="/sys/class/tty", dev="/dev", node_is=None):
    """Locate the RT4K's tty by USB vid:pid. Returns (path, warning) or (None, None).

    Prefers /dev/rt4k-serial (the udev rename that keeps Zaparoo's auto-detect off
    it) when that node has the right major:minor; otherwise the kernel name, with
    a warning because Zaparoo will be stomping the line settings once a second.
    """
    node_is = node_is or _node_is
    for name in sorted(os.listdir(sys_tty)):
        if not name.startswith(("ttyUSB", "ttyACM")):
            continue
        usb = os.path.join(sys_tty, name, "device", "..", "..")
        try:
            vid = open(os.path.join(usb, "idVendor")).read().strip()
            pid = open(os.path.join(usb, "idProduct")).read().strip()
            major, minor = (int(x) for x in open(os.path.join(sys_tty, name, "dev")).read().split(":"))
        except (OSError, ValueError):
            continue
        if (vid, pid) != RT4K_VID_PID:
            continue
        renamed = os.path.join(dev, os.path.basename(RENAMED_NODE))
        if node_is(renamed, major, minor):
            return renamed, None
        kernel = os.path.join(dev, name)
        if node_is(kernel, major, minor):
            return kernel, ("%s is still the kernel name -- the rt4k-serial udev rule is not active, "
                            "so Zaparoo's PN532 auto-detect will re-set this port to 115200 once a "
                            "second and replies will be corrupt. See docs/zaparoo.md." % kernel)
    return None, None


def _node_is(path, major, minor):
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISCHR(st.st_mode) and os.major(st.st_rdev) == major and os.minor(st.st_rdev) == minor


# --- the serial session -----------------------------------------------------

class Session:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.buf = b""
        self.baud = None
        # A run that died mid-transfer leaves the RT4K below 2 Mbaud until it is
        # power-cycled, so probe the ladder instead of assuming.
        for rate in (RUN_BAUD, TRANSFER_BAUD, 500000):
            self.set_baud(rate)
            time.sleep(0.12)                   # PIPe: usbInitialSettleDelayMs
            os.write(self.fd, b"\r\n")         # PIPe: resync, then 20 ms
            time.sleep(0.02)
            self._drain(0.1)
            if any("FW Version" in r for r in self.command("ver", until=lambda l: "Build tag" in l or "Bad Command" in l)):
                self.baud = rate
                break
        if self.baud is None:
            os.close(self.fd)
            raise RuntimeError("no 'ver' reply at 2M/1M/500k baud -- RT4K off, asleep, or link down")
        if self.baud != RUN_BAUD:
            print("note: RT4K was at %d baud (a transfer died?); restoring %d" % (self.baud, RUN_BAUD), file=sys.stderr)
            self.negotiate(RUN_BAUD)

    def close(self):
        os.close(self.fd)

    def set_baud(self, rate):
        attrs = termios.tcgetattr(self.fd)
        cc = attrs[6]
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        cflag = termios.CS8 | termios.CLOCAL | termios.CREAD | termios.CRTSCTS  # no HUPCL
        termios.tcsetattr(self.fd, termios.TCSANOW, [0, 0, cflag, 0, BAUDS[rate], BAUDS[rate], cc])
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def negotiate(self, rate):
        """Move both ends to `rate` with the firmware's switch/confirm handshake."""
        if rate == self.baud:
            return
        replies = self.command("baud %d" % rate, until=lambda l: l.startswith(("baud", "Bad Command")))
        m = BAUD_SWITCH_RE.match(replies[-1] if replies else "")
        if not m or int(m.group(1)) != rate:
            raise RuntimeError("baud switch to %d refused: %r" % (rate, replies))
        previous = self.baud
        self.set_baud(rate)
        time.sleep(0.08)                       # PIPe: reconfigure settle
        replies = self.command("baud ok", until=lambda l: l.startswith(("baud", "no baud", "Bad Command")))
        m = BAUD_CONFIRM_RE.match(replies[-1] if replies else "")
        if not m or int(m.group(1)) != rate:
            time.sleep(BAUD_REVERT_S)          # firmware reverts on its own
            self.set_baud(previous)
            raise RuntimeError("baud switch to %d not confirmed: %r -- reverted to %d" % (rate, replies, previous))
        self.baud = rate

    def transfer(self, fn, *args, **kwargs):
        """fn(*args) bracketed by the drop to TRANSFER_BAUD and the climb back."""
        self.negotiate(TRANSFER_BAUD)
        try:
            return fn(*args, **kwargs)
        except Exception:
            self._drain(2.0)                   # let an aborted stream run out before talking again
            raise
        finally:
            try:
                self.negotiate(RUN_BAUD)
            except RuntimeError as e:          # never mask the transfer's own error
                print("WARNING: %s" % e, file=sys.stderr)

    def _write_all(self, buf, timeout=10.0):
        """Write every byte, waiting only when the kernel buffer is actually full.

        The fd is O_NONBLOCK, so os.write() returns EAGAIN (and can short-write)
        once the buffer fills. tcdrain() after every frame used to hide that by
        emptying the buffer each time -- at the cost of a third of the
        throughput. Selecting for writability instead blocks exactly as long as
        CTS# backpressure requires and no longer.
        """
        while buf:
            try:
                n = os.write(self.fd, buf)
            except BlockingIOError:
                n = 0
            buf = buf[n:]
            if buf and not select.select([], [self.fd], [], timeout)[1]:
                raise RuntimeError("serial write blocked for %.0f s" % timeout)

    def upload(self, path, data, pace_s=0.0):
        """put <size> <sha256> <path>, then the data frames. Returns the 'put done' line."""
        replies = self.command("put %d %s %s" % (len(data), hashlib.sha256(data).hexdigest(), path),
                               until=lambda l: l.startswith("put"))
        m = re.match(r"^put ready nonce=0x(?P<nonce>[0-9a-f]+)", replies[-1] if replies else "", re.I)
        if not m:
            raise RuntimeError("put %s: %s" % (path, replies[-1] if replies else "no reply"))
        nonce = int(m.group("nonce"), 16)
        total = put_frame_count(len(data))
        # Generated as they are sent -- see iter_put_frames(); building the list
        # first is what used to make large files look size-capped.
        for f in iter_put_frames(nonce, data):
            self._write_all(f)
            # No tcdrain per frame. Measured 2026-09-09 on an 867 KB upload:
            # draining every frame gives 49 KB/s at 1 Mbaud, not draining gives
            # 73 KB/s, and the RT4K itself caps at ~74 KB/s (~28 ms per 2 KB
            # frame). CRTSCTS is what actually paces the link -- with it OFF the
            # unit's own ring_drop/queue_drop counters climb and frames go
            # missing even at 1 Mbaud, so flow control is doing the work tcdrain
            # was being asked to do. 2 Mbaud is NOT worth it: 74 KB/s either way,
            # and reads overrun there.
            if pace_s:
                time.sleep(pace_s)
        # The firmware hashes the whole file before answering, so the wait has to
        # scale: 10 s is right for a 23 KB profile and far too short for 4.6 MB.
        settle = max(10.0, len(data) / 100000.0)
        replies = self.command("", first=settle, idle=0.3, until=lambda l: l.startswith("put"))
        if not replies or not replies[-1].startswith("put done"):
            os.write(self.fd, encode_frame(nonce, 6, total))
            raise RuntimeError("put %s: %s" % (path, replies[-1] if replies
                                               else "no 'put done' within %.0f s" % settle))
        return replies[-1]

    def fetch(self, path):
        """get -- path -> bytes, or None if the card has no such file."""
        try:
            ready, data, ok = self.download("get -- " + path, GET_READY_RE)
        except RuntimeError as e:
            if "cannot open" in str(e):
                return None
            raise
        if not ok:
            raise RuntimeError("get %s: SHA-256 mismatch" % path)
        return data

    def deploy(self, workdir):
        """One-session push of workdir/files/* per workdir/plan.tsv, with the
        three-way check first and a read-back verify after. Writes
        workdir/result.tsv and keeps every overwritten card copy in workdir/before/.
        """
        plan = read_plan(os.path.join(workdir, "plan.tsv"))
        out = open(os.path.join(workdir, "result.tsv"), "a", encoding="utf-8")
        counts = {}
        errors_in_a_row = 0
        for i, (rel, repo_md5, base_md5) in enumerate(plan, 1):
            path = "/" + rel
            status = ""
            for attempt in (1, 2):             # one retry: a bad-CRC get is a transient, not a verdict
                try:
                    before = self.fetch(path)
                    card_md5 = hashlib.md5(before).hexdigest() if before is not None else None
                    action = decide(card_md5, repo_md5, base_md5)
                    if action in ("push", "push-new"):
                        if before is not None:
                            dest = os.path.join(workdir, "before", rel)
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            with open(dest, "wb") as fh:
                                fh.write(before)
                        data = open(os.path.join(workdir, "files", rel), "rb").read()
                        self.upload(path, data)
                        after = self.fetch(path)
                        after_md5 = hashlib.md5(after).hexdigest() if after is not None else None
                        status = "verified" if after_md5 == repo_md5 else "VERIFY-FAILED"
                    errors_in_a_row = 0
                    break
                except RuntimeError as e:
                    action, card_md5, status = "error", None, str(e).replace("\t", " ")[:200]
                    self._drain(3.0)           # let a half-finished stream run out before retrying
                    if attempt == 1:
                        continue
                    errors_in_a_row += 1
                if errors_in_a_row >= 3:
                    out.write("%s\t%s\t%s\t%s\n" % (rel, action, card_md5 or "-", status))
                    out.flush()
                    print("STOPPING after 3 consecutive errors", flush=True)
                    break
            counts[action] = counts.get(action, 0) + 1
            out.write("%s\t%s\t%s\t%s\n" % (rel, action, card_md5 or "-", status))
            out.flush()
            if i % 25 == 0 or i == len(plan):
                print("%d/%d %s" % (i, len(plan), " ".join("%s=%d" % kv for kv in sorted(counts.items()))), flush=True)
        out.close()
        return counts

    def listing(self, path):
        """ls <path> at transfer baud (3,000-entry directories overrun at 2 M)."""
        lines = self.command("ls " + path if path else "ls", first=3.0, idle=2.0,
                             until=lambda l: l.startswith(("ls end", "ls err", "Bad Command")))
        if not lines or not lines[-1].startswith("ls end"):
            raise RuntimeError("ls %s: %s" % (path, lines[-1] if lines else "no reply"))
        return lines[:-1]

    def _read(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            chunk = os.read(self.fd, 65536)
        except BlockingIOError:
            return b""
        if os.environ.get("RT4K_RAW"):        # debugging aid: append every received byte
            with open(os.environ["RT4K_RAW"], "ab") as raw:
                raw.write(chunk)
        return chunk

    def _drain(self, timeout):
        """Read and discard everything for `timeout` seconds, unterminated tail included.

        Keeping a partial line here was a bug: after an aborted binary transfer
        the leftover frame bytes sat in the buffer and were parsed as the reply
        to the next command ('Bad Command: \\xa5Z..').
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._read(deadline - time.time())
        self.buf = b""

    def command(self, line, until=None, first=REPLY_FIRST_S, idle=REPLY_IDLE_S):
        """Send one line; return reply lines (prefix stripped). until(line) ends early.

        An empty `line` sends nothing and just collects replies.
        """
        if line:
            os.write(self.fd, line.encode("latin-1") + b"\r")
        replies = []
        deadline = time.time() + first
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            chunk = self._read(remaining)
            if not chunk:
                continue
            self.buf += chunk
            lines, self.buf = split_lines(self.buf)
            for raw in lines:
                _, text = strip_prefix(raw)
                replies.append(text)
                deadline = time.time() + idle
                if until and until(text):
                    time.sleep(PACING_S)
                    return replies
        time.sleep(PACING_S)
        return replies

    def download(self, wire, ready_re, error_re=r"err=|\berror\b|cannot open|nothing shown|bad command"):
        """Run a non-acknowledged RTL1 transfer. Returns (ready_match, bytes, sha_ok)."""
        os.write(self.fd, wire.encode("latin-1") + b"\r")
        ready = None
        deadline = time.time() + 2.0
        while ready is None:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RuntimeError("%s: no 'ready' line" % wire)
            chunk = self._read(remaining)
            if not chunk:
                continue
            self.buf += chunk
            while b"\n" in self.buf and ready is None:
                raw, self.buf = self.buf.split(b"\n", 1)
                _, text = strip_prefix(raw.replace(b"\r", b"").decode("latin-1"))
                if not text:
                    continue
                m = re.match(ready_re, text, re.I)
                if m:
                    ready = m
                elif re.search(error_re, text, re.I):
                    raise RuntimeError("%s: %s" % (wire, text))
        nonce = int(ready.group("nonce"), 16)
        # Read the whole stream before decoding anything. Decoding while frames
        # are still arriving let the 4 KB line-discipline buffer fill (4 ms at
        # 1 Mbaud), the kernel then throttles the USB reads, and the FT232R's
        # FIFO overruns -- every frame after that fails its CRC.
        stream = self.buf
        self.buf = b""
        done_marker = wire.split()[0].encode() + b" done"
        last = time.time()
        while done_marker not in stream[-64:]:
            chunk = self._read(0.2)
            if chunk:
                stream += chunk
                last = time.time()
            elif time.time() - last > BINARY_IDLE_S:
                os.write(self.fd, encode_frame(nonce, 6, 0))
                raise RuntimeError("%s: binary session timed out after %d bytes" % (wire, len(stream)))
        chunks, digest = [], None
        for seq, f in enumerate(Rtl1Decoder().feed(stream)):
            fault = frame_fault(f, nonce, seq)
            if fault:
                message, abort = fault
                if abort:
                    os.write(self.fd, encode_frame(nonce, 6, seq))
                raise RuntimeError("%s: %s" % (wire, message))
            if f["type"] == 3:
                chunks.append(f["payload"])
            else:
                digest = bytes(f["payload"])
        if digest is None:
            raise RuntimeError("%s: stream ended without a response frame (%d bytes)" % (wire, len(stream)))
        self.buf = b""                        # decoder.buf holds only the trailing "<cmd> done" line
        self._drain(0.1)
        data = b"".join(chunks)
        return ready, data, hashlib.sha256(data).digest() == digest


def lock_holder(lock=None):
    """PID currently holding `lock`, or None if free or the holder is gone."""
    lock = LOCK if lock is None else lock
    try:
        with open(os.path.join(lock, "pid")) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    # A lock whose owner died is stale, not held. The MiSTer reboots mid-sweep
    # often enough that a sticky lock would be worse than no lock at all.
    return pid if os.path.exists("/proc/%d" % pid) else None


def take_lock(wait_s=None, lock=None, _sleep=time.sleep, _now=time.monotonic):
    """Claim the serial port, waiting up to `wait_s`. True if claimed.

    Waits rather than failing outright: a firmware `put` holds the port for the
    better part of a minute, and a watcher that simply gave up would leave the
    wrong profile on screen. Bounded, because a full sweep of every core can hold
    it for over an hour and nothing should block that long.
    """
    lock = LOCK if lock is None else lock
    wait_s = LOCK_WAIT_S if wait_s is None else wait_s
    if os.environ.get("RT4K_NO_LOCK") == "1":
        return True
    deadline = _now() + wait_s
    while True:
        try:
            os.mkdir(lock)
        except FileExistsError:
            if lock_holder(lock) is None:
                # Stale: the holder is gone. Clear it and retry immediately.
                try:
                    os.remove(os.path.join(lock, "pid"))
                except OSError:
                    pass
                try:
                    os.rmdir(lock)
                except OSError:
                    pass
                continue
            if _now() >= deadline:
                return False
            _sleep(0.25)
            continue
        except OSError:
            return False
        with open(os.path.join(lock, "pid"), "w") as fh:
            fh.write(str(os.getpid()))
        return True


def drop_lock(lock=None):
    """Release the port. Only removes a lock this process owns."""
    lock = LOCK if lock is None else lock
    try:
        with open(os.path.join(lock, "pid")) as fh:
            if int(fh.read().strip()) != os.getpid():
                return          # someone else's lock -- never steal on exit
    except (OSError, ValueError):
        return
    try:
        os.remove(os.path.join(lock, "pid"))
    except OSError:
        pass
    try:
        os.rmdir(lock)
    except OSError:
        pass


# --- CLI ----------------------------------------------------------------------

def on_mister():
    return os.path.exists("/media/fat/MiSTer") or os.environ.get("RT4K_LOCAL") == "1"


def run_remote(argv):
    if not HOST:
        sys.exit("not on a MiSTer and RT4K_HOST is unset -- export RT4K_HOST=root@<your-mister> "
                 "(or run this on the MiSTer itself)")
    if argv[0] == "put" and len(argv) == 3:
        # The upload source is a workstation file; stage it on the MiSTer's tmpfs.
        staged = "/tmp/rt4k-put-" + os.path.basename(argv[1])
        if os.system("scp -q %s %s" % (shlex.quote(argv[1]), shlex.quote(HOST + ":" + staged))) != 0:
            sys.exit(2)
        argv = ["put", staged, argv[2]]
    src = open(os.path.abspath(__file__), "rb")
    os.dup2(src.fileno(), 0)
    env = " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in os.environ.items()
                   if k.startswith("RT4K_") and k != "RT4K_HOST")
    remote = (env + " " if env else "") + "python3 - " + " ".join(shlex.quote(a) for a in argv)
    os.execvp("ssh", ["ssh", "-o", "BatchMode=yes", HOST, remote])


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if not on_mister():
        run_remote(argv)
    path = os.environ.get("RT4K_DEV")
    if not path:
        path, warning = find_rt4k_tty()
        if warning:
            print("WARNING: " + warning, file=sys.stderr)
    if not path:
        print("RT4K FT232R (0403:6001) not found in /sys/class/tty -- is the RT4K powered and the "
              "splitter's data leg plugged into the MiSTer?", file=sys.stderr)
        return 2
    if not take_lock():
        print("another tool holds %s (pid %s) -- refusing to share the serial port; "
              "two readers on one tty interleave and corrupt binary transfers"
              % (LOCK, lock_holder()), file=sys.stderr)
        return 2
    try:
        try:
            s = Session(path)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 2
        try:
            return dispatch(s, argv)
        finally:
            s.close()
    finally:
        drop_lock()


def dispatch(s, argv):
    cmd, args = argv[0], argv[1:]
    if cmd == "status":
        for line in ("ver", "baud", "banner", "osd2 state"):
            for reply in s.command(line) or ["(no reply)"]:
                print(reply)
        return 0
    if cmd in ("osd", "osd2"):
        ready_re = (r"^osd ready rows=(?P<rows>\d+) stride=(?P<stride>\d+) width=(?P<width>\d+) cells=(?P<cells>\d+) nonce=0x(?P<nonce>[0-9a-f]+)"
                    if cmd == "osd" else
                    r"^osd2 ready on=(?P<on>\d) osk=(?P<osk>\d) rows=(?P<rows>\d+) cols=(?P<width>\d+) stride=(?P<stride>\d+) cells=(?P<cells>\d+) nonce=0x(?P<nonce>[0-9a-f]+)")
        try:
            ready, data, ok = s.transfer(s.download, cmd, ready_re)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1 if "nothing shown" not in str(e) else 3
        cells = int(ready.group("cells"))
        rows, width, stride = int(ready.group("rows")), int(ready.group("width")), int(ready.group("stride"))
        colors = data[cells:]
        for i, row in enumerate(osd_text(data[:cells], rows, width, stride)):
            if "--colors" in args:                # colour byte + glyph byte of the first cells: find the cursor row
                head = data[i * stride: i * stride + 3]
                print("%02x %s | %s" % (colors[i * stride] if i * stride < len(colors) else 0, head.hex(), row))
            else:
                print(row)
        if not ok:
            print("%s: SHA-256 mismatch" % cmd, file=sys.stderr)
            return 1
        return 0
    if cmd == "get":
        if len(args) != 1:
            print("usage: get /path/on/rt4k/card", file=sys.stderr)
            return 2
        try:
            ready, data, ok = s.transfer(s.download, "get -- " + args[0], GET_READY_RE)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        if not ok:
            print("get: SHA-256 mismatch after %d bytes" % len(data), file=sys.stderr)
            return 1
        if ready.group("len") != ready.group("total"):
            print("get: device sent %s of %s bytes" % (ready.group("len"), ready.group("total")), file=sys.stderr)
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return 0
    if cmd == "put":
        if len(args) != 2:
            print("usage: put LOCALFILE /path/on/rt4k/card", file=sys.stderr)
            return 2
        data = open(args[0], "rb").read()
        try:
            print(s.transfer(s.upload, args[1], data, float(os.environ.get("RT4K_PUT_PACE", "0"))))
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        return 0
    if cmd == "ls":
        try:
            for line in s.transfer(s.listing, " ".join(args)):
                print(line)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        return 0
    if cmd == "deploy":
        if len(args) != 1 or not os.path.isfile(os.path.join(args[0], "plan.tsv")):
            print("usage: deploy WORKDIR   (WORKDIR/plan.tsv + WORKDIR/files/; see rt4k-serial-deploy.py)", file=sys.stderr)
            return 2
        counts = s.transfer(s.deploy, args[0])
        print("DONE " + " ".join("%s=%d" % kv for kv in sorted(counts.items())), flush=True)
        return 0 if not counts.get("error") and not any(k == "VERIFY-FAILED" for k in counts) else 1
    line = " ".join(argv)
    replies = s.command(line)
    if not replies:
        print("%s: no reply (SVS commands are silent; anything else means the link is down)" % line, file=sys.stderr)
        return 1 if not line.startswith("SVS ") else 0
    for reply in replies:
        print(reply)
    return 1 if any(r.startswith("Bad Command") for r in replies) and not line.startswith("pwr ") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
