"""The deployed-baseline manifest: md5 + card-relative path, one file per line.

This is the merge base. Without it a three-way reconcile cannot tell a profile
tuned on the unit from one improved in the repo, so every difference becomes a
conflict. Two spaces separate the fields, md5sum-style; paths may contain
spaces -- including consecutive spaces -- so the split takes the FIRST run of
two spaces in the line, which is always the digest/path separator since the
digest itself (hex, fixed width) never contains one.

read() strips both "\\n" and "\\r\\n" line endings. In practice this is
belt-and-suspenders: opening in text mode with the default `newline=None`
already normalizes "\\r\\n" to "\\n" via universal-newline translation before
`rstrip` ever sees the line, so a bare `rstrip("\\n")` behaves identically for
files read through this module. The explicit "\\r\\n" strip is kept anyway as
cheap defence against a caller or future refactor that opens the file in
binary or newline="" mode.

Filenames coming off a FAT/exFAT card are not guaranteed to be valid UTF-8
(legacy 8.3 names, wrong codepage, corrupted directory entries). Python's
os.walk/os.listdir hand those back re-encoded with `surrogateescape`, so both
open() calls here use the same error handler -- matching how Python normally
round-trips OS paths -- instead of crashing the whole sync on one bad name.

A truncated or corrupted line (a real possibility: this file lives on FAT,
and a write can be interrupted) is not allowed to fail silently. read() warns
to stderr, with the line number, for any non-blank, non-comment line that
doesn't split into a (digest, name) pair, or whose digest isn't 32 hex
characters. It still parses what it can rather than raising -- a corrupt
baseline should degrade loudly, not abort the sync -- except a line with no
recoverable name is dropped, same as before.
"""
import re
import sys

HEADER = "# RT4K deployed baseline - md5 and card-relative path of the last push\n"

_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


def read(path):
    try:
        fh = open(path, encoding="utf-8", errors="surrogateescape")
    except FileNotFoundError:
        return {}
    with fh:
        out = {}
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            digest, sep, name = line.partition("  ")
            if not sep or not name:
                print(
                    f"manifest: {path}:{lineno}: malformed line, skipping: {line!r}",
                    file=sys.stderr,
                )
                continue
            if not _HEX32.match(digest):
                print(
                    f"manifest: {path}:{lineno}: digest is not 32 hex characters: {line!r}",
                    file=sys.stderr,
                )
            out[name] = digest
        return out


def write(path, entries):
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write(HEADER)
        for name in sorted(entries):
            fh.write(f"{entries[name]}  {name}\n")
