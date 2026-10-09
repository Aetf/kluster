"""The account roots and the one chain that finds them.

Every root is looked up the same way — desktop secret store, token file,
environment variable, prompt — so the tests are mostly about *order*: which
layer answers when more than one could, and what a run asks for when the
layers between them hold half a root.

Driven against a real `keyring` backend held in memory rather than against a
desktop session, so the code under test is the code that runs on a workstation
— `keyring` resolves the backend the same way either way.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import keyring.backends.fail
import pytest
from memory_keyring import MemoryKeyring, installed

from kluster.scripts.credentials import cli, devices, kdbx, masters, workstation
from kluster.scripts.credentials.kdbx import KdbxError


@pytest.fixture(autouse=True)
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The file layer, moved out of the checkout.

    `workstation` resolves `.credentials/` from its own `__file__`, so without
    this a test would read — and `forget` would delete — whatever the operator
    running it happens to keep there. The environment layer needs nothing here:
    `root_credentials` empties it for the whole process, which is why the cases
    below that want a variable set it themselves.
    """
    directory = tmp_path / '.credentials'
    monkeypatch.setattr(workstation, 'directory', lambda: directory)
    return directory


@pytest.fixture
def store() -> Iterator[MemoryKeyring]:
    backend = MemoryKeyring()
    with installed(backend):
        yield backend


@pytest.fixture
def headless() -> Iterator[None]:
    """A machine with no secret store at all — CI, or a server over SSH."""
    with installed(keyring.backends.fail.Keyring()):
        yield


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
    assert masters.ROOTS
    for root in masters.ROOTS.values():
        assert root.fields
        assert root.console
        assert len({field.name for field in root.fields}) == len(root.fields)


def test_every_field_names_its_file_and_its_variable() -> None:
    # The chain is register-driven: a field with no file name and no variable
    # name has two of its four layers missing, and nothing would say so.
    assert masters.ROOTS
    files = [field.file for root in masters.ROOTS.values() for field in root.fields]
    variables = [field.env for root in masters.ROOTS.values() for field in root.fields]

    assert all(files) and len(set(files)) == len(files)
    assert all(variables) and len(set(variables)) == len(variables)


def test_a_root_is_only_an_account_a_script_authenticates_as() -> None:
    """The membership rule, which is what keeps a provider credential out of here.

    A console credential the system can hold instead belongs to §3 and is
    delivered into the stack that reads it (`devices.py`); it is a root only
    where a script has to act as the account itself. The GitHub admin token is
    the worked example of the wrong side of that line -- it is a provider
    credential, so it lives in the `github` stack's config and nowhere here --
    and a root re-added for it would give one credential two homes.
    """
    # Symbolically: the GitHub admin token is a row of the console-made
    # register and not a field of any root. Naming its config key here instead
    # would only restate the line that declares it.
    assert devices.GITHUB_ADMIN in devices.DEVICES
    assert not any(devices.GITHUB_ADMIN.startswith(member) for member in masters.ROOTS)


def test_a_remembered_root_is_read_without_asking(store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))
    _ = masters.remember(masters.ROOTS[masters.B2], _answers('account-id'))

    # Every later use -- bootstrap, a rotation, a minter -- goes through this.
    credential = masters.load(masters.ROOTS[masters.B2], _refuse)

    assert credential[masters.B2_KEY] == 'master-key'
    assert store.items[(kdbx.KEYRING_SERVICE, 'account-root/b2/key')] == 'master-key'


def test_a_root_the_store_does_not_have_is_asked_for(headless: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # The fallback is what keeps a headless machine working: no backend is not
    # an error, it is the case the prompt exists for.
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))

    credential = masters.load(masters.ROOTS[masters.B2], _answers('account-id'))

    assert credential[masters.B2_ACCOUNT_ID] == 'account-id'
    assert credential[masters.B2_KEY] == 'master-key'


