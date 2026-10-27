"""Every tool `mise.toml` pins is held to its bytes by `mise.lock`.

A version names a release and not its contents, so a release asset uploaded
again under the same version moves under a pin written as a version alone.
`mise.lock` records, for each pin, the artifact's URL and its sha256 on the
platform every runner and workstation runs, and mise checks a download against
that sum before it installs it (`verify_checksum` in mise's `src/backend`, at
the release CI pins). That check can only refuse a sum that is recorded, and
mise records one on its own for whatever it fetched when an entry is missing.
So what is held here is the shape that leaves no such gap: `mise.toml` declares
its tools locked, which turns a missing entry into a refusal, and every pin has
one entry, at its own version, carrying a sha256.

Whether a recorded sum is the *right* one is not answerable without the
artifact. The install answers it: a sum that does not match what the release
serves fails `mise install` by name in every job that installs the toolchain.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).parent.parent

#: The platform the lock carries: every runner, a cloud session and the
#: workstations are this one.
PLATFORM = 'linux-x64'

#: A sum the publisher's metadata or the artifact itself gave `mise lock`. mise
#: writes a `blake3:` sum only when it computes one from a download it was not
#: told to expect, which is the trust-on-first-use this lock exists to replace.
SHA256 = re.compile(r'sha256:[0-9a-f]{64}')


def _pins() -> dict[str, tuple[str, dict[str, Any]]]:
    """Each `[tools]` key, with its version and the options it is written with."""
    pins: dict[str, tuple[str, dict[str, Any]]] = {}
    for name, pin in tomllib.loads((ROOT / 'mise.toml').read_text())['tools'].items():
        if isinstance(pin, str):
            pins[name] = (pin, {})
        else:
            table = dict(cast(dict[str, Any], pin))
            pins[name] = (str(table.pop('version')), table)
    return pins


def _entries() -> dict[str, list[dict[str, Any]]]:
    return cast(dict[str, list[dict[str, Any]]], tomllib.loads((ROOT / 'mise.lock').read_text())['tools'])


PINS = _pins()


def _as_mise_spells(value: object) -> str:
    """An option's value as `mise.lock` records it: a string, with a boolean as TOML writes one."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def test_the_lock_is_binding() -> None:
    """Without `locked`, a pin with no entry installs and records what it fetched."""
    config = tomllib.loads((ROOT / 'mise.toml').read_text())
    assert config.get('tool_config', {}).get('locked') is True


def test_the_lock_holds_the_pinned_tools_and_no_others() -> None:
    """A tool respelled under another backend leaves its entry behind as a stray."""
    assert sorted(_entries()) == sorted(PINS)


@pytest.mark.parametrize('name', sorted(PINS))
def test_each_pin_has_one_entry_at_its_version(name: str) -> None:
    """The entry is the pin's: its version, and every option it records written in the pin with that value.

    An entry records only the options its backend keys an install by -- a
    conda pin's `channel`, but none of an `http:` pin's `url` or `bin_path`
    -- so what is held is that the entry says nothing the pin does not: an
    option recorded with another value, or one the pin does not hold, is an
    entry written for some other pin. The other direction, an option the pin
    writes that its backend keys by and the entry lacks, is mise's own
    refusal: an install takes an entry only where the options it records
    equal the ones the backend computes from the pin (`get_locked_version`),
    and otherwise fails with the pin `not in the lockfile`. mise records
    every option as a string, so the pin's value is compared as mise spells
    it.
    """
    version, options = PINS[name]
    entries = _entries().get(name, [])

    assert [entry['version'] for entry in entries] == [version], f'run `mise lock {name}` after moving its pin'
    recorded: dict[str, Any] = entries[0].get('options', {})
    pinned = {key: _as_mise_spells(options[key]) for key in recorded if key in options}
    assert pinned == recorded, f'run `mise lock {name}` after changing its options'


@pytest.mark.parametrize('name', sorted(PINS))
def test_each_entry_records_the_artifact_and_its_sum(name: str) -> None:
    (entry,) = _entries()[name]
    platform = entry.get(f'platforms.{PLATFORM}', {})

    assert str(platform.get('url', '')).startswith('https://')
    assert SHA256.fullmatch(str(platform.get('checksum', ''))), f'{name} has no sha256 for {PLATFORM}'
