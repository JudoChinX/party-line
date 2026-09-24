"""The RT4K serial tool, device-free parts.

What is worth pinning: the RTL1 frame codec (a wrong CRC or a bad resync
silently corrupts an OSD dump or a file pull), reply-line splitting, OSD text
rendering, and the tty resolver -- which must never hand back the PN532's port.
"""
import importlib.util
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "rt4k_serial", os.path.join(REPO, "scripts", "rt4k-serial.py"))
rt4k_serial = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt4k_serial)


class Crc16Test(unittest.TestCase):
    def test_ccitt_check_value(self):
        # CRC-16/CCITT-FALSE check value for "123456789".
        self.assertEqual(rt4k_serial.crc16(b"123456789"), 0x29B1)

    def test_empty_is_init(self):
        self.assertEqual(rt4k_serial.crc16(b""), 0xFFFF)


class FrameCodecTest(unittest.TestCase):
    def test_roundtrip(self):
        frame = rt4k_serial.encode_frame(0xBEEF, 3, 7, b"hello")
        self.assertEqual(frame[:2], b"\xa5\x5a")
        frames = rt4k_serial.Rtl1Decoder().feed(frame)
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual((f["nonce"], f["type"], f["seq"], bytes(f["payload"]), f["crc_ok"]),
                         (0xBEEF, 3, 7, b"hello", True))

    def test_layout_matches_reference(self):
        # Header: nonce lo/hi, len lo/hi, type, seq; CRC over those + payload, LE.
        frame = rt4k_serial.encode_frame(0x0102, 2, 1, b"\x00" * 32)
        self.assertEqual(frame[2:8], bytes([0x02, 0x01, 0x20, 0x00, 0x02, 0x01]))
        self.assertEqual(len(frame), 10 + 32)

    def test_resyncs_past_garbage_and_split_feeds(self):
        frame = rt4k_serial.encode_frame(1, 3, 0, b"abc")
        dec = rt4k_serial.Rtl1Decoder()
        self.assertEqual(dec.feed(b"junk\xa5" + frame[:5]), [])
        frames = dec.feed(frame[5:] + b"\xa5")
        self.assertEqual(len(frames), 1)
        self.assertEqual(bytes(frames[0]["payload"]), b"abc")
        # The trailing lone A5 is kept as a possible frame start.
        self.assertEqual(dec.buf, b"\xa5")

    def test_bad_crc_flagged(self):
        frame = bytearray(rt4k_serial.encode_frame(1, 3, 0, b"abc"))
        frame[9] ^= 0xFF
        frames = rt4k_serial.Rtl1Decoder().feed(bytes(frame))
        self.assertEqual(len(frames), 1)
        self.assertFalse(frames[0]["crc_ok"])

    def test_oversized_length_is_skipped(self):
        bogus = b"\xa5\x5a\x00\x00\xff\xff\x03\x00"
        good = rt4k_serial.encode_frame(1, 3, 0, b"")
        frames = rt4k_serial.Rtl1Decoder().feed(bogus + good)
        self.assertEqual([f["payload"] for f in frames], [b""])

    def test_encode_rejects_oversize(self):
        with self.assertRaises(ValueError):
            rt4k_serial.encode_frame(1, 3, 0, b"x" * 2049)


class Crc16ImplementationTest(unittest.TestCase):
    """crc16() is binascii.crc_hqx."""

    def test_accepts_a_running_start_value(self):
        self.assertEqual(rt4k_serial.crc16(b"9", rt4k_serial.crc16(b"12345678")),
                         rt4k_serial.crc16(b"123456789"))


