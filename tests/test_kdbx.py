"""The offline store, exercised against real databases in a temp directory.

Worth testing now that it is a library call rather than a `keepassxc-cli`
invocation: `bootstrap` and `rotate` both create a database and write every
seed into it (credentials.md §4), so a silent failure here is a kit that looks
written and is not.
"""

from __future__ import annotations

import logging
import types
from collections.abc import Callable
from pathlib import Path

import keyring.errors
import pytest

from kluster.scripts.credentials import workstation
from kluster.scripts.credentials.kdbx import KEYRING_SERVICE, PATH_ENV, KdbxError, KdbxStore

PASSWORD = 'correct horse battery staple'


@pytest.fixture
def store(tmp_path: Path) -> KdbxStore:
    return KdbxStore.create(tmp_path / 'kit.kdbx', PASSWORD)


@pytest.fixture
def secret_store(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """The desktop secret store as a dictionary, for the length of one test.

    A stand-in rather than the real thing: the tests below both read and write
    it, and a suite that writes to the operator's login keyring leaves entries
    behind on a machine it does not own.
    """
    stored: dict[tuple[str, str], str] = {}

    def get_password(service: str, account: str) -> str | None:
        return stored.get((service, account))

    def set_password(service: str, account: str, password: str) -> None:
        stored[(service, account)] = password

    def delete_password(service: str, account: str) -> None:
        if stored.pop((service, account), None) is None:
            raise keyring.errors.PasswordDeleteError(account)

    monkeypatch.setattr('keyring.get_password', get_password)
    monkeypatch.setattr('keyring.set_password', set_password)
    monkeypatch.setattr('keyring.delete_password', delete_password)
    monkeypatch.setattr('keyring.get_keyring', lambda: types.SimpleNamespace(name='a dictionary'))
    return stored


def test_created_database_round_trips_a_secret(store: KdbxStore) -> None:
    store.put('seeds/B2 seed key', 'key-id', 'key-secret')

    assert store.get('seeds/B2 seed key') == 'key-secret'
    assert store.get('seeds/B2 seed key', attribute='UserName') == 'key-id'


def test_the_kit_defaults_to_the_checkouts_own_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A checkout carries everything local it needs: no environment wiring is
    # the difference between a second machine working and a second machine
    # needing a note somewhere about which variable to set.
    monkeypatch.delenv(PATH_ENV, raising=False)
    monkeypatch.setattr(workstation, 'directory', lambda: tmp_path / '.credentials')
    _ = KdbxStore.create(workstation.kit_path(), PASSWORD)

    assert KdbxStore.from_env().path == tmp_path / '.credentials' / 'kit.kdbx'
    # Created by the kit itself, and no wider than the operator: everything
    # else in there is a workstation slot.
    assert (tmp_path / '.credentials').stat().st_mode & 0o777 == 0o700


def test_the_environment_overrides_the_default_kit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A kit on removable media, or shared between checkouts, is the case the
    # variable stays for.
    elsewhere = tmp_path / 'stick' / 'kluster.kdbx'
    _ = KdbxStore.create(elsewhere, PASSWORD)
    monkeypatch.setenv(PATH_ENV, str(elsewhere))
    monkeypatch.setattr(workstation, 'directory', lambda: tmp_path / '.credentials')

    assert KdbxStore.from_env().path == elsewhere


def test_create_refuses_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / 'kit.kdbx'
    _ = KdbxStore.create(path, PASSWORD)

    # Rotation writes a new file; the retired one outlives it (§2.2).
    with pytest.raises(KdbxError, match='already exists'):
        _ = KdbxStore.create(path, PASSWORD)


def test_writes_survive_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / 'kit.kdbx'
    KdbxStore.create(path, PASSWORD).put('seeds/Recovery key', 'age1identifier', 'AAAA')

    # Every put saves, so an interrupted bring-up keeps what it already wrote.
    reopened = KdbxStore(path=path)
    reopened.unlock_with(PASSWORD)
    assert reopened.get('seeds/Recovery key') == 'AAAA'


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


def test_the_secret_store_is_keyed_by_the_resolved_path(
    tmp_path: Path, secret_store: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / 'checkout' / '.credentials'
    real.mkdir(parents=True)
    kit = real / 'kit.kdbx'
    _ = KdbxStore.create(kit, PASSWORD)
    KdbxStore(path=kit).remember(PASSWORD)

    # One file, three spellings: as stored, through a symlinked checkout, and
    # relative to the directory the operator happens to be in. A store keyed
    # on the spelling makes each of them a separate entry, and every kit open
    # from a spelling that is not the stored one prompts again.
    link = tmp_path / 'link'
    link.symlink_to(tmp_path / 'checkout')
    monkeypatch.chdir(real)

    assert list(secret_store) == [(KEYRING_SERVICE, str(kit))]
    assert KdbxStore(path=link / '.credentials' / 'kit.kdbx')._remembered() == PASSWORD  # pyright: ignore[reportPrivateUsage]
    assert KdbxStore(path=Path('kit.kdbx'))._remembered() == PASSWORD  # pyright: ignore[reportPrivateUsage]


def test_a_kit_named_another_way_opens_without_a_prompt(
    tmp_path: Path, secret_store: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = tmp_path / 'checkout' / 'kit.kdbx'
    kit.parent.mkdir()
    _ = KdbxStore.create(kit, PASSWORD)
    KdbxStore(path=kit).remember(PASSWORD)
    link = tmp_path / 'link'
    link.symlink_to(tmp_path / 'checkout')
    monkeypatch.setattr('getpass.getpass', _refuse)

    store = KdbxStore(path=link / 'kit.kdbx')
    store.unlock()

    assert store.entries() == []
    assert len(secret_store) == 1


def test_forget_removes_the_entry_whichever_way_the_kit_is_named(
    tmp_path: Path, secret_store: dict[tuple[str, str], str]
) -> None:
    kit = tmp_path / 'checkout' / 'kit.kdbx'
    kit.parent.mkdir()
    _ = KdbxStore.create(kit, PASSWORD)
    KdbxStore(path=kit).remember(PASSWORD)
    link = tmp_path / 'link'
    link.symlink_to(tmp_path / 'checkout')

    KdbxStore(path=link / 'kit.kdbx').forget()

    assert not secret_store


def test_the_prompt_names_the_command_that_stops_it(
    tmp_path: Path,
    secret_store: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A ceremony is a dozen commands; a machine that has never opted in
    # prompts on each one, and nothing in the prompt says that is a choice.
    path = tmp_path / 'kit.kdbx'
    _ = KdbxStore.create(path, PASSWORD)
    monkeypatch.setattr('getpass.getpass', _types(PASSWORD))
    caplog.set_level(logging.INFO)

    KdbxStore(path=path).unlock()

    assert not secret_store
    assert 'credentials kdbx remember' in caplog.text


def _refuse(_prompt: str) -> str:
    raise AssertionError('the operator was prompted despite a stored password')


def _stored(password: str | None) -> Callable[[KdbxStore], str | None]:
    return lambda _self: password


def _types(password: str) -> Callable[[str], str]:
    return lambda _prompt: password
