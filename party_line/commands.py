"""The complete set of commands party_line can send to the RT4K.

COMMANDS is the single source of truth: the CLI's subcommands, the session's
wire format, the README's command table and the "only these words reach the
tty" test are all derived from it. Adding a row is the only way to add a
command.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Any
from typing import Callable
from typing import Optional

KIND_DOWNLOAD = 'download'
KIND_LISTING = 'listing'
KIND_TEXT = 'text'
LISTING_ENTRY = 'ent '
LISTING_TRUNCATED = 'ls truncated'
MAX_NUMBER_DIGITS = 9
MAX_PATH_LENGTH = 200

Data = dict[str, Any]
Parser = Callable[..., Data]

_BUILD = re.compile(r'Build tag: (\S+)')
_FIELD = re.compile(r'(\w+)=(\S+)')
_PATH_FIELD = re.compile(r'\b(file|nm)=(.*\S)')
_PRINTABLE_CELLS = range(32, 127)
_PRINTABLE_PATH = re.compile(r'[\x20-\x7e]{1,%d}' % MAX_PATH_LENGTH)
_SEPARATORS = re.compile(r'[/\\]')
_VERSION = re.compile(r'(.+?), FW Version: (\S+)')


class ValidationError(ValueError):
    """An argument that must never reach the RT4K."""


def _check_path(path: str) -> None:
    """Reject anything but short printable ASCII without a '..' segment (either separator)."""
    if not _PRINTABLE_PATH.fullmatch(path):
        raise ValidationError(f'path must be 1-{MAX_PATH_LENGTH} printable ASCII characters: {path!r}')
    if '..' in _SEPARATORS.split(path):
        raise ValidationError(f'path may not contain a ".." segment: {path!r}')


def _fields(line: str) -> Data:
    """The key=value pairs in one reply line; a trailing 'file' or 'nm' path keeps its spaces."""
    fields: Data = dict(_FIELD.findall(line))
    fields.update(_PATH_FIELD.findall(line))
    return fields


def _number(fields: dict[str, str], key: str) -> int:
    """A ready-line field as a number, or 0 when the firmware left it out or garbled it."""
    value = fields.get(key, '')
    return int(value) if value.isdecimal() and len(value) <= MAX_NUMBER_DIGITS else 0


def parse_fields(lines: list[str]) -> Data:
    """Every key=value pair in a reply, as the firmware sent it.

    Args:
        lines: Reply lines, prefixes stripped.

    Returns:
        Keys to string values, e.g. {'loaded': '1', 'dir': '/profile', 'file': 'DV1/MENU.rt4'}.
    """
    data: Data = {}
    for line in lines:
        data.update(_fields(line))
    return data


def parse_file(data: bytes, _ready: dict[str, str]) -> Data:
    """Describe a downloaded file.

    Args:
        data: The file's bytes, already checked against the firmware's SHA-256.
        _ready: The ready line's fields; unused.

    Returns:
        size, sha256 (hex) and content_base64.
    """
    return {
        'size': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
        'content_base64': base64.b64encode(data).decode('ascii'),
    }


def parse_input(lines: list[str]) -> Data:
    """Parse 'input=0 HDMI ic=2 model=0'.

    Args:
        lines: Reply lines, prefixes stripped.

    Returns:
        The key=value fields, plus the bare words as 'name' when there are any.
    """
    data = parse_fields(lines)
    words = [word for line in lines for word in line.split() if '=' not in word]
    if words:
        data['name'] = ' '.join(words)
    return data


def parse_listing(lines: list[str]) -> Data:
    """Parse the 'ent ...' lines of an ls reply.

    Args:
        lines: Reply lines, ending with 'ls end <n>'.

    Returns:
        entries (a dict of fields per entry), count, and truncated: whether the
        firmware said it stopped at its 512-entry limit.
    """
    entries = [_fields(line) for line in lines if line.startswith(LISTING_ENTRY)]
    truncated = any(line.startswith(LISTING_TRUNCATED) for line in lines)
    return {'entries': entries, 'count': len(entries), 'truncated': truncated}


def parse_osd(data: bytes, ready: dict[str, str]) -> Data:
    """Turn an osd download (character cells, then colours) into text rows.

    Args:
        data: The downloaded cells.
        ready: The ready line's fields: rows, width and stride.

    Returns:
        rows: one string per row, cut by stride and trimmed to width. NUL is a
        space and any other unprintable cell a dot. Without a usable width and
        stride, or with rows wider than their stride, there are no rows, and
        never more rows than the data holds.
    """
    rows, width, stride = (_number(ready, key) for key in ('rows', 'width', 'stride'))
    count = min(rows, (len(data) + stride - 1) // stride) if 0 < width <= stride else 0
    text = []
    for row in range(count):
        cells = data[row * stride : row * stride + width]
        line = ''.join(chr(cell) if cell in _PRINTABLE_CELLS else ('.' if cell else ' ') for cell in cells)
        text.append(line.rstrip())
    return {'rows': text}


def parse_ver(lines: list[str]) -> Data:
    """Parse 'RT4KPRO, FW Version: 1.86.0' and 'Build tag: b0817c'.

    Args:
        lines: Reply lines, prefixes stripped.

    Returns:
        model, firmware and build, each only when its line arrived.
    """
    data: Data = {}
    for line in lines:
        version, build = _VERSION.match(line), _BUILD.match(line)
        if version:
            data.update(model=version.group(1), firmware=version.group(2))
        if build:
            data['build'] = build.group(1)
    return data


def validate_card_path(path: str) -> None:
    """Accept an absolute path on the RT4K's card, without spaces.

    Args:
        path: The path as typed, e.g. '/profile/DV1/SNES.rt4'.

    Raises:
        ValidationError: If the path is unsafe to put on the wire.
    """
    _check_path(path)
    if not path.startswith('/'):
        raise ValidationError(f'card paths start with "/": {path!r}')
    if ' ' in path:
        raise ValidationError(f'card paths may not contain spaces: {path!r}')


@dataclass(frozen=True)
class Command:
    """One row of the allowlist.

    Attributes:
        name: The CLI subcommand.
        wire: What is sent, with '{}' where the argument goes.
        kind: KIND_TEXT, KIND_LISTING or KIND_DOWNLOAD.
        help: One line for --help and the README.
        parse: Turns the reply into a result's data: reply lines for text and
            listing rows, (bytes, ready-line fields) for downloads. Pure.
        arg: The argument's metavar, or None when the command takes none.
        validate: Checks the argument; required when arg is set.
    """

    name: str
    wire: str
    kind: str
    help: str
    parse: Parser
    arg: Optional[str] = None
    validate: Optional[Callable[[str], None]] = None

    def render(self, value: Optional[str] = None) -> str:
        """Build the exact text sent to the RT4K, validating the argument first.

        Args:
            value: The argument, or None for commands that take none.

        Returns:
            The wire text, without the trailing carriage return.

        Raises:
            ValidationError: If the argument is missing, unexpected or unsafe.
        """
        if self.arg is None:
            if value is not None:
                raise ValidationError(f'{self.name} takes no argument')
            return self.wire
        if value is None or self.validate is None:
            raise ValidationError(f'{self.name} needs {self.arg}')
        self.validate(value)
        return self.wire.format(value)


COMMANDS = (
    Command('ver', 'ver', KIND_TEXT, 'Firmware version and build tag.', parse_ver),
    Command('profile', 'prof get', KIND_TEXT, 'The profile currently loaded.', parse_fields),
    Command('input', 'input', KIND_TEXT, 'The selected input.', parse_input),
    Command('output', 'output', KIND_TEXT, 'The selected output.', parse_fields),
    Command('osd', 'osd', KIND_DOWNLOAD, 'The on-screen menu, as text.', parse_osd),
    Command(
        'ls',
        'ls {}',
        KIND_LISTING,
        'List a folder on the card (firmware stops at 512 entries).',
        parse_listing,
        'DIR',
        validate_card_path,
    ),
    Command(
        'stat', 'stat {}', KIND_TEXT, 'Size and time of a file on the card.', parse_fields, 'PATH', validate_card_path
    ),
    Command(
        'get',
        'get -- {}',
        KIND_DOWNLOAD,
        'Copy a file from the card to stdout.',
        parse_file,
        'PATH',
        validate_card_path,
    ),
)
BY_NAME = {command.name: command for command in COMMANDS}