class PutFramesTest(unittest.TestCase):
    def test_chunks_then_empty_terminator(self):
        data = bytes(range(256)) * 9            # 2304 bytes: one full frame + 256
        frames = rt4k_serial.put_frames(0x1234, data)
        decoded = [rt4k_serial.Rtl1Decoder().feed(f)[0] for f in frames]
        self.assertEqual([(f["type"], f["seq"], len(f["payload"])) for f in decoded],
                         [(3, 0, 2048), (3, 1, 256), (3, 2, 0)])
        self.assertTrue(all(f["crc_ok"] and f["nonce"] == 0x1234 for f in decoded))
        self.assertEqual(b"".join(bytes(f["payload"]) for f in decoded), data)

    def test_empty_file_is_just_the_terminator(self):
        frames = rt4k_serial.put_frames(1, b"")
        self.assertEqual(len(frames), 1)
        self.assertEqual(rt4k_serial.Rtl1Decoder().feed(frames[0])[0]["seq"], 0)

    def test_frame_count_matches_the_generator(self):
        for size in (0, 1, 2047, 2048, 2049, 23004, 819200, 866848, 4610184):
            self.assertEqual(rt4k_serial.put_frame_count(size),
                             sum(1 for _ in rt4k_serial.iter_put_frames(7, bytes(size))),
                             "frame count wrong for %d bytes" % size)

    def test_first_frame_is_produced_without_building_the_rest(self):
        """The regression guard for the 2026-09-06 'size cap'.

        Frames must reach the wire lazily. Building them all first spends
        seconds of pure-Python CRC after `put ready` -- 4.34 s for 800 KB on the
        DE10-Nano -- and the firmware gives up before frame 0 arrives, which
        looks exactly like a size limit. If upload() ever goes back to
        materialising the list, this fails.
        """
        data = bytes(range(256)) * 8 * 2000     # ~4 MB, i.e. firmware-image scale
        frames = rt4k_serial.iter_put_frames(0x1234, data)
        counted = [0]
        crc = rt4k_serial.crc16

        def counting_crc(buf, *a, **kw):
            counted[0] += 1
            return crc(buf, *a, **kw)

        rt4k_serial.crc16 = counting_crc
        try:
            first = next(frames)
        finally:
            rt4k_serial.crc16 = crc
        self.assertEqual(counted[0], 1, "first frame cost %d CRC passes, not 1" % counted[0])
        decoded = rt4k_serial.Rtl1Decoder().feed(first)[0]
        self.assertEqual((decoded["type"], decoded["seq"], len(decoded["payload"])), (3, 0, 2048))
        self.assertTrue(decoded["crc_ok"])


class DecideTest(unittest.TestCase):
    """decide() must agree with reconcile.classify() on every combination
    a deploy can meet: the card copy present or absent, matching the repo,
    the baseline, both, or neither."""

    def test_matches_reconcile_on_every_combination(self):
        from rt4k import reconcile
        digests = {"a": "a" * 32, "b": "b" * 32, "c": "c" * 32}
        for card in (None, "a", "b", "c"):
            for repo in ("a", "b"):
                for base in (None, "a", "b", "c"):
                    card_map = {"p": digests[card]} if card else {}
                    base_map = {"p": digests[base]} if base else {}
                    (d,) = reconcile.classify(card_map, {"p": digests[repo]}, base_map)
                    self.assertEqual(
                        rt4k_serial.decide(card_map.get("p"), digests[repo], base_map.get("p")),
                        d.action.value, (card, repo, base))

    def test_plan_parsing(self):
        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as fh:
            fh.write("# comment\nprofile/DV1/X.rt4\t%s\t-\nprofile/DV1/Y.rt4\t%s\t%s\n" % ("1" * 32, "2" * 32, "3" * 32))
        plan = rt4k_serial.read_plan(fh.name)
        self.assertEqual(plan, [("profile/DV1/X.rt4", "1" * 32, None), ("profile/DV1/Y.rt4", "2" * 32, "3" * 32)])


