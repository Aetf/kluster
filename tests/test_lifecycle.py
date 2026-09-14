"""Bootstrap and rotation, driven against real KeePass files.

The properties worth holding are the ones that only show up on the second
run or on the day something is lost: that an interrupted bootstrap resumes
instead of duplicating, that a rotation leaves the retired kit exactly as it
was, and that a credential no API can create stops the run with instructions
rather than being invented.
"""

from __future__ import annotations

import functools
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests
from b2_api import ACCOUNT_ID as B2_ACCOUNT
from b2_api import FakeApi as B2Api
from cloudflare_api import ACCOUNT_ID as CLOUDFLARE_ACCOUNT
from cloudflare_api import MINTING_POLICY
from cloudflare_api import FakeApi as CloudflareApi
from cloudflare_api import console_seed
from oci_conventions import with_tenancy_ocid
from test_oci_iam import ROOT_USER, TENANCY, Tenancy

from kluster import conventions
from kluster.scripts.credentials import (
    age,
    b2,
    cloudflare,
    entries,
    escrow,
    lifecycle,
    masters,
    oci_iam,
    pulumi_config,
    workstation,
)
from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore
from kluster.scripts.credentials.masters import CredentialRejected

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


def _pastes(*values: str | type[BaseException]) -> Callable[[str], str]:
    """`_answers` for a hidden prompt, where an answer may be the keystroke that ends it."""
    remaining = list(values)

    def prompt(_message: str) -> str:
        if not remaining:
            raise AssertionError('the run asked more questions than expected')
        answer = remaining.pop(0)
        if isinstance(answer, str):
            return answer
        raise answer

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


def _never() -> KdbxStore:
    raise AssertionError('the run made its successor kit when it should have refused first')


def test_an_unknown_member_is_refused_before_the_successor_is_written(kit: KdbxStore) -> None:
    # A rotation that matches no row would otherwise report an empty list as a
    # finished run, leaving a successor kit with nothing in it.
    with pytest.raises(KdbxError, match='no seed named'):
        _ = lifecycle.rotate(kit, _never, prompt=_refuse, only='nonesuch')


@needs_age
def test_rotating_the_recovery_key_re_wraps_rather_than_re_generating(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path
) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    passphrase = escrow.generate(registry, escrow.PASSPHRASE)
    retired = kit.get(escrow.RECOVERY_ENTRY)

    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)
    rotated = lifecycle.rotate(kit, lambda: successor, prompt=_refuse, only='recovery', registry=registry)

    assert rotated == ['recovery']
    assert successor.get(escrow.RECOVERY_ENTRY) != retired
    # The plaintext is untouched, which is what makes this rotation free of
    # production consequences -- and the retired kit destroyable.
    assert escrow.Vault.open(successor, registry).recover(escrow.PASSPHRASE) == passphrase
    assert kit.get(escrow.RECOVERY_ENTRY) == retired


@dataclass
class WholeKit:
    """A kit with every row the walk rotates, and the fake platforms behind them.

    A recovery key, an OCI seed in the fake tenancy `conventions` records, a B2
    seed in the fake account it records, and a Cloudflare dashboard that hands
    the walk a token it accepts. The kit's rotation runs entirely against the
    fakes: the tenancy reaches `rotate_seed` through the parameter `lifecycle`
    has no way to pass, and one `requests` module serves both HTTP platforms,
    so its `get` is routed to the fake the URL is for.
    """

    tenancy: Tenancy
    b2_api: B2Api
    dashboard: CloudflareApi
    user_id: str
    oci_key: str
    b2_key: str
    #: Every console prompt the walk made.
    console_visits: list[str]


