"""Tests for the allowlist and its validators."""

from __future__ import annotations

import pytest

from party_line import commands

_render_cases = {
    'ver': {'name': 'ver', 'value': None, 'expected': 'ver'},
    'profile': {'name': 'profile', 'value': None, 'expected': 'prof get'},
}

_refuse_cases = {
    'unexpected_arg': {'name': 'ver', 'value': 'x'},
}


@pytest.mark.parametrize(
    'name, value, expected',
    [(case['name'], case['value'], case['expected']) for case in _render_cases.values()],
    ids=list(_render_cases.keys()),
)
def test_render(name: str, value: str, expected: str) -> None:
    """Test each row renders its exact wire text."""
    assert commands.BY_NAME[name].render(value) == expected


@pytest.mark.parametrize(
    'name, value',
    [(case['name'], case['value']) for case in _refuse_cases.values()],
    ids=list(_refuse_cases.keys()),
)
def test_render_refuses(name: str, value: str) -> None:
    """Test unsafe or malformed arguments never become wire text."""
    with pytest.raises(commands.ValidationError):
        commands.BY_NAME[name].render(value)


_parse_cases = {
    'ver': {
        'name': 'ver',
        'lines': ['RT4KPRO, FW Version: 1.86.0', 'Build tag: b0817c'],
        'expected': {'model': 'RT4KPRO', 'firmware': '1.86.0', 'build': 'b0817c'},
    },
    'ver_without_build': {
        'name': 'ver',
        'lines': ['RT4KPRO, FW Version: 1.86.0'],
        'expected': {'model': 'RT4KPRO', 'firmware': '1.86.0'},
    },
    'profile': {
        'name': 'profile',
        'lines': ['prof loaded=1 dir=/profile file=DV1/MENU.rt4'],
        'expected': {'loaded': '1', 'dir': '/profile', 'file': 'DV1/MENU.rt4'},
    },
    'profile_with_spaces': {
        'name': 'profile',
        'lines': ['prof loaded=1 dir=/profile file=_CRT Emulation/JVC D200.rt4'],
        'expected': {'loaded': '1', 'dir': '/profile', 'file': '_CRT Emulation/JVC D200.rt4'},
    },
}


@pytest.mark.parametrize(
    'name, lines, expected',
    [(case['name'], case['lines'], case['expected']) for case in _parse_cases.values()],
    ids=list(_parse_cases.keys()),
)
def test_parse(name: str, lines: list[str], expected: dict[str, object]) -> None:
    """Test each text row's parser turns real firmware replies into strings, as sent."""
    assert commands.BY_NAME[name].parse(lines) == expected
