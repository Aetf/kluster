"""`main`'s dispatch reaches every family's handler.

The match in `main` keys on `(family, member, action)`, but not every family
defines every level: `bootstrap` and `rotate` have no `<action>` subparser, so
the attribute is absent from their namespaces and only a guarded read works.
These tests drive the real argument parser, so a family whose namespace shape
the dispatch mis-reads fails here rather than on an operator's first run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kluster.scripts.credentials import cli
from kluster.scripts.credentials.kdbx import PATH_ENV, KdbxStore

PASSWORD = 'kit-password'


@pytest.fixture
def kit_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / 'kit.kdbx'
    _ = KdbxStore.create(path, PASSWORD)
    monkeypatch.setenv(PATH_ENV, str(path))
    return path


def test_bootstrap_dispatches_without_an_action_level(kit_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | None] = []

    def fake_bootstrap(kit: KdbxStore, *, prompt: Callable[[str], str], only: str | None) -> list[str]:
        calls.append(only)
        return []

    monkeypatch.setattr(cli.lifecycle, 'bootstrap', fake_bootstrap)
    assert cli.main(['bootstrap']) == 0
    assert calls == [None]


def test_rotate_dispatches_without_an_action_level(
    kit_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    successor = tmp_path / 'next.kdbx'
    calls: list[str | None] = []

    def fake_rotate(
        kit: KdbxStore,
        into: KdbxStore,
        *,
        prompt: Callable[[str], str],
        only: str | None,
    ) -> list[str]:
        calls.append(only)
        return ['derivation']

    def answer(_prompt: str) -> str:
        return PASSWORD

    monkeypatch.setattr(cli.lifecycle, 'rotate', fake_rotate)
    monkeypatch.setattr('getpass.getpass', answer)
    assert cli.main(['rotate', '--into', str(successor)]) == 0
    assert calls == [None]