def test_the_fallback_prompt_says_where_the_credential_comes_from(
    headless: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))

    _ = masters.load(masters.ROOTS[masters.B2], _answers('account-id'))

    # A headless run is exactly the case where the operator cannot open the
    # app and look, so the console steps travel with the prompt.
    root = masters.ROOTS[masters.B2]
    for line in root.console.splitlines():
        assert line in caplog.text
    (quoted,) = re.findall(r'`credentials ([^`]+)`', caplog.text)
    parsed = vars(cli.build_parser().parse_args(quoted.split()))
    assert (parsed['subject'], parsed['member'], parsed['action']) == ('root', root.member, 'remember')


def test_a_half_remembered_root_asks_only_for_the_rest(store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    store.items[(kdbx.KEYRING_SERVICE, 'account-root/b2/account-id')] = 'stored-account'
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))

    credential = masters.load(masters.ROOTS[masters.B2], _refuse)

    assert credential[masters.B2_ACCOUNT_ID] == 'stored-account'
    assert credential[masters.B2_KEY] == 'master-key'


def test_the_secret_store_wins_over_the_file_and_the_file_over_the_variable(
    store: MemoryKeyring, local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole chain, on one field at a time, with every layer holding a
    # different value: the order is the point, so a test that only ever has
    # one layer filled proves nothing about it.
    store.items[(kdbx.KEYRING_SERVICE, 'account-root/b2/account-id')] = 'from-the-store'
    _ = workstation.write(local / 'roots' / 'b2.account-id', 'from-the-file')
    _ = workstation.write(local / 'roots' / 'b2.key', 'from-the-file')
    monkeypatch.setenv('KLUSTER_B2_ACCOUNT_ID', 'from-the-environment')
    monkeypatch.setenv('KLUSTER_B2_KEY', 'from-the-environment')

    credential = masters.load(masters.ROOTS[masters.B2], _refuse)

    assert credential[masters.B2_ACCOUNT_ID] == 'from-the-store'
    assert credential[masters.B2_KEY] == 'from-the-file'


def test_the_variable_answers_when_neither_the_store_nor_the_file_does(
    headless: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The layer a CI job and a one-off shell use: handed in, written nowhere.
    monkeypatch.setenv('KLUSTER_B2_ACCOUNT_ID', 'from-the-environment')
    monkeypatch.setenv('KLUSTER_B2_KEY', 'from-the-environment')

    credential = masters.load(masters.ROOTS[masters.B2], _refuse)

    assert credential[masters.B2_KEY] == 'from-the-environment'


def test_a_root_half_held_by_two_layers_asks_for_nothing(
    store: MemoryKeyring, local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Layers are consulted per field, so a root can be spread across them --
    # the OCI PEM in a file, its OCIDs in the store -- and still be complete.
    store.items[(kdbx.KEYRING_SERVICE, 'account-root/oci/tenancy')] = 'ocid1.tenancy.oc1..aaa'
    store.items[(kdbx.KEYRING_SERVICE, 'account-root/oci/user')] = 'ocid1.user.oc1..bbb'
    pem = '-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n'
    _ = workstation.write(local / 'roots' / 'oci.private-key', pem)
    monkeypatch.setattr('getpass.getpass', _refuse)

    credential = masters.load(masters.ROOTS[masters.OCI], _refuse)

    # A PEM's line structure is the value, so the file layer hands it back
    # exactly as written rather than stripped like a pasted token.
    assert credential[masters.OCI_PRIVATE_KEY] == pem


def test_a_half_written_file_is_treated_as_absent(headless: None, monkeypatch: pytest.MonkeyPatch, local: Path) -> None:
    # An empty file costs a prompt; storing the empty string would instead
    # make the next run skip the prompt and fail against the provider.
    _ = workstation.write(local / 'roots' / 'b2.key', '\n')
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))

    credential = masters.load(masters.ROOTS[masters.B2], _answers('account-id'))

    assert credential[masters.B2_KEY] == 'master-key'


def test_remember_falls_back_to_the_file_where_there_is_no_secret_store(
    headless: None, local: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # `remember` has to mean something on a headless box too: the file layer
    # is the one place such a machine can keep a root at all.
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))

    _ = masters.remember(masters.ROOTS[masters.B2], _answers('account-id'))

    assert (local / 'roots' / 'b2.key').read_text() == 'master-key\n'
    assert 'no desktop secret store' in caplog.text
    assert masters.load(masters.ROOTS[masters.B2], _refuse)[masters.B2_KEY] == 'master-key'