class LineTest(unittest.TestCase):
    def test_split_strips_cr_and_keeps_partial(self):
        lines, rest = rt4k_serial.split_lines(b"[COM] a\r\n\r\n[COM] b\nparti")
        self.assertEqual(lines, ["[COM] a", "[COM] b"])
        self.assertEqual(rest, b"parti")

    def test_prefix_kinds(self):
        self.assertEqual(rt4k_serial.strip_prefix("[COM] ver"), ("device", "ver"))
        self.assertEqual(rt4k_serial.strip_prefix("[MCU] x"), ("mcu", "x"))
        self.assertEqual(rt4k_serial.strip_prefix("osd done"), ("system", "osd done"))


class OsdTextTest(unittest.TestCase):
    def test_rows_use_stride_not_width(self):
        cells = b"AB\x00\x00CD\x00\x00"
        self.assertEqual(rt4k_serial.osd_text(cells, rows=2, width=2, stride=4), ["AB", "CD"])

    def test_non_printables(self):
        self.assertEqual(rt4k_serial.osd_text(b"A\x00\x80B", 1, 4, 4), ["A .B"])


class FindTtyTest(unittest.TestCase):
    def _tree(self, ports):
        root = tempfile.mkdtemp()
        sys_tty = os.path.join(root, "sys")
        for name, vid, pid, devnum in ports:
            usb = os.path.join(sys_tty, "usbdev-" + name)
            iface = os.path.join(usb, "iface")
            portdir = os.path.join(iface, name)
            os.makedirs(portdir)
            os.makedirs(os.path.join(sys_tty, name))
            os.symlink(portdir, os.path.join(sys_tty, name, "device"))
            for fname, val in (("idVendor", vid), ("idProduct", pid)):
                with open(os.path.join(usb, fname), "w") as f:
                    f.write(val + "\n")
            with open(os.path.join(sys_tty, name, "dev"), "w") as f:
                f.write(devnum + "\n")
        return sys_tty

    def test_prefers_renamed_node(self):
        sys_tty = self._tree([("ttyUSB0", "0403", "6001", "188:0"), ("ttyUSB1", "1a86", "7523", "188:1")])
        nodes = {("/dev/rt4k-serial", 188, 0): True}
        path, warning = rt4k_serial.find_rt4k_tty(sys_tty, "/dev", node_is=lambda p, M, m: nodes.get((p, M, m), False))
        self.assertEqual((path, warning), ("/dev/rt4k-serial", None))

    def test_falls_back_to_kernel_name_with_warning(self):
        sys_tty = self._tree([("ttyUSB0", "1a86", "7523", "188:0"), ("ttyUSB1", "0403", "6001", "188:1")])
        nodes = {("/dev/ttyUSB1", 188, 1): True}
        path, warning = rt4k_serial.find_rt4k_tty(sys_tty, "/dev", node_is=lambda p, M, m: nodes.get((p, M, m), False))
        self.assertEqual(path, "/dev/ttyUSB1")
        self.assertIn("Zaparoo", warning)

    def test_never_returns_the_pn532(self):
        sys_tty = self._tree([("ttyUSB0", "1a86", "7523", "188:0")])
        path, warning = rt4k_serial.find_rt4k_tty(sys_tty, "/dev", node_is=lambda p, M, m: True)
        self.assertEqual((path, warning), (None, None))


if __name__ == "__main__":
    unittest.main()


