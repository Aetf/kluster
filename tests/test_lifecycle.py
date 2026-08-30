"""Bootstrap and rotation, driven against real KeePass files.

The properties worth holding are the ones that only show up on the second
run or on the day something is lost: that an interrupted bootstrap resumes
instead of duplicating, that a rotation leaves the retired kit exactly as it
was, and that a credential no API can create stops the run with instructions
rather than being invented.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from kluster.scripts.credentials import age, entries, escrow, lifecycle, masters, workstation
from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore

PASSWORD = 'kit-password'

age_binary = shutil.which(age.BINARY)
needs_age = pytest.mark.skipif(age_binary is None, reason='age is not on PATH (mise x -- ...)')


def _answers(*values: str) -> Callable[[str], str]:
    """A prompt that hands back canned answers in order."""
    remaining = list(values)

    def prompt(_message: str) -> str:
        if not remaining:
            raise AssertionError('the run asked more questions than expected')
        return remaining.pop(0)

    return prompt


def _refuse(_message: str) -> str:
    raise AssertionError('the run prompted when it should not have')


@pytest.fixture
def kit(tmp_path: Path) -> KdbxStore:
    return KdbxStore.create(tmp_path / 'kit.kdbx', PASSWORD)


@pytest.fixture
def registry(tmp_path: Path) -> escrow.Registry:
    return escrow.Registry.open(tmp_path / 'escrow')


@needs_age
def test_the_recovery_key_is_generated_without_asking_anyone(kit: KdbxStore, registry: escrow.Registry) -> None:
    created = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)

    assert created == ['recovery']
    # Both halves land in one act: the private one in the kit, the public one
    # in the file every ciphertext is written to.
    assert kit.get(escrow.RECOVERY_ENTRY).startswith(age.SECRET_PREFIX)
    assert registry.recipients() == [kit.get(escrow.RECOVERY_ENTRY, attribute='UserName')]


@needs_age
def test_bootstrap_resumes_rather_than_repeating(kit: KdbxStore, registry: escrow.Registry) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    before = kit.get(escrow.RECOVERY_ENTRY)

    # The second run must not overwrite it: every ciphertext under escrow/
    # opens with this key and nothing else (§2.2).
    created = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)

    assert created == []
    assert kit.get(escrow.RECOVERY_ENTRY) == before


def test_a_console_only_seed_is_stored_from_what_the_operator_pastes(
    kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row of §2's shape rather than one of its members: the generic
    # console-only path -- an identifier typed, a secret pasted hidden, no key
    # file -- is what a seed on a platform with no API falls to, and it has to
    # keep working whether or not the register happens to hold such a row
    # today. Every member §2 does hold has a branch of its own in `create_seed`.
    seed = entries.Seed(
        member='example',
        title='Example console seed',
        identifier='the name the console shows it under',
        mints='a successor of its own class',
        mints_own_successor=False,
        console='the provider console → API tokens → New token.',
    )
    monkeypatch.setattr('getpass.getpass', _answers('a-pasted-secret'))

    lifecycle.create_seed(seed, kit=kit, prompt=_answers('an-identifier'))

    assert kit.get(seed.entry) == 'a-pasted-secret'
    assert kit.get(seed.entry, attribute='UserName') == 'an-identifier'


def test_a_key_file_is_stored_as_an_attachment(kit: KdbxStore, tmp_path: Path) -> None:
    pem = tmp_path / 'app.pem'
    _ = pem.write_bytes(b'-----BEGIN PRIVATE KEY-----\n')

    created = lifecycle.bootstrap(kit, prompt=_answers('Iv1.clientid', str(pem)), only='github-dispatch')

    assert created == ['github-dispatch']
    entry = entries.SEEDS['github-dispatch'].entry
    # §2.1: key material that is a file lives as an attachment, not in the
    # password field.
    assert kit.attachments(entry) == ['private-key.pem']
    assert kit.attachment(entry, 'private-key.pem').startswith(b'-----BEGIN')
    assert kit.get(entry, attribute='UserName') == 'Iv1.clientid'


def test_an_account_root_is_read_at_the_moment_it_is_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mint borrows its account root from the desktop secret store, or from
    # the operator when there is none (§2). No database but the kit is opened
    # for it, and nothing is read before the row that needs it is reached.
    def nothing_remembered(_account: str) -> str | None:
        return None

    monkeypatch.setattr('kluster.scripts.credentials.kdbx.remembered', nothing_remembered)
    monkeypatch.setattr('getpass.getpass', _answers('master-key'))

    credential = lifecycle.root('b2', _answers('account-id'))

    assert (credential['account-id'], credential['key']) == ('account-id', 'master-key')


def test_every_seed_that_needs_a_root_has_one_registered() -> None:
    # A minter whose account root is not in the register is one that can only
    # fail at the moment it is reached.
    for member, seed in entries.SEEDS.items():
        if member == 'recovery' or seed.manual:
            continue
        assert member in masters.ROOTS


def test_an_unknown_member_is_refused(kit: KdbxStore) -> None:
    with pytest.raises(KdbxError, match='no seed named'):
        _ = lifecycle.bootstrap(kit, prompt=_refuse, only='nonesuch')


def test_an_unknown_member_is_refused_before_the_successor_is_written(kit: KdbxStore, tmp_path: Path) -> None:
    # A rotation that matches no row would otherwise report an empty list as a
    # finished run, leaving a successor kit with nothing in it.
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)

    with pytest.raises(KdbxError, match='no seed named'):
        _ = lifecycle.rotate(kit, successor, prompt=_refuse, only='nonesuch')

    assert successor.entries() == []


@needs_age
def test_rotating_the_recovery_key_re_wraps_rather_than_re_generating(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path
) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    passphrase = escrow.generate(registry, escrow.PASSPHRASE)
    retired = kit.get(escrow.RECOVERY_ENTRY)

    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)
    rotated = lifecycle.rotate(kit, successor, prompt=_refuse, only='recovery', registry=registry)

    assert rotated == ['recovery']
    assert successor.get(escrow.RECOVERY_ENTRY) != retired
    # The plaintext is untouched, which is what makes this rotation free of
    # production consequences -- and the retired kit destroyable.
    assert escrow.Vault.open(successor, registry).recover(escrow.PASSPHRASE) == passphrase
    assert kit.get(escrow.RECOVERY_ENTRY) == retired


@needs_age
def test_the_environment_recovers_the_passphrase_and_reads_the_url(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path
) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    passphrase = escrow.generate(registry, escrow.PASSPHRASE)
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    _ = (bundle / 'backend-url').write_text('postgres://operator@192.0.2.10:5432/pulumi_state\n')

    found = lifecycle.environment(kit, bundle, registry)

    # The one place it exists outside its consumers is a committed ciphertext
    # nobody can open without the kit.
    assert found.passphrase == passphrase
    assert found.url is not None and found.url.startswith('postgres://operator@')


@needs_age
def test_a_bundle_left_where_it_used_to_live_is_read_once_and_loudly(
    kit: KdbxStore,
    registry: escrow.Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The bundle became a workstation slot inside the checkout; a machine that
    # still has one under ~/.config keeps working, and is told where it now
    # belongs rather than being left to wonder why nothing changed.
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    _ = escrow.generate(registry, escrow.PASSPHRASE)
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    _ = (legacy / lifecycle.URL_FILE).write_text('postgres://operator@192.0.2.10:5432/pulumi_state\n')
    slot = tmp_path / '.credentials' / 'state-backend'
    monkeypatch.setattr(workstation, 'LEGACY_BUNDLE_DIR', legacy)
    monkeypatch.setattr(workstation, 'bundle_dir', lambda: slot)

    found = lifecycle.environment(kit, slot, registry)

    assert found.url is not None and found.url.startswith('postgres://operator@')
    assert 'state-backend bundle operator' in caplog.text


@needs_age
def test_the_moved_bundle_is_only_looked_for_under_the_default(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `--bundle-dir somewhere-else` means that directory and no other: a
    # fallback that fired for an explicit path would answer a question the
    # operator did not ask.
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    _ = escrow.generate(registry, escrow.PASSPHRASE)
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    _ = (legacy / lifecycle.URL_FILE).write_text('postgres://operator@192.0.2.10:5432/pulumi_state\n')
    monkeypatch.setattr(workstation, 'LEGACY_BUNDLE_DIR', legacy)
    monkeypatch.setattr(workstation, 'bundle_dir', lambda: tmp_path / '.credentials' / 'state-backend')

    found = lifecycle.environment(kit, tmp_path / 'asked-for', registry)

    assert found.url is None


@needs_age
def test_a_missing_bundle_still_yields_the_passphrase(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path
) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    _ = escrow.generate(registry, escrow.PASSPHRASE)

    found = lifecycle.environment(kit, tmp_path / 'absent', registry)

    # The appliance not existing yet is the normal case during bring-up.
    assert found.passphrase
    assert found.url is None
