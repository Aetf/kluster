"""Bootstrap and rotation, driven against real KeePass files.

The properties worth holding are the ones that only show up on the second
run or on the day something is lost: that an interrupted bootstrap resumes
instead of duplicating, that an interrupted rotation resumes into the
successor it was writing and mints nothing twice, that a rotation leaves the
retired kit exactly as it was, and that a credential no API can create stops
the run with instructions rather than being invented.
"""

from __future__ import annotations

import functools
import io
import re
import shutil
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import keyring.backends.fail
import oci_clock
import pinned_tools
import pytest
import requests
from b2_api import ACCOUNT_ID as B2_ACCOUNT
from b2_api import FakeApi as B2Api
from cloudflare_api import ACCOUNT_ID as CLOUDFLARE_ACCOUNT
from cloudflare_api import MINTING_POLICY, console_seed
from cloudflare_api import FakeApi as CloudflareApi
from credentials_command_tree import named_commands, named_leaves
from memory_keyring import installed
from oci_clock import SimulatedClock
from oci_conventions import with_tenancy_ocid
from oci_tenancy import ROOT_USER, TENANCY, Tenancy

from kluster import conventions
from kluster.lib import stack_environment
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


@pytest.fixture
def pinned_age() -> None:
    """The pinned `age` and `age-keygen`, refused when either is missing rather than skipped."""
    pinned_tools.require(*pinned_tools.AGE)


needs_age = pytest.mark.usefixtures('pinned_age')


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


@pytest.fixture(autouse=True)
def unhurried(monkeypatch: pytest.MonkeyPatch) -> SimulatedClock:
    """`oci_iam`'s propagation waits, which every OCI mint here reaches, run on `oci_clock`'s clock."""
    return oci_clock.install(monkeypatch)


def test_the_propagation_waits_run_on_the_simulated_clock() -> None:
    assert oci_clock.one_refusal_outwaited() == [oci_iam.PROPAGATION_INTERVAL]


@pytest.fixture
def kit(tmp_path: Path) -> KdbxStore:
    return KdbxStore.create(tmp_path / 'kit.kdbx', PASSWORD)


@pytest.fixture
def registry(tmp_path: Path) -> escrow.Registry:
    return escrow.Registry.open(tmp_path / 'escrow')


def _unlocked(path: Path) -> KdbxStore:
    """An existing database, opened the way the command opens one."""
    store = KdbxStore(path=path)
    store.unlock_with(PASSWORD)
    return store


def _successor(path: Path) -> lifecycle.Successor:
    """Where a rotation under test writes: opened when the file exists, created when it does not."""
    return lifecycle.Successor(path=path, open=_unlocked, create=lambda path: KdbxStore.create(path, PASSWORD))


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


@pytest.mark.usefixtures('slots')
def test_an_account_root_is_read_at_the_moment_it_is_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mint borrows its account root from the desktop secret store, or from
    # the operator when there is none (§2). No database but the kit is opened
    # for it, and nothing is read before the row that needs it is reached.
    # The chain's file layer is `slots`', under `tmp_path`: in a checkout
    # whose own `.credentials/roots/` holds this account's root, the chain
    # would answer from there, and the assertion below would print it.
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


def _never(_path: Path) -> KdbxStore:
    raise AssertionError('the run touched its successor kit when it should have refused first')


def test_an_unknown_member_is_refused_before_the_successor_is_written(kit: KdbxStore, tmp_path: Path) -> None:
    # A rotation that matches no row would otherwise report an empty list as a
    # finished run, leaving a successor kit with nothing in it.
    untouched = lifecycle.Successor(path=tmp_path / 'successor.kdbx', open=_never, create=_never)

    with pytest.raises(KdbxError, match='no seed named'):
        _ = lifecycle.rotate(kit, untouched, prompt=_refuse, only='nonesuch')