def whole_kit(kit: KdbxStore, registry: escrow.Registry, monkeypatch: pytest.MonkeyPatch) -> WholeKit:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    tenancy = Tenancy()
    with_tenancy_ocid(monkeypatch, TENANCY)
    oci_root = masters.Credential(
        root=masters.ROOTS[masters.OCI],
        values={
            masters.OCI_TENANCY: TENANCY,
            masters.OCI_USER: ROOT_USER,
            masters.OCI_PRIVATE_KEY: oci_iam.generate_key().private_pem,
        },
    )
    user_id = oci_iam.create_seed(root=oci_root, seeds=kit, seed_entry=entries.SEEDS['oci'].entry, connect=tenancy)
    monkeypatch.setattr(oci_iam, 'rotate_seed', functools.partial(oci_iam.rotate_seed, connect=tenancy))
    b2_api = B2Api()
    dashboard = CloudflareApi()

    def get(url: str, **request: Any) -> requests.Response:
        return b2_api.get(url, **request) if url == b2.AUTHORIZE_URL else dashboard.get(url, **request)

    monkeypatch.setattr(requests, 'get', get)
    monkeypatch.setattr(requests, 'post', b2_api.post)
    monkeypatch.setattr(requests, 'request', dashboard.request)
    monkeypatch.setattr(conventions, 'B2_ACCOUNT', conventions.B2Account(region='us-west-002', account_id=B2_ACCOUNT))
    b2_root = masters.Credential(
        root=masters.ROOTS[masters.B2], values={'account-id': b2_api.master.key_id, 'key': b2_api.master.secret}
    )
    b2_key = b2.create_seed(root=b2_root, seeds=kit, seed_entry=entries.SEEDS['b2'].entry)
    _ = dashboard.add_zone(conventions.ZONE_PRIMARY)
    monkeypatch.setattr(conventions, 'CLOUDFLARE_ACCOUNT', conventions.CloudflareAccount(account_id=CLOUDFLARE_ACCOUNT))
    console_visits: list[str] = []

    def console(message: str) -> str:
        console_visits.append(message)
        return console_seed(dashboard)

    monkeypatch.setattr('getpass.getpass', console)
    return WholeKit(
        tenancy=tenancy,
        b2_api=b2_api,
        dashboard=dashboard,
        user_id=user_id,
        oci_key=oci_iam.fingerprint(oci_iam.load_seed(kit, entries.SEEDS['oci'].entry).private_key),
        b2_key=b2_key,
        console_visits=console_visits,
    )


@needs_age
def test_the_whole_kit_rotates_row_by_row_into_the_successor(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole = whole_kit(kit, registry, monkeypatch)
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)

    rotated = lifecycle.rotate(kit, lambda: successor, prompt=_refuse, registry=registry)

    # Every row of the register, in its order, and each row's successor is the
    # one credential its platform is left with -- the account checks up front
    # pass a correct kit through, and the console row was asked for once.
    assert rotated == list(entries.SEEDS)
    assert successor.entries() == sorted(seed.entry for seed in entries.SEEDS.values())
    assert whole.tenancy.identity.keys[whole.user_id] == [
        oci_iam.fingerprint(oci_iam.load_seed(successor, entries.SEEDS['oci'].entry).private_key)
    ]
    assert whole.b2_api.named(b2.SEED.name) == [successor.get(entries.SEEDS['b2'].entry, attribute='UserName')]
    assert len(whole.console_visits) == 1


@needs_age
def test_a_refusal_knowable_in_advance_rotates_nothing(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A B2 account refusal during `kit rotate` leaves every other row un-rotated.

    OCI rotates ahead of B2 in the walk and retires its predecessor the moment
    its successor is stored, so a refusal raised where B2's own rotation raises
    it would land with OCI's old-kit key already gone -- a state no re-run
    resumes from, because the successor file exists and the retired kit's OCI
    row no longer authenticates. Every account check therefore runs before any
    row rotates, and the property is that such a refusal costs nothing: no key
    retired, no row written, no successor file made, no console visit asked
    for.
    """
    whole = whole_kit(kit, registry, monkeypatch)
    # The one thing wrong with the kit: its B2 seed belongs to an account that
    # is not the one `conventions` records.
    monkeypatch.setattr(
        conventions, 'B2_ACCOUNT', conventions.B2Account(region='us-west-002', account_id='some-other-account')
    )
    successor = tmp_path / 'successor.kdbx'

    with pytest.raises(CredentialRejected, match=f'{B2_ACCOUNT}.*some-other-account'):
        _ = lifecycle.rotate(kit, lambda: KdbxStore.create(successor, PASSWORD), prompt=_refuse, registry=registry)

    # The OCI row is un-rotated: its old key is the one key on the user, and
    # the kit's row still holds it. Nothing else moved either.
    assert whole.tenancy.identity.keys[whole.user_id] == [whole.oci_key]
    assert oci_iam.fingerprint(oci_iam.load_seed(kit, entries.SEEDS['oci'].entry).private_key) == whole.oci_key
    assert whole.b2_api.named(b2.SEED.name) == [whole.b2_key]
    assert not successor.exists()
    assert whole.console_visits == []


def _wrong_template(dashboard: CloudflareApi) -> str:
    """A console-made token from the template as the dashboard offers it: zone work, no minting."""
    return dashboard.add('kluster-seed', [{**MINTING_POLICY, 'permission_groups': [{'id': 'g'}]}])


@needs_age
def test_a_refused_paste_at_the_console_row_is_asked_again(
    kit: KdbxStore,
    registry: escrow.Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Cloudflare token the dashboard made wrong is re-asked for, not raised.

    The token does not exist before the walk reaches its row, so no pre-flight
    can see it, and by then the recovery and OCI rows have retired their
    predecessors -- a refusal there would strand a run nothing resumes. The
    operator is on the page that fixes it, so the walk says why and asks
    again; the row is written once, with the token that was accepted.
    """
    whole = whole_kit(kit, registry, monkeypatch)
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)
    accepted = console_seed(whole.dashboard)
    monkeypatch.setattr('getpass.getpass', _answers(_wrong_template(whole.dashboard), accepted))

    rotated = lifecycle.rotate(kit, lambda: successor, prompt=_refuse, registry=registry)

    assert rotated == list(entries.SEEDS)
    assert successor.get(entries.SEEDS['cloudflare'].entry) == accepted
    assert successor.get(entries.SEEDS['cloudflare'].entry, attribute='UserName') == whole.dashboard.values[accepted]
    # The refusal names what the paste lacked, while the operator can still act on it.
    assert any(cloudflare.MINTING_PERMISSION in record.message for record in caplog.records)
    # And the walk went on: the row after the console one rotated as usual.
    assert whole.b2_api.named(b2.SEED.name) == [successor.get(entries.SEEDS['b2'].entry, attribute='UserName')]


