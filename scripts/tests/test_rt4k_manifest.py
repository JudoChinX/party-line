import contextlib
import io
import os
import tempfile
import unittest

from rt4k import manifest


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "m.txt")

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(manifest.read(self.path), {})

    def test_roundtrip(self):
        entries = {"profile/dv1/snes.rt4": "a" * 32, "profile/dv1/menu.rt4": "b" * 32}
        manifest.write(self.path, entries)
        self.assertEqual(manifest.read(self.path), entries)

    def test_written_file_is_sorted_and_stable(self):
        manifest.write(self.path, {"z.rt4": "1" * 32, "a.rt4": "2" * 32})
        first = open(self.path).read()
        manifest.write(self.path, {"a.rt4": "2" * 32, "z.rt4": "1" * 32})
        self.assertEqual(first, open(self.path).read())
        self.assertTrue(first.index("a.rt4") < first.index("z.rt4"))

    def test_paths_with_spaces_survive(self):
        entries = {"profile/nintendo game boy/gb - lcd green.rt4": "c" * 32}
        manifest.write(self.path, entries)
        self.assertEqual(manifest.read(self.path), entries)

    def test_comments_and_blank_lines_ignored(self):
        with open(self.path, "w") as fh:
            fh.write("# a comment\n\n" + "d" * 32 + "  x.rt4\n")
        self.assertEqual(manifest.read(self.path), {"x.rt4": "d" * 32})

    def test_path_with_consecutive_double_space_survives(self):
        # partition("  ") must find the digest/path separator -- the FIRST
        # run of two spaces in the line -- not a later one inside the path
        # itself. Real card paths do this, e.g. "GB - LCD  Alpha.rt4".
        entries = {"profile/Nintendo Game Boy/GB - LCD  Alpha Yellow-Green.rt4": "e" * 32}
        manifest.write(self.path, entries)
        self.assertEqual(manifest.read(self.path), entries)

    def test_crlf_input_round_trips(self):
        # This data comes off a FAT card and may round-trip through Windows
        # tools, which write CRLF. Pins that CRLF input parses cleanly --
        # NOT a regression test for a real bug: text-mode open() with the
        # default newline=None already normalizes "\r\n" to "\n" via
        # universal-newline translation before rstrip ever runs, so this
        # passes against a bare rstrip("\n") too. The explicit rstrip("\r\n")
        # in read() is kept only as cheap defence against a future refactor
        # that opens in binary or newline="" mode.
        with open(self.path, "wb") as fh:
            fh.write(b"# a comment\r\n" + b"f" * 32 + b"  profile/menu.rt4\r\n")
        self.assertEqual(manifest.read(self.path), {"profile/menu.rt4": "f" * 32})

    def test_realistic_and_unicode_paths_roundtrip(self):
        entries = {
            "profile/Nintendo Game Boy/GB - LCD Alpha Yellow-Green (DV1 default).rt4": "1" * 32,
            "profile/ターボグラフックス/menu.rt4": "2" * 32,
        }
        manifest.write(self.path, entries)
        self.assertEqual(manifest.read(self.path), entries)

    def test_surrogate_escaped_filename_roundtrips(self):
        # Filenames off FAT/exFAT that aren't valid UTF-8 (legacy 8.3 names,
        # wrong codepage, corrupted directory entries) come back from
        # os.walk/os.listdir re-encoded with surrogateescape. Strict UTF-8
        # on open() crashes the whole sync on one bad filename.
        name = os.fsdecode(b"profile/broken_\xffname.rt4")
        entries = {name: "c" * 32}
        manifest.write(self.path, entries)
        self.assertEqual(manifest.read(self.path), entries)

    def test_malformed_line_with_no_separator_is_dropped_and_warned(self):
        with open(self.path, "w") as fh:
            fh.write("truncated-mid-write-no-separator\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = manifest.read(self.path)
        self.assertEqual(result, {})
        self.assertIn(f"{self.path}:1:", stderr.getvalue())

    def test_malformed_line_with_empty_digest_is_kept_and_warned(self):
        with open(self.path, "w") as fh:
            fh.write("  path/with/empty/digest.rt4\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = manifest.read(self.path)
        self.assertEqual(result, {"path/with/empty/digest.rt4": ""})
        self.assertIn(f"{self.path}:1:", stderr.getvalue())

    def test_malformed_line_with_short_digest_is_kept_and_warned(self):
        with open(self.path, "w") as fh:
            fh.write("abc  profile/short-digest.rt4\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = manifest.read(self.path)
        self.assertEqual(result, {"profile/short-digest.rt4": "abc"})
        self.assertIn(f"{self.path}:1:", stderr.getvalue())

    def test_well_formed_lines_produce_no_warnings(self):
        manifest.write(self.path, {"profile/menu.rt4": "a" * 32})
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            manifest.read(self.path)
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