@needs_age
def test_rotating_the_recovery_key_re_wraps_rather_than_re_generating(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path
) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    passphrase = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)
    retired = kit.get(escrow.RECOVERY_ENTRY)

    rotated = lifecycle.rotate(
        kit, _successor(tmp_path / 'successor.kdbx'), prompt=_refuse, only='recovery', registry=registry
    )

    successor = _unlocked(tmp_path / 'successor.kdbx')
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

    rotated = lifecycle.rotate(kit, _successor(tmp_path / 'successor.kdbx'), prompt=_refuse, registry=registry)

    successor = _unlocked(tmp_path / 'successor.kdbx')
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
    it would land with OCI's old-kit key already gone and a second run owed.
    Every account check therefore runs before any row rotates, and the
    property is that such a refusal costs nothing: no key retired, no row
    written, no successor file made, no console visit asked for.
    """
    whole = whole_kit(kit, registry, monkeypatch)
    # The one thing wrong with the kit: its B2 seed belongs to an account that
    # is not the one `conventions` records.
    monkeypatch.setattr(
        conventions, 'B2_ACCOUNT', conventions.B2Account(region='us-west-002', account_id='some-other-account')
    )
    successor = tmp_path / 'successor.kdbx'

    with pytest.raises(CredentialRejected, match=f'{B2_ACCOUNT}.*some-other-account'):
        _ = lifecycle.rotate(kit, _successor(successor), prompt=_refuse, registry=registry)

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
    predecessors -- a refusal there would cost a second run and a second
    console visit. The operator is on the page that fixes it, so the walk
    says why and asks again; the row is written once, with the token that was
    accepted.
    """
    whole = whole_kit(kit, registry, monkeypatch)
    accepted = console_seed(whole.dashboard)
    monkeypatch.setattr('getpass.getpass', _answers(_wrong_template(whole.dashboard), accepted))

    rotated = lifecycle.rotate(kit, _successor(tmp_path / 'successor.kdbx'), prompt=_refuse, registry=registry)

    successor = _unlocked(tmp_path / 'successor.kdbx')
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
    blind, accepted = console_seed(whole.dashboard), console_seed(whole.dashboard)
    pastes = _answers(blind, accepted)

    def paste(message: str) -> str:
        # The fake's zone listing is per account rather than per token, so
        # the first paste is made blind by the flag and the second is not.
        whole.dashboard.seed_sees_zones = not whole.dashboard.seed_sees_zones
        return pastes(message)

    whole.dashboard.seed_sees_zones = True
    monkeypatch.setattr('getpass.getpass', paste)

    _ = lifecycle.rotate(kit, _successor(tmp_path / 'successor.kdbx'), prompt=_refuse, registry=registry)

    assert _unlocked(tmp_path / 'successor.kdbx').get(entries.SEEDS['cloudflare'].entry) == accepted
    messages = [record.message for record in caplog.records]
    refused = next(i for i, message in enumerate(messages) if 'refused, and nothing was stored' in message)
    refusal, hint = messages[refused], messages[refused + 1]
    assert cloudflare.ZONE_VISIBILITY_PERMISSION in refusal
    assert 'paste the token again at this prompt' in hint
    for line in (refusal, hint):
        assert not re.search(r'seed \S+ create', line), line
        assert '`credentials ' not in line, line


