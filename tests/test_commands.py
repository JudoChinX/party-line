"""Tests for the allowlist and its validators."""

from __future__ import annotations

import pytest

from party_line import commands

_render_cases = {
    'ver': {'name': 'ver', 'value': None, 'expected': 'ver'},
    'profile': {'name': 'profile', 'value': None, 'expected': 'prof get'},
    'get': {'name': 'get', 'value': '/profile/DV1/MENU.rt4', 'expected': 'get -- /profile/DV1/MENU.rt4'},
    'ls': {'name': 'ls', 'value': '/profile', 'expected': 'ls /profile'},
    'remote': {'name': 'remote', 'value': 'menu', 'expected': 'remote menu'},
    'remote_aux8': {'name': 'remote', 'value': 'aux8', 'expected': 'remote aux8'},
}

_refuse_cases = {
    'newline_injection': {'name': 'stat', 'value': '/a\nrm /b'},
    'tab': {'name': 'stat', 'value': '/a\tb'},
    'dotdot_middle': {'name': 'stat', 'value': '/profile/../x'},
    'dotdot_backslash': {'name': 'get', 'value': '/profile\\..\\x'},
    'empty': {'name': 'stat', 'value': ''},
    'card_relative': {'name': 'stat', 'value': 'profile/A.rt4'},
    'card_space': {'name': 'ls', 'value': '/_CRT Emulation'},
    'unknown_key': {'name': 'remote', 'value': 'rm'},
    'missing_arg': {'name': 'stat', 'value': None},
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


def test_every_argument_row_has_a_validator() -> None:
    """Test no row can put an unchecked argument on the wire."""
    assert all(command.validate is not None for command in commands.COMMANDS if command.arg is not None)


def test_remote_keys_are_documented_buttons() -> None:
    """Test the key set is the 52 documented buttons."""
    assert len(commands.REMOTE_KEYS) == 52
    assert {'menu', 'pwr', 'prof12', 'aux8', 'res4'} <= commands.REMOTE_KEYS


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
    'input': {
        'name': 'input',
        'lines': ['input=0 HDMI ic=2 model=0'],
        'expected': {'input': '0', 'name': 'HDMI', 'ic': '2', 'model': '0'},
    },
    'input_without_a_name': {
        'name': 'input',
        'lines': ['input=0 ic=2 model=0'],
        'expected': {'input': '0', 'ic': '2', 'model': '0'},
    },
    'output': {
        'name': 'output',
        'lines': ['output sel=0 live=0 model=0'],
        'expected': {'sel': '0', 'live': '0', 'model': '0'},
    },
    'stat': {
        'name': 'stat',
        'lines': ['stat t=F sz=23040 mt=1577836800 at=0x20 nm=/profile/DV1/MENU.rt4'],
        'expected': {'t': 'F', 'sz': '23040', 'mt': '1577836800', 'at': '0x20', 'nm': '/profile/DV1/MENU.rt4'},
    },
    'stat_error': {'name': 'stat', 'lines': ['stat err=2 NOSUCH'], 'expected': {'err': '2'}},
    'ls': {
        'name': 'ls',
        'lines': ['ent t=D sz=0 mt=1 nm=DV1', 'ent t=F sz=9 mt=2 nm=JVC D200.rt4', 'ls end 2'],
        'expected': {
            'entries': [
                {'t': 'D', 'sz': '0', 'mt': '1', 'nm': 'DV1'},
                {'t': 'F', 'sz': '9', 'mt': '2', 'nm': 'JVC D200.rt4'},
            ],
            'count': 2,
            'truncated': False,
        },
    },
    'ls_truncated': {
        'name': 'ls',
        'lines': ['ent t=F sz=9 mt=2 nm=A.rt4', 'ls truncated at 512', 'ls end 512'],
        'expected': {'entries': [{'t': 'F', 'sz': '9', 'mt': '2', 'nm': 'A.rt4'}], 'count': 1, 'truncated': True},
    },
    'remote': {'name': 'remote', 'lines': ['Serial Remote: menu'], 'expected': {'key': 'menu'}},
}


@pytest.mark.parametrize(
    'name, lines, expected',
    [(case['name'], case['lines'], case['expected']) for case in _parse_cases.values()],
    ids=list(_parse_cases.keys()),
)
def test_parse(name: str, lines: list[str], expected: dict[str, object]) -> None:
    """Test each text row's parser turns real firmware replies into strings, as sent."""
    assert commands.BY_NAME[name].parse(lines) == expected


def test_osd_rows_use_stride_and_mark_non_printables() -> None:
    """Test rows are cut by stride, trimmed to width, NUL is a space and others a dot."""
    cells = b'AB\x00\x01' + b'zz' + b'CD' + b'\x00' * 4
    assert commands.parse_osd(cells, {'rows': '2', 'width': '4', 'stride': '6'}) == {'rows': ['AB .', 'CD']}


_garbled_osd_cases = {
    'rows_not_a_number': {'rows': 'x', 'width': '2', 'stride': '2'},
    'rows_superscript_digit': {'rows': '\u00b2', 'width': '2', 'stride': '2'},
    'rows_too_long': {'rows': '9' * 5000, 'width': '2', 'stride': '2'},
    'stride_not_a_number': {'rows': '2', 'width': '2', 'stride': 'x'},
    'width_missing': {'rows': '2', 'stride': '2'},
    'width_over_stride': {'rows': '2', 'width': '4', 'stride': '2'},
}


@pytest.mark.parametrize('ready', list(_garbled_osd_cases.values()), ids=list(_garbled_osd_cases))
def test_osd_with_a_garbled_ready_line_has_no_rows(ready: dict[str, str]) -> None:
    """Test missing or garbled geometry gives no rows instead of raising or repeating a row."""
    assert commands.parse_osd(b'ABCD', ready) == {'rows': []}


def test_osd_never_has_more_rows_than_the_data() -> None:
    """Test a row count beyond the data is cut to the rows the data holds."""
    assert commands.parse_osd(b'ABCDE', {'rows': '9', 'width': '2', 'stride': '2'}) == {'rows': ['AB', 'CD', 'E']}


def test_parse_file() -> None:
    """Test get's data describes the bytes: size, SHA-256 and base64."""
    assert commands.BY_NAME['get'].parse(b'hi', {}) == {
        'size': 2,
        'sha256': '8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4',
        'content_base64': 'aGk=',
    }
