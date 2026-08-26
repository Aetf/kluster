"""The account roots: stored once, read on every use, prompted for when absent.

Driven against a real `keyring` backend held in memory rather than against a
desktop session, so the code under test is the code that runs on a workstation
— `keyring` resolves the backend the same way either way.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.backends.fail
import keyring.errors
import pytest

from kluster.scripts.credentials import kdbx, masters
from kluster.scripts.credentials.kdbx import KdbxError


class MemoryKeyring(keyring.backend.KeyringBackend):
    """A Secret Service that lives for the length of one test."""

    # `keyring`'s own backends declare this the same way; the base class makes
    # it a class property, which a plain value cannot match by type.
    priority: float = 1  # pyright: ignore[reportIncompatibleVariableOverride]

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError(username)
        del self.items[(service, username)]


def _current() -> keyring.backend.KeyringBackend:
    """Whatever backend this machine resolves to, or none at all.

    Resolution itself raises where a Secret Service is configured but not
    running, which is the state a test runner is usually in.
    """
    try:
        return keyring.get_keyring()
    except Exception:  # noqa: BLE001 - an unresolvable backend is "no backend"
        return keyring.backends.fail.Keyring()


@pytest.fixture
def store() -> Iterator[MemoryKeyring]:
    previous = _current()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def headless() -> Iterator[None]:
    """A machine with no secret store at all — CI, or a server over SSH."""
    previous = _current()
    keyring.set_keyring(keyring.backends.fail.Keyring())
    yield
    keyring.set_keyring(previous)


def _answers(*values: str) -> Callable[[str], str]:
    remaining = list(values)

    def prompt(_message: str) -> str:
        if not remaining:
            raise AssertionError('the run asked more questions than expected')
        return remaining.pop(0)

    return prompt


def _refuse(_message: str) -> str:
    raise AssertionError('the run prompted when it should not have')


def test_every_root_has_fields_and_console_steps() -> None:
    # A root with no fields is one the scripts cannot ask for; a root with no
    # console steps is one a headless prompt cannot explain (§2).
    for root in masters.ROOTS.values():
        assert root.fields
        assert root.console
        assert len({field.name for field in root.fields}) == len(root.fields)


def test_a_remembered_root_is_read_without_asking(store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))
    _ = masters.remember(masters.ROOTS['b2'], _answers('account-id'))

    # Every later use -- bootstrap, a rotation, a minter -- goes through this.
    credential = masters.load(masters.ROOTS['b2'], _refuse)

    assert credential['key'] == 'master-key'
    assert store.items[(kdbx.KEYRING_SERVICE, 'account-root/b2/key')] == 'master-key'


def test_a_root_the_store_does_not_have_is_asked_for(headless: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # The fallback is what keeps a headless machine working: no backend is not
    # an error, it is the case the prompt exists for.
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))

    credential = masters.load(masters.ROOTS['b2'], _answers('account-id'))

    assert credential['account-id'] == 'account-id'
    assert credential['key'] == 'master-key'


def test_the_fallback_prompt_says_where_the_credential_comes_from(
    headless: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))

    _ = masters.load(masters.ROOTS['b2'], _answers('account-id'))

    # A headless run is exactly the case where the operator cannot open the
    # app and look, so the console steps travel with the prompt.
    assert 'Application Keys' in caplog.text
    assert 'credentials master b2 remember' in caplog.text


def test_a_half_remembered_root_asks_only_for_the_rest(store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    store.items[(kdbx.KEYRING_SERVICE, 'account-root/b2/account-id')] = 'stored-account'
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))

    credential = masters.load(masters.ROOTS['b2'], _refuse)

    assert credential['account-id'] == 'stored-account'
    assert credential['key'] == 'master-key'


def test_a_file_field_is_read_from_the_path_it_is_given(
    store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem = tmp_path / 'oci.pem'
    _ = pem.write_text('-----BEGIN PRIVATE KEY-----\n')
    monkeypatch.setattr('getpass.getpass', _refuse)

    _ = masters.remember(masters.ROOTS['oci'], _answers('ocid1.tenancy.oc1..aaa', 'ocid1.user.oc1..bbb', str(pem)))

    # A PEM is not something anyone pastes into a prompt; what is stored is
    # its content, so the file is needed once and never again.
    credential = masters.load(masters.ROOTS['oci'], _refuse)
    assert credential['private-key'].startswith('-----BEGIN')
    assert credential['tenancy'] == 'ocid1.tenancy.oc1..aaa'
    assert credential['user'] == 'ocid1.user.oc1..bbb'


def test_stored_reports_each_field_without_disclosing_it(store: MemoryKeyring) -> None:
    store.items[(kdbx.KEYRING_SERVICE, 'account-root/oci/tenancy')] = 'ocid1.tenancy.oc1..aaa'

    assert masters.stored(masters.ROOTS['oci']) == {'tenancy': True, 'user': False, 'private-key': False}


def test_forget_removes_every_field(store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))
    _ = masters.remember(masters.ROOTS['b2'], _answers('account-id'))

    masters.forget(masters.ROOTS['b2'])

    assert store.items == {}


def test_forgetting_a_root_that_is_not_there_says_so(store: MemoryKeyring) -> None:
    with pytest.raises(KdbxError, match='not in the secret store'):
        masters.forget(masters.ROOTS['b2'])


def test_an_empty_answer_is_refused(headless: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # Storing an empty string would make the next run skip the prompt and
    # fail against the provider instead.
    monkeypatch.setattr('getpass.getpass', _answers(''))

    with pytest.raises(KdbxError, match='required'):
        _ = masters.load(masters.ROOTS['b2'], _answers('account-id'))


def test_the_kit_password_and_the_roots_share_one_store(store: MemoryKeyring) -> None:
    # One mechanism, not two: the database password and the account roots are
    # keyed apart by prefix rather than by living in different places.
    kdbx.store('/kit.kdbx', 'kit-password')
    kdbx.store('account-root/b2/key', 'master-key')

    assert kdbx.remembered('/kit.kdbx') == 'kit-password'
    assert kdbx.remembered('account-root/b2/key') == 'master-key'
    assert set(store.items) == {
        (kdbx.KEYRING_SERVICE, '/kit.kdbx'),
        (kdbx.KEYRING_SERVICE, 'account-root/b2/key'),
    }