class SerialLockTest(unittest.TestCase):
    """The advisory lock that stops two writers sharing the one tty.

    This exists because of a near miss: a firmware flash pushed a 4.6 MB bitstream
    over a 52 s window while rt4k-dv1-watch.sh was free to fire `prof load`
    into the same port on any core switch. Text commands survive interleaving;
    a binary transfer does not.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.lock = os.path.join(self.dir, "rt4k-serial.lock")

    def test_take_then_holder_is_us(self):
        self.assertTrue(rt4k_serial.take_lock(wait_s=0, lock=self.lock))
        self.assertEqual(rt4k_serial.lock_holder(self.lock), os.getpid())

    def test_drop_releases(self):
        rt4k_serial.take_lock(wait_s=0, lock=self.lock)
        rt4k_serial.drop_lock(self.lock)
        self.assertFalse(os.path.exists(self.lock))
        self.assertIsNone(rt4k_serial.lock_holder(self.lock))

    def test_free_lock_has_no_holder(self):
        self.assertIsNone(rt4k_serial.lock_holder(self.lock))

    def test_live_holder_blocks_and_we_give_up(self):
        # A lock held by a process that really exists (us) must not be stolen.
        os.mkdir(self.lock)
        with open(os.path.join(self.lock, "pid"), "w") as fh:
            fh.write(str(os.getpid()))
        self.assertFalse(rt4k_serial.take_lock(wait_s=0, lock=self.lock))

    def test_stale_lock_is_reclaimed(self):
        # The MiSTer reboots mid-sweep often enough that a sticky lock left by a
        # dead pid would be worse than no lock: it would wedge every later run.
        os.mkdir(self.lock)
        with open(os.path.join(self.lock, "pid"), "w") as fh:
            fh.write("999999")          # not a live pid
        self.assertTrue(rt4k_serial.take_lock(wait_s=0, lock=self.lock))
        self.assertEqual(rt4k_serial.lock_holder(self.lock), os.getpid())

    def test_garbage_pid_file_is_stale_not_fatal(self):
        os.mkdir(self.lock)
        with open(os.path.join(self.lock, "pid"), "w") as fh:
            fh.write("not-a-pid")
        self.assertTrue(rt4k_serial.take_lock(wait_s=0, lock=self.lock))

    def test_missing_pid_file_is_stale(self):
        os.mkdir(self.lock)                     # dir but no pid file
        self.assertTrue(rt4k_serial.take_lock(wait_s=0, lock=self.lock))

    def test_it_waits_rather_than_failing_immediately(self):
        # A firmware put holds the port for ~a minute. A watcher that gave up at
        # once would leave the wrong profile on screen, so take_lock waits.
        slept = []
        held = {"n": 0}

        def fake_sleep(d):
            slept.append(d)
            if len(slept) == 3:                 # holder goes away mid-wait
                os.remove(os.path.join(self.lock, "pid"))
                os.rmdir(self.lock)
            held["n"] += 1

        os.mkdir(self.lock)
        with open(os.path.join(self.lock, "pid"), "w") as fh:
            fh.write(str(os.getpid()))
        self.assertTrue(rt4k_serial.take_lock(wait_s=99, lock=self.lock,
                                              _sleep=fake_sleep))
        self.assertGreaterEqual(len(slept), 3)

    def test_wait_is_bounded(self):
        # a full sweep of every core holds the lock for over an hour; nothing
        # should block behind it indefinitely.
        clock = {"t": 0.0}

        def fake_now():
            return clock["t"]

        def fake_sleep(d):
            clock["t"] += d

        os.mkdir(self.lock)
        with open(os.path.join(self.lock, "pid"), "w") as fh:
            fh.write(str(os.getpid()))
        self.assertFalse(rt4k_serial.take_lock(wait_s=5, lock=self.lock,
                                               _sleep=fake_sleep, _now=fake_now))
        self.assertLessEqual(clock["t"], 6.0)

    def test_drop_never_steals_someone_elses_lock(self):
        # An exiting process must not free a lock another tool is holding --
        # that would silently re-open the interleaving this prevents.
        os.mkdir(self.lock)
        with open(os.path.join(self.lock, "pid"), "w") as fh:
            fh.write("999999")
        rt4k_serial.drop_lock(self.lock)
        self.assertTrue(os.path.exists(self.lock))

    def test_env_escape_hatch(self):
        os.mkdir(self.lock)
        with open(os.path.join(self.lock, "pid"), "w") as fh:
            fh.write(str(os.getpid()))
        os.environ["RT4K_NO_LOCK"] = "1"
        try:
            self.assertTrue(rt4k_serial.take_lock(wait_s=0, lock=self.lock))
        finally:
            del os.environ["RT4K_NO_LOCK"]
