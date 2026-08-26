"""Bootstrap and rotation, driven against real KeePass files.

The properties worth holding are the ones that only show up on the second
run or on the day something is lost: that an interrupted bootstrap resumes
instead of duplicating, that a rotation leaves the retired kit exactly as it
was, and that a credential no API can create stops the run with instructions
rather than being invented.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kluster.scripts.credentials import entries, lifecycle, masters, seeds, workstation
from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore

PASSWORD = 'kit-password'


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


def test_the_derivation_seed_is_generated_without_asking_anyone(kit: KdbxStore) -> None:
    created = lifecycle.bootstrap(kit, prompt=_refuse, only='derivation')

    assert created == ['derivation']
    assert len(seeds.load_seed(kit)) == seeds.SEED_LENGTH


def test_bootstrap_resumes_rather_than_repeating(kit: KdbxStore) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='derivation')
    before = seeds.load_seed(kit)

    # The second run must not overwrite it: everything derived from the old
    # seed -- the backups above all -- would be orphaned (§2.2).
    created = lifecycle.bootstrap(kit, prompt=_refuse, only='derivation')

    assert created == []
    assert seeds.load_seed(kit) == before


def test_a_console_only_seed_is_stored_from_what_the_operator_pastes(
    kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('getpass.getpass', _answers('zt-token-value'))

    created = lifecycle.bootstrap(kit, prompt=_answers('zerotier-central'), only='zerotier')

    assert created == ['zerotier']
    entry = entries.SEEDS['zerotier'].entry
    assert kit.get(entry) == 'zt-token-value'
    assert kit.get(entry, attribute='UserName') == 'zerotier-central'


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
        if member == 'derivation' or seed.manual:
            continue
        assert member in masters.ROOTS


def test_an_unknown_member_is_refused(kit: KdbxStore) -> None:
    with pytest.raises(KdbxError, match='no seed named'):
        _ = lifecycle.bootstrap(kit, prompt=_refuse, only='nonesuch')


def test_rotation_writes_a_new_seed_and_leaves_the_retired_kit_untouched(kit: KdbxStore, tmp_path: Path) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='derivation')
    retired = seeds.load_seed(kit)

    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)
    rotated = lifecycle.rotate(kit, successor, prompt=_refuse, only='derivation')

    assert rotated == ['derivation']
    assert seeds.load_seed(successor) != retired
    # §2.2: the retired seed outlives its rotation, because backups encrypted
    # under it cannot be re-encrypted retroactively.
    assert seeds.load_seed(kit) == retired


def test_the_environment_derives_the_passphrase_and_reads_the_url(kit: KdbxStore, tmp_path: Path) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='derivation')
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    _ = (bundle / 'backend-url').write_text('postgres://operator@192.0.2.10:5432/pulumi_state\n')

    values = lifecycle.environment(kit, bundle)

    # Derived, never stored: the passphrase lives in no slot an operator can
    # read, which is why there was no way to obtain it before.
    assert values['PULUMI_CONFIG_PASSPHRASE'] == seeds.pulumi_passphrase(seeds.load_seed(kit))
    assert values['PULUMI_BACKEND_URL'].startswith('postgres://operator@')


def test_a_bundle_left_where_it_used_to_live_is_read_once_and_loudly(
    kit: KdbxStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The bundle became a workstation slot inside the checkout; a machine that
    # still has one under ~/.config keeps working, and is told where it now
    # belongs rather than being left to wonder why nothing changed.
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='derivation')
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    _ = (legacy / lifecycle.URL_FILE).write_text('postgres://operator@192.0.2.10:5432/pulumi_state\n')
    slot = tmp_path / '.credentials' / 'state-backend'
    monkeypatch.setattr(workstation, 'LEGACY_BUNDLE_DIR', legacy)
    monkeypatch.setattr(workstation, 'bundle_dir', lambda: slot)

    values = lifecycle.environment(kit, slot)

    assert values['PULUMI_BACKEND_URL'].startswith('postgres://operator@')
    assert 'state-backend bundle operator' in caplog.text


def test_the_moved_bundle_is_only_looked_for_under_the_default(
    kit: KdbxStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `--bundle-dir somewhere-else` means that directory and no other: a
    # fallback that fired for an explicit path would answer a question the
    # operator did not ask.
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='derivation')
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    _ = (legacy / lifecycle.URL_FILE).write_text('postgres://operator@192.0.2.10:5432/pulumi_state\n')
    monkeypatch.setattr(workstation, 'LEGACY_BUNDLE_DIR', legacy)
    monkeypatch.setattr(workstation, 'bundle_dir', lambda: tmp_path / '.credentials' / 'state-backend')

    values = lifecycle.environment(kit, tmp_path / 'asked-for')

    assert 'PULUMI_BACKEND_URL' not in values


def test_a_missing_bundle_still_yields_the_passphrase(kit: KdbxStore, tmp_path: Path) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='derivation')

    values = lifecycle.environment(kit, tmp_path / 'absent')

    # The appliance not existing yet is the normal case during bring-up.
    assert 'PULUMI_CONFIG_PASSPHRASE' in values
    assert 'PULUMI_BACKEND_URL' not in values
