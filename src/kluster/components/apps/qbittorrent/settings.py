"""Merge the settings the component owns into the configuration qBittorrent keeps, as the init container of its pod.

It runs in the server's own image, under the interpreter the image carries for
the server's search plugins, so it imports nothing beyond the standard
library: `python3 settings.py <configuration> <owned settings>`. The component
(`kluster.components.apps.qbittorrent`) renders the owned settings as JSON,
`OwnedSetting`'s fields for each, and mounts this file beside them.

**Each owned setting has one place in the merged file**, the one the pinned
server reads it from: its key's first component is the INI section, and the
rest, `/` written as `\\`, the key within it, which is how the server's
settings store writes a key. Each is set there, where the file holds it or at
the end of its section where it does not, with a section added at the end of
the file where the file has none. Each key the server's migration would move
onto it (`replaces`) is removed, since the server, starting on a file an older
release wrote, moves such a key over the newer one and would undo the merge.
Every other line stays as the file holds it, in its order.

A server with no configuration yet takes the owned settings as its initial
state, and fills in its defaults around them. A file of any other shape than
the server writes is refused and left as it is: an empty file, a line that is
neither a section, a setting nor blank, or a last line with no end, which is a
file cut short within a line. The server would read such a file as fewer
settings than it kept, and a merge over it would write that back whole.

The result is written beside the file, flushed to disk and renamed over it, so
a merge cut short, or a node lost after the rename, leaves a whole file. It is
the pod's user's, with mode 0600, since the file holds the interface's password
hash.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

__all__ = ('OwnedSetting', 'Refused', 'main', 'merge', 'read_owned')


class Refused(ValueError):
    """The configuration is not in the shape the server writes, so nothing is merged over it."""


@dataclass(frozen=True)
class OwnedSetting:
    """One setting the component owns: its key as the server names it, its value as the server writes it, and the keys it replaces."""

    key: str
    """`Section/Key`, the pinned server's name for the setting, `/` between its parts."""
    value: str
    """The value as the server's settings store writes it in the INI file, quoting included."""
    replaces: tuple[str, ...] = ()
    """The keys an older release kept the setting under, which the server's migration moves onto `key`."""


def _place(key: str) -> tuple[str, str]:
    """The section and the INI key a setting's key is written at."""
    section, _, rest = key.partition('/')
    if not section or not rest:
        raise ValueError(f'{key!r} names no section and key: a setting is `Section/Key`')
    return section, rest.replace('/', '\\')


def _section_of(line: str) -> str | None:
    """The section a header line opens, or `None` for any other line."""
    stripped = line.strip()
    if stripped.startswith('[') and stripped.endswith(']'):
        return stripped[1:-1]
    return None


def merge(configuration: str | None, owned: tuple[OwnedSetting, ...]) -> str:
    """`configuration` with every owned setting set and every key it replaces removed; the owned settings alone where it is `None`.

    :raises Refused: Where `configuration` is not in the shape the server
        writes, which is left to whoever restores it.
    """
    wanted: dict[tuple[str, str], str] = {}
    for setting in owned:
        wanted[_place(setting.key)] = setting.value
    dropped = {_place(key) for setting in owned for key in setting.replaces}

    lines = _lines(configuration)
    # Each section's lines in order, under its name; the lines ahead of the
    # first header, which the server writes none of, under `None`.
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in lines:
        header = _section_of(line)
        if header is not None:
            sections.append((header, [line]))
        elif not line.strip() or '=' in line:
            sections[-1][1].append(line)
        else:
            raise Refused(f'the line {line!r} is neither a section, a setting nor blank')

    written: set[tuple[str, str]] = set()
    out: list[str] = []
    for name, body in sections:
        kept: list[str] = []
        for line in body:
            if name is None or _section_of(line) is not None or '=' not in line:
                kept.append(line)
                continue
            place = (name, line.partition('=')[0])
            if place in dropped:
                continue
            if place in wanted:
                if place not in written:
                    kept.append(f'{place[1]}={wanted[place]}')
                    written.add(place)
                continue
            kept.append(line)
        # What the section lacks goes at its end, ahead of the blank line
        # the server leaves between sections.
        missing = [(section, key) for (section, key) in wanted if section == name and (section, key) not in written]
        trailing: list[str] = []
        while kept and not kept[-1].strip() and len(kept) > 1:
            trailing.insert(0, kept.pop())
        kept += [f'{key}={wanted[(section, key)]}' for section, key in missing]
        written.update(missing)
        out += kept + trailing

    for section in dict.fromkeys(section for section, _ in wanted):
        absent = [(s, key) for (s, key) in wanted if s == section and (s, key) not in written]
        if not absent:
            continue
        if out and out[-1].strip():
            out.append('')
        out.append(f'[{section}]')
        out += [f'{key}={wanted[(s, key)]}' for s, key in absent]
        written.update(absent)
    return '\n'.join(out) + '\n'


def _lines(configuration: str | None) -> list[str]:
    """The configuration's lines, refused where they cannot be the whole of what the server wrote."""
    if configuration is None:
        return []
    if not configuration.strip():
        raise Refused('the file is empty')
    if not configuration.endswith('\n'):
        raise Refused('the last line has no end, so the file was cut short within it')
    return configuration[:-1].split('\n')


def read_owned(text: str) -> tuple[OwnedSetting, ...]:
    """The owned settings, from the JSON the component renders: a list of objects of `OwnedSetting`'s fields."""
    loaded: object = json.loads(text)
    if not isinstance(loaded, list):
        raise ValueError('the owned settings are a list')
    settings: list[OwnedSetting] = []
    for entry in cast('list[object]', loaded):
        if not isinstance(entry, dict):
            raise ValueError('each owned setting is an object')
        fields = cast('dict[str, object]', entry)
        key, value, replaces = fields.get('key'), fields.get('value'), fields.get('replaces', [])
        if not isinstance(key, str) or not isinstance(value, str) or not isinstance(replaces, list):
            raise ValueError(f'the owned setting {fields!r} is not a key, a value and the keys it replaces')
        names = tuple(str(it) for it in cast('list[object]', replaces))
        settings.append(OwnedSetting(key=key, value=value, replaces=names))
    return tuple(settings)


def main(argv: list[str]) -> int:
    """Merge the owned settings at `argv[1]` into the configuration at `argv[0]`, writing it in place."""
    if len(argv) != 2:
        print('usage: settings.py <configuration> <owned settings>', file=sys.stderr)
        return 2
    target, owned_file = Path(argv[0]), Path(argv[1])
    owned = read_owned(owned_file.read_text(encoding='utf-8'))
    existing = target.read_text(encoding='utf-8') if target.exists() else None
    try:
        merged = merge(existing, owned)
    except Refused as refused:
        print(f'{target}: {refused}; refusing to merge over it', file=sys.stderr)
        return 1
    if existing is None:
        print(f'{target} does not exist yet: installing the owned settings as its initial state')
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f'{target.name}.merged')
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as out:
        out.write(merged)
        out.flush()
        os.fsync(out.fileno())
    os.replace(staged, target)
    print(f'{target}: set {", ".join(setting.key for setting in owned)}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
