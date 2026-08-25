"""The offline store, exercised against real databases in a temp directory.

Worth testing now that it is a library call rather than a `keepassxc-cli`
invocation: `bootstrap` and `rotate` both create a database and write every
seed into it (credentials.md §4), so a silent failure here is a kit that looks
written and is not.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore

PASSWORD = 'correct horse battery staple'


@pytest.fixture
def store(tmp_path: Path) -> KdbxStore:
    return KdbxStore.create(tmp_path / 'kit.kdbx', PASSWORD)


def test_created_database_round_trips_a_secret(store: KdbxStore) -> None:
    store.put('seeds/B2 seed key', 'key-id', 'key-secret')

    assert store.get('seeds/B2 seed key') == 'key-secret'
    assert store.get('seeds/B2 seed key', attribute='UserName') == 'key-id'


def test_create_refuses_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / 'kit.kdbx'
    _ = KdbxStore.create(path, PASSWORD)

    # Rotation writes a new file; the retired one outlives it (§2.2).
    with pytest.raises(KdbxError, match='already exists'):
        _ = KdbxStore.create(path, PASSWORD)


def test_writes_survive_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / 'kit.kdbx'
    KdbxStore.create(path, PASSWORD).put('seeds/Derivation seed', 'derivation-seed', 'AAAA')

    # Every put saves, so an interrupted bring-up keeps what it already wrote.
    reopened = KdbxStore(path=path)
    reopened.unlock_with(PASSWORD)
    assert reopened.get('seeds/Derivation seed') == 'AAAA'


def test_nested_groups_are_created_on_demand(store: KdbxStore) -> None:
    store.put('seeds/providers/oci/API key', 'ocid1', 'pem')

    assert store.entries('seeds/providers') == ['seeds/providers/oci/API key']


def test_entries_are_filtered_by_group(store: KdbxStore) -> None:
    store.put('seeds/one', 'a', 'x')
    store.put('other/two', 'b', 'y')

    assert store.entries('seeds') == ['seeds/one']
    assert store.entries() == ['other/two', 'seeds/one']


def test_put_is_idempotent(store: KdbxStore) -> None:
    store.put('seeds/one', 'a', 'x')
    store.put('seeds/one', 'a2', 'x2')

    assert store.entries('seeds') == ['seeds/one']
    assert store.get('seeds/one') == 'x2'
    assert store.get('seeds/one', attribute='UserName') == 'a2'


def test_missing_entry_names_itself(store: KdbxStore) -> None:
    with pytest.raises(KdbxError, match='no entry'):
        _ = store.get('seeds/absent')


def test_describe_omits_the_password(store: KdbxStore) -> None:
    store.put('seeds/one', 'a', 'the-secret')

    described = store.describe('seeds/one')

    assert described['Title'] == 'one'
    assert described['UserName'] == 'a'
    assert 'the-secret' not in described.values()


def test_wrong_password_is_reported_as_such(tmp_path: Path) -> None:
    path = tmp_path / 'kit.kdbx'
    _ = KdbxStore.create(path, PASSWORD)

    # A rotation must fail before it mints anything, not halfway through.
    with pytest.raises(KdbxError, match='could not unlock'):
        KdbxStore(path=path).unlock_with('wrong')


def test_a_remembered_password_skips_the_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'kit.kdbx'
    _ = KdbxStore.create(path, PASSWORD)

    store = KdbxStore(path=path)
    monkeypatch.setattr(KdbxStore, '_remembered', _stored(PASSWORD))
    monkeypatch.setattr('getpass.getpass', _refuse)

    store.unlock()
    assert store.entries() == []


def test_a_stale_stored_password_falls_through_to_the_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'kit.kdbx'
    _ = KdbxStore.create(path, PASSWORD)

    store = KdbxStore(path=path)
    # The database's password was changed since it was remembered: one typed
    # password, not a failed run.
    monkeypatch.setattr(KdbxStore, '_remembered', _stored('stale'))
    monkeypatch.setattr('getpass.getpass', _types(PASSWORD))

    store.unlock()
    assert store.entries() == []


def test_no_secret_store_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'kit.kdbx'
    _ = KdbxStore.create(path, PASSWORD)

    store = KdbxStore(path=path)

    # A headless machine has no Secret Service at all; that is the case the
    # prompt exists for, not a failure.
    def explode(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError('no backend')

    monkeypatch.setattr('keyring.get_password', explode)
    monkeypatch.setattr('getpass.getpass', _types(PASSWORD))

    store.unlock()
    assert store.entries() == []


def _refuse(_prompt: str) -> str:
    raise AssertionError('the operator was prompted despite a stored password')


def _stored(password: str | None) -> Callable[[KdbxStore], str | None]:
    return lambda _self: password


def _types(password: str) -> Callable[[str], str]:
    return lambda _prompt: password
