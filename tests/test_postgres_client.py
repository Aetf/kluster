"""The workstation's Postgres client is `mise.toml`'s, on the appliance's major.

`state-backend dump` and `restore` run `pg_dump` and `pg_restore` from `PATH`,
and `mise.toml`'s `postgres` pin is what puts them there. The appliance's
server is whatever major `settings.POSTGRES_IMAGE` names. `pg_dump` refuses a
server of a newer major than its own, and what a client of a newer major
writes is not guaranteed to load into an older server, so the two majors are
held equal rather than left to drift apart with each bump.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import cast

import pytest

from kluster.lib.state_backend import settings, state

ROOT = Path(__file__).parent.parent

#: The key `mise.toml` pins the client under.
PIN = 'postgres'


def _tools() -> dict[str, object]:
    return tomllib.loads((ROOT / 'mise.toml').read_text())['tools']


def _major(version: str) -> str:
    found = re.match(r'\d+', version)
    assert found is not None, f'{version!r} does not start with a major'
    return found.group()


def test_the_local_client_is_on_the_appliance_major() -> None:
    pin = _tools()[PIN]
    assert isinstance(pin, dict)
    pinned = cast(dict[str, object], pin)['version']
    assert isinstance(pinned, str)
    _, tag = settings.POSTGRES_IMAGE.rsplit(':', 1)

    assert _major(pinned) == _major(tag)


def test_a_missing_client_names_its_pin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The refusal sends the operator to the pin, so the pin it names has to be
    # one `mise.toml` carries. Nothing is reached: the tool is not found
    # before any connection is opened.
    monkeypatch.setattr(state, 'PG_DUMP', 'pg_dump-that-is-not-installed')
    target = state.Connection(url='postgresql://nobody@127.0.0.1:1/nothing', env={})

    with pytest.raises(state.StateError, match=re.escape(f'`{PIN}` pin in mise.toml')):
        state.pg_dump(target, tmp_path / 'archive')
    assert PIN in _tools()
