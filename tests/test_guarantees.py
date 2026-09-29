"""The audit guarantees in docs/technical-audit.md, as tests."""

from __future__ import annotations

from party_line import commands

WIRE_WORDS = {'ver', 'prof', 'input', 'output', 'osd', 'ls', 'stat', 'get', 'remote', 'pwr', 'baud'}


def test_wire_words_are_the_audited_set() -> None:
    """Guarantee 2: the first words are exactly what docs/technical-audit.md lists."""
    assert commands.wire_words() == WIRE_WORDS