@needs_age
def test_the_re_ask_at_the_console_row_names_no_command_but_the_paste(
    kit: KdbxStore,
    registry: escrow.Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """What a refused paste prints under `rotate` sends the operator back to the prompt and nowhere else.

    The refusal for a token that lists no zone is the one that used to end
    with a command, `seed cloudflare create` (Aetf/kluster-ops#347). Run while
    `rotate` waits at this prompt, that command writes the token into the
    retired kit, the default `--kdbx`, so neither the refusal nor the hint
    after it may name it: the refusal says what the token must carry, and the
    hint says the paste goes here.
    """
    whole = whole_kit(kit, registry, monkeypatch)
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)
    blind, accepted = console_seed(whole.dashboard), console_seed(whole.dashboard)
    pastes = _answers(blind, accepted)

    def paste(message: str) -> str:
        # The fake's zone listing is per account rather than per token, so
        # the first paste is made blind by the flag and the second is not.
        whole.dashboard.seed_sees_zones = not whole.dashboard.seed_sees_zones
        return pastes(message)

    whole.dashboard.seed_sees_zones = True
    monkeypatch.setattr('getpass.getpass', paste)

    _ = lifecycle.rotate(kit, lambda: successor, prompt=_refuse, registry=registry)

    assert successor.get(entries.SEEDS['cloudflare'].entry) == accepted
    messages = [record.message for record in caplog.records]
    refused = next(i for i, message in enumerate(messages) if 'refused, and nothing was stored' in message)
    refusal, hint = messages[refused], messages[refused + 1]
    assert cloudflare.ZONE_VISIBILITY_PERMISSION in refusal
    assert 'paste the new token at this prompt' in hint
    for line in (refusal, hint):
        assert not re.search(r'seed \S+ create', line), line
        assert '`credentials ' not in line, line