def test_remember_keeps_a_root_in_the_secret_store_alone_where_there_is_one(
    store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One layer, not two: the store is the one a backup cannot copy, so a
    # plaintext file written beside it would undo the reason for using it.
    root = masters.ROOTS[masters.B2]
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))

    _ = masters.remember(root, _answers('account-id'))

    assert masters.stored(root) == {field.name: masters.STORE for field in root.fields}
    assert [field.file for field in root.fields if workstation.root_path(field.file).exists()] == []


def test_forget_removes_the_token_file_as_well_as_the_store_entry(
    store: MemoryKeyring, local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))
    _ = masters.remember(masters.ROOTS[masters.B2], _answers('account-id'))
    # Both writable layers carry something, which is what makes the assertions
    # below distinguish a `forget` that clears one from a `forget` that clears
    # the machine: `remember` used the store, so the file is written here.
    _ = workstation.write(workstation.root_path('b2.key'), 'master-key')

    masters.forget(masters.ROOTS[masters.B2])

    # Forgetting a root leaves nothing behind on the machine; the environment
    # layer is the caller's shell and not this command's to unset.
    assert not (local / 'roots' / 'b2.key').exists()
    assert store.items == {}


def test_a_file_field_is_read_from_the_path_it_is_given(
    store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem = tmp_path / 'oci.pem'
    _ = pem.write_text('-----BEGIN PRIVATE KEY-----\n')
    monkeypatch.setattr('getpass.getpass', _refuse)

    _ = masters.remember(
        masters.ROOTS[masters.OCI], _answers('ocid1.tenancy.oc1..aaa', 'ocid1.user.oc1..bbb', str(pem))
    )

    # A PEM is not something anyone pastes into a prompt; what is stored is
    # its content, so the file is needed once and never again.
    credential = masters.load(masters.ROOTS[masters.OCI], _refuse)
    assert credential[masters.OCI_PRIVATE_KEY].startswith('-----BEGIN')
    assert credential[masters.OCI_TENANCY] == 'ocid1.tenancy.oc1..aaa'
    assert credential[masters.OCI_USER] == 'ocid1.user.oc1..bbb'


def test_stored_reports_the_layer_of_each_field_without_disclosing_it(
    store: MemoryKeyring, local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.items[(kdbx.KEYRING_SERVICE, 'account-root/oci/tenancy')] = 'ocid1.tenancy.oc1..aaa'
    _ = workstation.write(local / 'roots' / 'oci.user', 'ocid1.user.oc1..bbb')
    monkeypatch.setenv('KLUSTER_OCI_PRIVATE_KEY', '-----BEGIN PRIVATE KEY-----\n')

    # Which layer answered is the useful half of "will this run ask me
    # anything": a field the environment is holding up is one that disappears
    # with the shell it was exported in.
    assert masters.stored(masters.ROOTS[masters.OCI]) == {
        masters.OCI_TENANCY: masters.STORE,
        masters.OCI_USER: masters.FILE,
        masters.OCI_PRIVATE_KEY: masters.ENVIRONMENT,
    }
    b2 = masters.ROOTS[masters.B2]
    assert masters.stored(b2) == {field.name: None for field in b2.fields}


def test_forget_removes_every_field(store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))
    _ = masters.remember(masters.ROOTS[masters.B2], _answers('account-id'))

    masters.forget(masters.ROOTS[masters.B2])

    assert store.items == {}


def test_forgetting_a_root_that_is_not_there_says_so(store: MemoryKeyring) -> None:
    with pytest.raises(KdbxError, match='not in the secret store'):
        masters.forget(masters.ROOTS[masters.B2])


def test_an_empty_answer_is_refused(headless: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # Storing an empty string would make the next run skip the prompt and
    # fail against the provider instead.
    monkeypatch.setattr('getpass.getpass', _answers(''))

    with pytest.raises(KdbxError, match='required'):
        _ = masters.load(masters.ROOTS[masters.B2], _answers('account-id'))


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