def _dashboard_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The network dies for the Cloudflare API and no other platform."""
    routed = requests.get

    def unreachable(url: str, **request: Any) -> requests.Response:
        if url.startswith(cloudflare.API):
            raise requests.ConnectionError('the network')
        return routed(url, **request)

    monkeypatch.setattr(requests, 'get', unreachable)


@needs_age
def test_a_transport_failure_at_the_console_row_is_raised_not_asked_again(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-ask boundary is `CredentialRejected` and nothing wider: no dashboard page fixes the network."""
    whole = whole_kit(kit, registry, monkeypatch)
    monkeypatch.setattr('getpass.getpass', _answers(console_seed(whole.dashboard)))
    _dashboard_unreachable(monkeypatch)

    with pytest.raises(requests.ConnectionError):
        _ = lifecycle.rotate(kit, _successor(tmp_path / 'successor.kdbx'), prompt=_refuse, registry=registry)

    assert not _unlocked(tmp_path / 'successor.kdbx').has(entries.SEEDS['cloudflare'].entry)


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
    row itself and every row after it are not, and the same command with the
    same `--into` picks up at this row. The message is the only thing that
    tells the operator all of that -- and it arrives as the error the command
    prints, not as a traceback.
    """
    whole = whole_kit(kit, registry, monkeypatch)
    monkeypatch.setattr('getpass.getpass', _pastes(_wrong_template(whole.dashboard), '', stop))

    with pytest.raises(KdbxError, match=r'holds recovery, oci.*does not hold cloudflare.*no row after it') as caught:
        _ = lifecycle.rotate(kit, _successor(tmp_path / 'successor.kdbx'), prompt=_refuse, registry=registry)

    successor = _unlocked(tmp_path / 'successor.kdbx')
    assert isinstance(caught.value.__cause__, stop)
    # The consequences §4.2 promises the message says, held word for word:
    # what the retired kit can no longer do, what was not done, and how the
    # run goes on.
    assert 'holds recovery, oci, whose predecessors in the retired kit no longer work' in str(caught.value)
    assert 'does not hold cloudflare, whose row in the retired kit this run did not touch' in str(caught.value)
    assert 'no row after it was rotated' in str(caught.value)
    assert 're-run the same command with the same `--into` to resume' in str(caught.value)
    # Before the row: rotated, with the OCI user's one key the successor's.
    assert successor.has(entries.SEEDS['recovery'].entry)
    assert whole.tenancy.identity.keys[whole.user_id] == [
        oci_iam.fingerprint(oci_iam.load_seed(successor, entries.SEEDS['oci'].entry).private_key)
    ]
    # The row itself and the one after it: untouched in both kits and at the platform.
    assert not successor.has(entries.SEEDS['cloudflare'].entry)
    assert not successor.has(entries.SEEDS['b2'].entry)
    assert whole.b2_api.named(b2.SEED.name) == [whole.b2_key]


class Interrupted(Exception):
    """The run died here: a crash, not a refusal, so nothing catches it."""


def _rows(store: KdbxStore) -> dict[str, tuple[str, str]]:
    """Every seed row the store holds, by entry: its identifier and its secret (the OCI row's is its key file)."""
    rows: dict[str, tuple[str, str]] = {}
    for seed in entries.SEEDS.values():
        if not store.has(seed.entry):
            continue
        if seed.member == entries.OCI:
            secret = store.attachment(seed.entry, entries.OCI_KEY_ATTACHMENT).decode()
        else:
            secret = store.get(seed.entry)
        rows[seed.entry] = (store.get(seed.entry, attribute='UserName'), secret)
    return rows


def _oci_key(store: KdbxStore) -> str:
    return oci_iam.fingerprint(oci_iam.load_seed(store, entries.SEEDS['oci'].entry).private_key)


def _interrupt_mid_rewrap(patch: pytest.MonkeyPatch, _successor: Path) -> None:
    """Dies after the re-wrap has written one ciphertext and before the next.

    One file is under the successor key and the other under the retired one,
    `escrow/RECIPIENTS` still names the retired key, and the successor holds
    the only copy of the key the first file opens with.
    """
    encrypted = age.encrypt
    written: list[str] = []

    def one_then_die(plaintext: str, recipients: list[str]) -> str:
        if written:
            raise Interrupted('the second ciphertext was never written')
        written.append(plaintext)
        return encrypted(plaintext, recipients)

    patch.setattr(age, 'encrypt', one_then_die)


def _interrupt_after_oci_store(patch: pytest.MonkeyPatch, successor: Path) -> None:
    """Dies once the successor's OCI row is stored and before the run signs as it to sweep.

    The user holds the predecessor and the successor both, and the successor
    kit holds the only private half of the second. The first session opened
    after the row is stored is the sweep's, whatever the run authorized as
    before that.
    """
    authorized = oci_iam.Iam.authorize

    def until_stored(*args: Any, **kwargs: Any) -> oci_iam.Iam:
        if oci_iam.holds_seed(_unlocked(successor), entries.SEEDS['oci'].entry):
            raise Interrupted('died before the sweep as the successor')
        return authorized(*args, **kwargs)

    patch.setattr(oci_iam.Iam, 'authorize', until_stored)


def _interrupt_after_b2_put(patch: pytest.MonkeyPatch, _successor: Path) -> None:
    """Dies once the successor's B2 row is stored and before the predecessor is deleted."""

    def die(*_args: Any, **_kwargs: Any) -> None:
        raise Interrupted('died before retiring the predecessor')

    patch.setattr(b2, 'retire_others', die)


@dataclass(frozen=True)
class Interruption:
    """Where a first run dies, and which rows the successor holds when it has."""

    arrange: Callable[[pytest.MonkeyPatch, Path], None]
    held: tuple[str, ...]


INTERRUPTIONS: dict[str, Interruption] = {
    'mid-rewrap': Interruption(_interrupt_mid_rewrap, ('recovery',)),
    'after-oci-store': Interruption(_interrupt_after_oci_store, ('recovery', 'oci')),
    'at-cloudflare': Interruption(lambda patch, _successor: _dashboard_unreachable(patch), ('recovery', 'oci')),
    'after-b2-put': Interruption(_interrupt_after_b2_put, ('recovery', 'oci', 'cloudflare', 'b2')),
}


@needs_age
@pytest.mark.parametrize('interruption', list(INTERRUPTIONS), ids=list(INTERRUPTIONS))
def test_a_rotation_interrupted_at_any_row_resumes_into_the_same_successor(
    kit: KdbxStore,
    registry: escrow.Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    """Re-running the same command with the same `--into` ends where a clean run ends.

    Whatever row the first run died at, the second opens the successor it was
    writing, finishes every row already in it -- the key it stored is the one
    the platform is left with, and nothing is minted or pasted a second time
    -- and rotates the rest. The retired kit is read and never written.
    """
    whole = whole_kit(kit, registry, monkeypatch)
    passphrase = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)
    _ = escrow.generate(escrow.Vault.open(kit, registry), escrow.CA)
    retired = kit.path.read_bytes()
    successor = _successor(tmp_path / 'successor.kdbx')

    with pytest.MonkeyPatch.context() as first_run, pytest.raises((Interrupted, requests.ConnectionError)):
        INTERRUPTIONS[interruption].arrange(first_run, successor.path)
        _ = lifecycle.rotate(kit, successor, prompt=_refuse, registry=registry)
    died_at = _unlocked(successor.path)
    stored = _rows(died_at)
    assert set(stored) == {entries.SEEDS[member].entry for member in INTERRUPTIONS[interruption].held}
    stored_oci = _oci_key(died_at) if entries.SEEDS['oci'].entry in stored else None
    visits_before = len(whole.console_visits)

    rotated = lifecycle.rotate(kit, successor, prompt=_refuse, registry=registry)

    resumed = _unlocked(successor.path)
    assert rotated == list(entries.SEEDS)
    # Every row the first run stored is the row the successor ends with.
    assert {entry: row for entry, row in _rows(resumed).items() if entry in stored} == stored
    # And each platform is left with exactly the successor's key.
    assert whole.tenancy.identity.keys[whole.user_id] == [_oci_key(resumed)]
    assert stored_oci is None or _oci_key(resumed) == stored_oci
    assert escrow.Vault.open(resumed, registry).recover(escrow.PASSPHRASE) == passphrase
    assert registry.recipients() == [age.recipient(resumed.get(escrow.RECOVERY_ENTRY))]
    assert whole.b2_api.named(b2.SEED.name) == [resumed.get(entries.SEEDS['b2'].entry, attribute='UserName')]
    # The console is visited by the second run only where the first run never
    # stored the token -- once, whether or not it had asked before dying.
    asked_again = entries.SEEDS['cloudflare'].entry not in stored
    assert len(whole.console_visits) == visits_before + (1 if asked_again else 0)
    assert kit.path.read_bytes() == retired


@needs_age
def test_a_completed_rotation_re_run_is_a_no_op_at_every_platform(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole = whole_kit(kit, registry, monkeypatch)
    passphrase = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)
    successor = _successor(tmp_path / 'successor.kdbx')
    _ = lifecycle.rotate(kit, successor, prompt=_refuse, registry=registry)
    done = _unlocked(successor.path)
    rows, oci_key, b2_calls = _rows(done), _oci_key(done), len(whole.b2_api.calls)

    rotated = lifecycle.rotate(kit, successor, prompt=_refuse, registry=registry)

    again = _unlocked(successor.path)
    assert rotated == list(entries.SEEDS)
    assert _rows(again) == rows
    assert whole.tenancy.identity.keys[whole.user_id] == [oci_key]
    assert whole.b2_api.named(b2.SEED.name) == [rows[entries.SEEDS['b2'].entry][0]]
    assert 'b2_create_key' not in whole.b2_api.calls[b2_calls:]
    assert escrow.Vault.open(again, registry).recover(escrow.PASSPHRASE) == passphrase
    assert len(whole.console_visits) == 1


@needs_age
def test_the_pre_flight_reads_a_rotated_row_from_the_successor(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row already rotated into the successor is checked as the successor holds it.

    B2's account is knowable only by authorizing as the seed, and once the
    row has rotated the retired kit's key is one the account refuses: a
    pre-flight reading it there would refuse a sound resume before the walk.
    """
    whole = whole_kit(kit, registry, monkeypatch)
    successor = _successor(tmp_path / 'successor.kdbx')
    _ = lifecycle.rotate(kit, successor, prompt=_refuse, only='b2', registry=registry)
    b2_key = _unlocked(successor.path).get(entries.SEEDS['b2'].entry, attribute='UserName')

    rotated = lifecycle.rotate(kit, successor, prompt=_refuse, registry=registry)

    assert rotated == list(entries.SEEDS)
    assert whole.b2_api.named(b2.SEED.name) == [b2_key]
    assert _unlocked(successor.path).get(entries.SEEDS['b2'].entry, attribute='UserName') == b2_key


def _not_this_kits_successor(which: str, kit: KdbxStore, tmp_path: Path) -> Path:
    """An existing file that is not the successor `kit` was writing."""
    path = tmp_path / f'{which}.kdbx'
    match which:
        case 'the-kit-itself':
            return kit.path
        case 'a-copy':
            _ = shutil.copy(kit.path, path)
        case 'a-bootstrapped-kit':
            _ = lifecycle.bootstrap(
                KdbxStore.create(path, PASSWORD),
                prompt=_refuse,
                only='recovery',
                registry=escrow.Registry.open(tmp_path / 'another-escrow'),
            )
        case 'another-kits-successor':
            KdbxStore.create(path, PASSWORD).mark_successor_of(str(uuid4()))
        case _:  # pragma: no cover - the parametrization names each case
            raise AssertionError(which)
    return path


@needs_age
@pytest.mark.parametrize('which', ['the-kit-itself', 'a-copy', 'a-bootstrapped-kit', 'another-kits-successor'])
def test_an_existing_into_that_is_not_this_kits_successor_is_refused_before_anything(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    """The successor file names its predecessor, and `--into` is refused where it names another (§4.2).

    The kit itself and a copy of it share one database identity; a kit
    `bootstrap` wrote carries no marker; a retired kit of this installation,
    or a successor written from some other kit, carries a marker naming a kit
    that is not this one. Each is refused by name, before any key is retired or
    any console visit asked for, and the file is left byte for byte.
    """
    whole = whole_kit(kit, registry, monkeypatch)
    path = _not_this_kits_successor(which, kit, tmp_path)
    before = path.read_bytes()

    with pytest.raises(KdbxError, match=re.escape(str(path))) as refused:
        _ = lifecycle.rotate(kit, _successor(path), prompt=_refuse, registry=registry)

    assert 'resumes only into the' in str(refused.value) or 'is not the successor of' in str(refused.value)
    if which == 'another-kits-successor':
        # The one refusal that names what such a file is likely to be.
        assert 'a retired kit of this installation' in str(refused.value)
    assert path.read_bytes() == before
    assert whole.tenancy.identity.keys[whole.user_id] == [whole.oci_key]
    assert whole.b2_api.named(b2.SEED.name) == [whole.b2_key]
    assert whole.console_visits == []
    assert _rows(kit) == _rows(_unlocked(kit.path))


@needs_age
def test_a_successor_that_died_before_its_first_row_is_resumed_from_that_row(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The marker is written at creation and before any row, so a successor
    # that exists and is empty is this kit's and the re-run starts at the top.
    whole = whole_kit(kit, registry, monkeypatch)
    successor = _successor(tmp_path / 'successor.kdbx')

    def die(*_args: Any, **_kwargs: Any) -> None:
        raise Interrupted('died before the first row')

    with pytest.MonkeyPatch.context() as first_run, pytest.raises(Interrupted):
        first_run.setattr(escrow, 'rotate_recovery', die)
        _ = lifecycle.rotate(kit, successor, prompt=_refuse, registry=registry)
    assert _unlocked(successor.path).entries() == []

    rotated = lifecycle.rotate(kit, successor, prompt=_refuse, registry=registry)

    resumed = _unlocked(successor.path)
    assert rotated == list(entries.SEEDS)
    assert whole.tenancy.identity.keys[whole.user_id] == [_oci_key(resumed)]
    assert whole.b2_api.named(b2.SEED.name) == [resumed.get(entries.SEEDS['b2'].entry, attribute='UserName')]
    assert len(whole.console_visits) == 1


@needs_age
def test_a_successor_whose_marker_was_never_written_is_refused_naming_kit_ls(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An existing file with no marker: the refusal says how to tell a
    # successor that died before its marker (empty; deleted by hand) from a
    # kit that is simply not this one's successor (rows).
    whole = whole_kit(kit, registry, monkeypatch)
    path = tmp_path / 'successor.kdbx'
    _ = KdbxStore.create(path, PASSWORD)

    with pytest.raises(KdbxError, match='no lineage marker') as refused:
        _ = lifecycle.rotate(kit, _successor(path), prompt=_refuse, registry=registry)

    ((_, args),) = named_commands(str(refused.value))
    assert (args['kdbx'], args['subject'], args['member']) == (path, 'kit', 'ls')
    assert whole.tenancy.identity.keys[whole.user_id] == [whole.oci_key]
    assert whole.console_visits == []


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

    with pytest.raises(KdbxError, match=r'example.*not in the pre-flight'):
        lifecycle.prove_account(kit, minting)
    lifecycle.prove_account(kit, console_made)


@needs_age
def test_the_environment_recovers_the_passphrase_and_reads_the_url(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path
) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    passphrase = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    _ = (bundle / 'backend-url').write_text('postgres://operator@192.0.2.10:5432/pulumi_state\n')

    found = lifecycle.environment(kit, bundle, registry)

    # The one place it exists outside its consumers is a committed ciphertext
    # nobody can open without the kit.
    assert found.passphrase == passphrase
    assert found.url is not None and found.url.startswith('postgres://operator@')


@needs_age
def test_a_missing_bundle_still_yields_the_passphrase(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path
) -> None:
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    _ = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)

    found = lifecycle.environment(kit, tmp_path / 'absent', registry)

    # The appliance not existing yet is the normal case during bring-up.
    assert found.passphrase
    assert found.url is None


@pytest.fixture
def slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The chain's store and file layers, moved off the operator's: no store, and slots under `tmp_path`.

    Nobody is at a terminal either, so a run the chain does not answer is
    refused rather than left at a prompt.
    """
    directory = tmp_path / 'checkout' / '.credentials'
    monkeypatch.setattr(workstation, 'directory', lambda: directory)
    monkeypatch.setattr('sys.stdin', io.StringIO())
    with installed(keyring.backends.fail.Keyring()):
        yield directory


@needs_age
def test_an_operator_stack_is_given_the_passphrase_the_chain_finds(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, slots: Path
) -> None:
    """The `credentials` commands find the operator passphrase as the driver does, not by opening the escrow.

    The escrow holds a generation of its own here, different from the slot's,
    so a run that recovered it instead of asking the chain is told apart by
    the value.
    """
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    stack_passphrase = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)
    escrowed = escrow.generate(escrow.Vault.open(kit, registry), escrow.OPERATOR_PASSPHRASE)
    slots.mkdir(parents=True)
    _ = (slots / stack_environment.OPERATOR_PASSPHRASE_SLOT).write_text('from-the-slot\n')
    assert pulumi_config.APART, 'nothing to exercise: no stack is encrypted apart from the others'

    found = lifecycle.environment(kit, tmp_path / 'absent', registry)

    for stack in pulumi_config.APART:
        passphrase = found.variables(stack)[pulumi_config.PASSPHRASE_ENV]
        assert passphrase == 'from-the-slot'
        assert passphrase != escrowed
    # And every other stack is still on the stack passphrase.
    assert found.variables(conventions.STACK_NAMES.dns)[pulumi_config.PASSPHRASE_ENV] == stack_passphrase


@needs_age
def test_an_operator_stack_the_chain_does_not_answer_for_is_refused_naming_recover(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, slots: Path
) -> None:
    """The escrow is the recovery path the refusal names, not a place a run reads from on its own.

    The escrow holds a generation, which is the state of every machine that
    holds the kit; the chain holds nothing, so the run is refused and told
    what fills it.
    """
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    _ = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)
    _ = escrow.generate(escrow.Vault.open(kit, registry), escrow.OPERATOR_PASSPHRASE)
    assert not slots.exists()

    found = lifecycle.environment(kit, tmp_path / 'absent', registry)

    assert pulumi_config.APART, 'nothing to exercise: no stack is encrypted apart from the others'
    for stack, row in pulumi_config.APART.items():
        with pytest.raises(pulumi_config.PassphraseMissing) as refusal:
            _ = found.variables(stack)
        assert ('derived', row, 'recover') in named_leaves(str(refusal.value))