@needs_age
def test_a_transport_failure_at_the_console_row_is_raised_not_asked_again(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-ask boundary is `CredentialRejected` and nothing wider: no dashboard page fixes the network."""
    whole = whole_kit(kit, registry, monkeypatch)
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)
    monkeypatch.setattr('getpass.getpass', _answers(console_seed(whole.dashboard)))
    routed = requests.get

    def unreachable(url: str, **request: Any) -> requests.Response:
        if url.startswith(cloudflare.API):
            raise requests.ConnectionError('the network')
        return routed(url, **request)

    monkeypatch.setattr(requests, 'get', unreachable)

    with pytest.raises(requests.ConnectionError):
        _ = lifecycle.rotate(kit, lambda: successor, prompt=_refuse, registry=registry)

    assert not successor.has(entries.SEEDS['cloudflare'].entry)


@needs_age
@pytest.mark.parametrize('stop', [EOFError, KeyboardInterrupt], ids=['end-of-input', 'ctrl-c'])
def test_stopping_at_the_paste_says_what_each_kit_holds(
    kit: KdbxStore,
    registry: escrow.Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: type[BaseException],
) -> None:
    """Ctrl-C or end of input at the paste is the stop, and the error says where that leaves things.

    An empty paste is not: it is asked again, like a refused one. The rows
    before the console one have rotated and are in the successor; the console
    row itself and every row after it are not. That is the state a re-run
    does not resume from, so the message is the only thing that tells the
    operator -- and it arrives as the error the command prints, not as a
    traceback.
    """
    whole = whole_kit(kit, registry, monkeypatch)
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)
    monkeypatch.setattr('getpass.getpass', _pastes(_wrong_template(whole.dashboard), '', stop))

    with pytest.raises(KdbxError, match=r'holds recovery, oci.*does not hold cloudflare.*no row after it') as caught:
        _ = lifecycle.rotate(kit, lambda: successor, prompt=_refuse, registry=registry)

    assert isinstance(caught.value.__cause__, stop)
    # The two consequences §4.2 promises the message says, held word for
    # word: what the retired kit can no longer do, and what was not done.
    assert 'holds recovery, oci, whose predecessors in the retired kit no longer work' in str(caught.value)
    assert 'does not hold cloudflare, whose row in the retired kit this run did not touch' in str(caught.value)
    assert 'no row after it was rotated' in str(caught.value)
    # Before the row: rotated, with the OCI user's one key the successor's.
    assert successor.has(entries.SEEDS['recovery'].entry)
    assert whole.tenancy.identity.keys[whole.user_id] == [
        oci_iam.fingerprint(oci_iam.load_seed(successor, entries.SEEDS['oci'].entry).private_key)
    ]
    # The row itself and the one after it: untouched in both kits and at the platform.
    assert not successor.has(entries.SEEDS['cloudflare'].entry)
    assert not successor.has(entries.SEEDS['b2'].entry)
    assert whole.b2_api.named(b2.SEED.name) == [whole.b2_key]


def test_a_self_reproducing_family_with_no_account_check_is_refused_before_the_walk(kit: KdbxStore) -> None:
    # A row of §2's shape whose platform can mint its successor, and whose
    # account check nobody has added to the pre-flight: refused by name, where
    # walking past it would let its rotation retire behind a refusal that
    # should have come first. A console-made row has nothing to check up
    # front and is passed through.
    minting = entries.Seed(
        member='example',
        title='Example minting seed',
        identifier='its key id',
        mints='a successor of its own class',
        mints_own_successor=True,
    )
    console_made = entries.Seed(
        member='pasted',
        title='Example console seed',
        identifier='the name the console shows it under',
        mints='a successor of its own class',
        mints_own_successor=False,
        console='the provider console → API tokens → New token.',
    )

    with pytest.raises(KdbxError, match='example.*not in the pre-flight'):
        lifecycle.prove_account(kit, minting)
    lifecycle.prove_account(kit, console_made)


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


@needs_age
def test_the_environment_carries_a_passphrase_for_every_stack_encrypted_apart(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path
) -> None:
    """The joint between the census and the recovery, exercised end to end.

    `pulumi_config.APART` says which stacks are off the estate passphrase and
    which register row each one's own comes from; this is what turns that into
    values a `pulumi` run can be started with. A version that walked its own
    list instead would fail *closed* -- the stack it forgot refuses rather than
    running under the estate passphrase -- but it would refuse telling an
    operator to run a `generate` they have already run, and nothing else in the
    suite reaches `apart` through this function at all.
    """
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    _ = escrow.generate(registry, escrow.PASSPHRASE)
    generated = {
        stack: escrow.generate(registry, escrow.rows()[row].name) for stack, row in pulumi_config.APART.items()
    }
    assert generated, 'nothing to exercise: no stack is encrypted apart from the estate'

    found = lifecycle.environment(kit, tmp_path / 'absent', registry)

    assert found.apart == generated
    # And each reaches the stack it belongs to rather than the estate's, which
    # is the whole of what a caller gets out of this.
    for stack, passphrase in generated.items():
        assert found.variables(stack)[pulumi_config.PASSPHRASE_ENV] == passphrase
    assert found.passphrase is not None and found.passphrase not in generated.values()


@needs_age
def test_a_stack_encrypted_apart_whose_escrow_is_empty_is_left_for_the_refusal(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path
) -> None:
    """The state a machine is in between the row being declared and its `generate`.

    Raising here would name a label while building an environment most commands
    never point at that stack anyway; leaving it out lets the refusal come from
    the place that knows which stack was asked for and names the row to run.
    """
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    _ = escrow.generate(registry, escrow.PASSPHRASE)

    found = lifecycle.environment(kit, tmp_path / 'absent', registry)

    assert found.apart == {}
    for stack, row in pulumi_config.APART.items():
        with pytest.raises(pulumi_config.PassphraseMissing, match=f'credentials derived {row} generate'):
            _ = found.variables(stack)
