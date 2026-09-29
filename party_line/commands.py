"""The complete set of commands party_line can send to the RT4K.

COMMANDS is the single source of truth: the CLI's subcommands, the session's
wire format, the README's command table and the "only these words reach the
tty" test are all derived from it. Adding a row is the only way to add a
command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from typing import Callable
from typing import Optional

KIND_TEXT = 'text'

Data = dict[str, Any]
Parser = Callable[..., Data]

_BUILD = re.compile(r'Build tag: (\S+)')
_FIELD = re.compile(r'(\w+)=(\S+)')
_PATH_FIELD = re.compile(r'\b(file|nm)=(.*\S)')
_VERSION = re.compile(r'(.+?), FW Version: (\S+)')


class ValidationError(ValueError):
    """An argument that must never reach the RT4K."""


def _fields(line: str) -> Data:
    """The key=value pairs in one reply line; a trailing 'file' or 'nm' path keeps its spaces."""
    fields: Data = dict(_FIELD.findall(line))
    fields.update(_PATH_FIELD.findall(line))
    return fields


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
)
BY_NAME = {command.name: command for command in COMMANDS}