class _Terminal(io.StringIO):
    """Standard input that answers as a terminal does, so the chain's last layer asks."""

    def isatty(self) -> bool:
        return True


@needs_age
def test_a_command_asks_for_the_operator_passphrase_at_most_once(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, slots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every `pulumi` call a command makes builds its environment again; the chain is walked for the first alone.

    `Stack.env` is read once per `pulumi` call -- a `fill` makes three -- so a
    chain that ends at a prompt would otherwise ask on each of them, and each
    call would run under whatever was typed for it.
    """
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    _ = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)
    assert not slots.exists()
    monkeypatch.setattr('sys.stdin', _Terminal())
    asked: list[str] = []

    def typed(question: str) -> str:
        asked.append(question)
        return 'typed-at-the-terminal'

    monkeypatch.setattr('getpass.getpass', typed)
    assert pulumi_config.APART, 'nothing to exercise: no stack is encrypted apart from the others'
    stack = next(iter(pulumi_config.APART))

    found = lifecycle.environment(kit, tmp_path / 'absent', registry)
    given = [found.variables(stack)[pulumi_config.PASSPHRASE_ENV] for _ in range(3)]

    assert given == ['typed-at-the-terminal'] * 3
    assert len(asked) == 1


@needs_age
def test_physical_is_given_its_own_passphrase_recovered_with_the_kit(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path
) -> None:
    """`physical` opens under the newest generation of its own label, and no other stack does.

    Recovered only when `physical` is asked for and at most once, as the
    operator passphrase is found: a command that writes three keys into the
    stack builds its environment three times.
    """
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    stack_passphrase = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)
    _ = escrow.generate(escrow.Vault.open(kit, registry), escrow.PHYSICAL_PASSPHRASE)
    newest = escrow.generate(escrow.Vault.open(kit, registry), escrow.PHYSICAL_PASSPHRASE)

    found = lifecycle.environment(kit, tmp_path / 'absent', registry)
    given = [found.variables(pulumi_config.PHYSICAL)[pulumi_config.PASSPHRASE_ENV] for _ in range(3)]

    assert given == [newest] * 3
    assert newest != stack_passphrase
    assert found.variables(conventions.STACK_NAMES.dns)[pulumi_config.PASSPHRASE_ENV] == stack_passphrase


@needs_age
def test_physical_with_nothing_escrowed_is_refused_naming_the_command_that_files_it(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path
) -> None:
    """A kit with no generation of `physical`'s passphrase refuses that stack and only that stack.

    And never by handing it the stack passphrase, which every Environment a
    pull request can reach holds.
    """
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    stack_passphrase = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)

    found = lifecycle.environment(kit, tmp_path / 'absent', registry)

    assert found.variables(conventions.STACK_NAMES.dns)[pulumi_config.PASSPHRASE_ENV] == stack_passphrase
    with pytest.raises(pulumi_config.PassphraseMissing) as refusal:
        _ = found.variables(pulumi_config.PHYSICAL)
    assert ('derived', pulumi_config.PHYSICAL_ROW, 'generate') in named_leaves(str(refusal.value))
    assert 'The stack passphrase the previewed stacks share is deliberately not used' in str(refusal.value)


@needs_age
def test_physical_is_moved_from_the_stack_passphrase_or_an_earlier_generation_of_its_own(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `re-encrypt` may find the stack under: the stack passphrase first, then its own generations, newest first.

    The newest generation is what it is moved onto, so it is never among them.
    """
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    stack_passphrase = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)
    first, second, newest = (
        escrow.generate(escrow.Vault.open(kit, registry), escrow.PHYSICAL_PASSPHRASE) for _ in range(3)
    )
    seen: list[tuple[str, Sequence[str], str]] = []

    def moved(stack: pulumi_config.Stack, *, former: Sequence[str]) -> bool:
        seen.append((stack.name, former, stack.env[pulumi_config.PASSPHRASE_ENV]))
        return True

    monkeypatch.setattr(pulumi_config.Stack, 're_encrypt', moved)
    monkeypatch.setattr(pulumi_config, 'project_dir', lambda: tmp_path)

    assert lifecycle.re_encrypt_physical(kit, tmp_path / 'absent', registry)
    assert seen == [(pulumi_config.PHYSICAL, [stack_passphrase, second, first], newest)]


@needs_age
def test_the_stacks_under_the_stack_passphrase_are_moved_from_its_earlier_generations(
    kit: KdbxStore, registry: escrow.Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `pulumi-passphrase re-encrypt` hands the move: the newest generation, and every earlier one, newest first.

    The newest is what the environment gives the stacks, so it is never among
    the passphrases a stack may be moved from, and `physical`'s own
    passphrase is not among them either.
    """
    _ = lifecycle.bootstrap(kit, prompt=_refuse, only='recovery', registry=registry)
    first, second, newest = (escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE) for _ in range(3))
    seen: list[tuple[str | None, Sequence[str]]] = []

    def moving(environment: pulumi_config.BackendEnvironment, *, former: Sequence[str]) -> list[str]:
        seen.append((environment.passphrase, former))
        return list(pulumi_config.ON_STACK_PASSPHRASE)

    monkeypatch.setattr(pulumi_config, 're_encrypt_on_stack_passphrase', moving)

    assert lifecycle.re_encrypt_stacks(kit, tmp_path / 'absent', registry) == list(pulumi_config.ON_STACK_PASSPHRASE)
    assert seen == [(newest, [second, first])]
