"""The B2 credential family: what it mints, what it retires, what survives.

Driven against a fake of the API rather than the API: what is being checked is
that the minter asks for the right things, retires as a credential that
outlives the retirement, and stores what comes back — all of which is fixed,
where what Backblaze does with the request is not.

The fake refuses what the platform refuses (each endpoint's capability, a
token whose key is gone), which is what makes two of the properties below
mean anything: a seed key deleting itself ends its own session, and a key
listing that stops at the first page reports an account smaller than it is.

The last section is a fault sweep: every remote call of every stage, failed
both before and after the service acted, with the invariants that must hold
either way — the kit never names a credential the account would refuse, a
re-run heals, and no run leaves more than the one key it was interrupted
mid-mint of.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests
from b2_api import ACCOUNT_ID, FakeApi, Key
from fake_gh import RecordedGh
from memory_kit import MemoryKit

from kluster import conventions
from kluster.scripts.credentials import b2, cli, derived, entries, masters, payload
from kluster.scripts.credentials.github_secrets import Forge
from kluster.scripts.credentials.kdbx import KdbxStore
from kluster.scripts.credentials.masters import CredentialRejected
from kluster.scripts.credentials.pulumi_config import SlotRefused
from kluster.scripts.credentials.delivery import Delivery
from kluster.scripts.state_backend import settings as appliance_settings

PASSWORD = 'kit-password'
SEED_ENTRY = entries.SEEDS['b2'].entry

BUCKET = 'kluster-state'

#: The bucket's lifecycle rule and the uploader's key are confined to the same
#: prefix, and the one home for it is `conventions` -- so a suite that made one
#: up would be driving a bucket the key it mints cannot write into.
PREFIX = conventions.STATE_DUMP_PREFIX
RETENTION_DAYS = 30
#: The lifecycle rule the dump prefix wants, as B2 exchanges it.
RETENTION: dict[str, Any] = {
    'fileNamePrefix': f'{PREFIX}/',
    'daysFromUploadingToHiding': RETENTION_DAYS,
    'daysFromHidingToDeleting': 1,
}

LIST_FILE_NAMES = 'b2_list_file_names'


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(b2.requests, 'get', fake.get)
    monkeypatch.setattr(b2.requests, 'post', fake.post)
    return fake


def _record_account(patch: pytest.MonkeyPatch) -> None:
    """Make the fake platform's account the one `conventions` records.

    Every mint here proves the account before it writes, so a suite driving a
    fake account has to be that account for the ordinary path to be the one
    under test. A function as well as a fixture because the call measurement
    the fault sweep rests on runs at import, where no fixture has run yet.
    """
    patch.setattr(conventions, 'B2_ACCOUNT', conventions.B2Account(region='us-west-002', account_id=ACCOUNT_ID))


@pytest.fixture(autouse=True)
def recorded_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorded account, for every test but the ones that say otherwise."""
    _record_account(monkeypatch)


@pytest.fixture
def kit(tmp_path: Path) -> KdbxStore:
    return KdbxStore.create(tmp_path / 'kit.kdbx', PASSWORD)


def _root(api: FakeApi) -> masters.Credential:
    """The account master key, as `masters` hands it over."""
    return masters.Credential(
        root=masters.ROOTS['b2'],
        values={'account-id': api.master.key_id, 'key': api.master.secret},
    )


def _seeded(api: FakeApi, kit: KdbxStore) -> str:
    key_id = b2.create_seed(root=_root(api), seeds=kit, seed_entry=SEED_ENTRY)
    return key_id


def _delivered[T](pending: Delivery[T]) -> T:
    """The credential a register row is left holding once its push has returned.

    A row cannot read the credential directly (`delivery.py`), so this is how
    a test comes by one at all — and it is the route that retires, which is
    what these cases are about. The push is the identity function:
    what is under test is the order, and the slot this key goes into belongs to
    another module.
    """
    return pending.deliver(lambda credential: credential)[0]


def _session(api: FakeApi, kit: KdbxStore) -> b2.Session:
    return b2.Session.from_entry(kit, SEED_ENTRY)


# -- the seed ---------------------------------------------------------------


def test_the_row_holds_the_key_id_and_the_key(api: FakeApi, kit: KdbxStore) -> None:
    key_id = _seeded(api, kit)

    # UserName is the public half everywhere in the kit (§2); the secret is
    # the application key, which B2 shows once.
    assert kit.get(SEED_ENTRY, attribute='UserName') == key_id
    assert kit.get(SEED_ENTRY) == api.keys[key_id].secret
    assert api.keys[key_id].name == b2.SEED.name


def test_an_account_wide_role_confines_its_key_to_neither_a_bucket_nor_a_prefix(api: FakeApi, kit: KdbxStore) -> None:
    key_id = _seeded(api, kit)

    # B2 reads a `bucketId` that is merely present as a confinement, so a role
    # with no scope has to state none rather than state it empty -- a seed
    # confined to one bucket could administer nothing else in the account.
    (_, body), *_ = [call for call in api.posted if call[0] == 'b2_create_key']
    assert 'bucketId' not in body
    assert 'namePrefix' not in body
    assert (api.keys[key_id].bucket_id, api.keys[key_id].name_prefix) == (None, None)


def test_the_seed_carries_bucket_administration_and_no_file_capability(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)

    (_, body), *_ = [call for call in api.posted if call[0] == 'b2_create_key']
    # The credential that administers the backup buckets cannot read a byte
    # out of them, and can replace itself: that pair is the whole design.
    assert set(body['capabilities']) == set(b2.CAPABILITIES)
    assert not set(body['capabilities']) & {'readFiles', 'writeFiles', 'listFiles', 'deleteFiles'}
    assert {'writeKeys', 'deleteKeys'} <= set(body['capabilities'])


def test_a_rejected_credential_names_the_key_id_it_was_given(api: FakeApi) -> None:
    # The likely mistake is an account e-mail in the username field, and the
    # API's own 401 says nothing about which half was wrong.
    with pytest.raises(CredentialRejected, match='someone@example.com'):
        _ = b2.Session.authorize('someone@example.com', 'master-key')


def test_a_row_missing_a_half_is_refused_before_the_call(api: FakeApi, kit: KdbxStore) -> None:
    kit.put(SEED_ENTRY, '', 'secret-without-an-id')

    with pytest.raises(ValueError, match='key id as its username'):
        _ = b2.Session.from_entry(kit, SEED_ENTRY)


def test_the_minted_seed_is_proven_to_work_before_the_kit_names_it(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)

    # Mint, authorize *as the new key*, and make it do something. A key that
    # cannot authenticate must never become the row the next run reads.
    assert api.calls[:4] == ['b2_authorize_account', 'b2_create_key', 'b2_authorize_account', 'b2_list_buckets']


def test_creating_the_seed_retires_a_seed_key_left_behind(api: FakeApi, kit: KdbxStore) -> None:
    orphan = api.add_key(b2.SEED.name)

    key_id = _seeded(api, kit)

    # A key nobody holds the secret of is a live permission, not a spare: the
    # run that mints the seed is the one that can see it and clear it.
    assert api.named(b2.SEED.name) == [key_id]
    assert orphan.key_id not in api.keys


def test_rotation_stores_the_successor_and_retires_the_predecessor(api: FakeApi, kit: KdbxStore) -> None:
    previous = _seeded(api, kit)

    key_id = b2.rotate_seed(kit, seed_entry=SEED_ENTRY)

    assert key_id != previous
    assert previous not in api.keys
    assert kit.get(SEED_ENTRY, attribute='UserName') == key_id
    assert api.named(b2.SEED.name) == [key_id]


def test_rotation_writes_a_new_kit_and_leaves_the_retired_one_untouched(
    api: FakeApi, kit: KdbxStore, memory_kit: KdbxStore
) -> None:
    previous = _seeded(api, kit)

    key_id = b2.rotate_seed(kit, seed_entry=SEED_ENTRY, into=memory_kit)

    # §4.2: the retired kit stays exactly as it was; the key it holds keeps
    # working until its successor has been verified.
    assert memory_kit.get(SEED_ENTRY, attribute='UserName') == key_id
    assert kit.get(SEED_ENTRY, attribute='UserName') == previous


def test_rotation_retires_every_superseded_seed_key(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    orphan = api.add_key(b2.SEED.name)

    key_id = b2.rotate_seed(kit, seed_entry=SEED_ENTRY)

    # More than one key to delete, and one of them is the credential the
    # rotation started as: a retirement signed with the predecessor stops
    # working the moment it deletes the predecessor, so it runs as the
    # successor instead.
    assert api.named(b2.SEED.name) == [key_id]
    assert orphan.key_id not in api.keys


@pytest.mark.parametrize('predecessor_live', [True, False], ids=['predecessor-live', 'predecessor-retired'])
def test_rotation_into_a_kit_already_holding_the_successor_mints_nothing(
    api: FakeApi, kit: KdbxStore, memory_kit: KdbxStore, predecessor_live: bool
) -> None:
    previous = _seeded(api, kit)
    # A rotation that stored its successor and died in its retirement: the
    # successor kit holds the row, and the account holds the successor's key,
    # a stray of the seed's name, and the predecessor -- unless the retirement
    # had reached it, after which the retired kit's row no longer authorizes.
    stored = api.add_key(b2.SEED.name)
    memory_kit.put(SEED_ENTRY, stored.key_id, stored.secret)
    _ = api.add_key(b2.SEED.name)
    if not predecessor_live:
        del api.keys[previous]
    api.calls.clear()

    key_id = b2.rotate_seed(kit, seed_entry=SEED_ENTRY, into=memory_kit)

    assert key_id == stored.key_id
    assert 'b2_create_key' not in api.calls
    assert api.named(b2.SEED.name) == [stored.key_id]
    # The row is untouched, and the retired kit is never written (§4.2).
    assert memory_kit.get(SEED_ENTRY, attribute='UserName') == stored.key_id
    assert memory_kit.get(SEED_ENTRY) == stored.secret
    assert kit.get(SEED_ENTRY, attribute='UserName') == previous


@pytest.mark.parametrize(('key_id', 'key'), [('', 'a-key'), ('a-key-id', '')], ids=['key id', 'key'])
def test_a_successor_row_missing_a_half_is_written_over(
    api: FakeApi, kit: KdbxStore, memory_kit: KdbxStore, key_id: str, key: str
) -> None:
    _ = _seeded(api, kit)
    # Present is not complete: a row the session cannot authorize from is no
    # key to keep, and treating it as one would refuse a rotation that has
    # nothing to resume. One half at a time, because each half is checked on
    # its own: a row blank in both is caught by either check alone.
    memory_kit.put(SEED_ENTRY, key_id, key)

    minted = b2.rotate_seed(kit, seed_entry=SEED_ENTRY, into=memory_kit)

    assert memory_kit.get(SEED_ENTRY, attribute='UserName') == minted
    assert memory_kit.get(SEED_ENTRY) == api.keys[minted].secret
    assert api.named(b2.SEED.name) == [minted]


def test_a_key_stops_working_the_moment_it_is_deleted(api: FakeApi, kit: KdbxStore) -> None:
    key_id = _seeded(api, kit)
    session = _session(api, kit)
    doomed = api.add_key(b2.SEED.name)

    session.delete_key(key_id)

    # The assumption the retirement order rests on, stated where it can be
    # seen: a token is a token *of a key*.
    with pytest.raises(requests.HTTPError):
        session.delete_key(doomed.key_id)


# -- the account check ------------------------------------------------------


def _elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """`conventions` recording an account that is not the one the seed reaches."""
    monkeypatch.setattr(
        conventions, 'B2_ACCOUNT', conventions.B2Account(region='us-west-002', account_id='some-other-account')
    )


def _unrecorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """`conventions` recording no account at all, as a fresh installation does."""
    monkeypatch.setattr(conventions, 'B2_ACCOUNT', conventions.B2Account(region='us-west-002'))


def _mints(api: FakeApi) -> int:
    return api.calls.count('b2_create_key')


def test_a_seed_in_another_account_mints_nothing_at_all(
    api: FakeApi, kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _seeded(api, kit)
    before = _mints(api)
    _elsewhere(monkeypatch)

    # Both accounts are named, because which of the two is stale -- a kit
    # re-seeded elsewhere, or an identifier written down wrong -- is the
    # operator's question and neither one alone answers it.
    with pytest.raises(CredentialRejected, match=f'{ACCOUNT_ID}.*some-other-account'):
        _ = b2.mint_management(kit, seed_entry=SEED_ENTRY)

    # The account is knowable from the authorization, which writes nothing, so
    # the refusal costs nothing. Held against `conventions` after the mint
    # instead, the same run would refuse and leave a live key behind in an
    # account this installation does not own -- recorded nowhere, and so known
    # to nobody who could revoke it.
    assert _mints(api) == before
    assert not api.named(b2.MANAGEMENT.name)


def test_an_account_root_from_another_account_creates_no_seed(
    api: FakeApi, kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _elsewhere(monkeypatch)

    with pytest.raises(CredentialRejected, match=f'{ACCOUNT_ID}.*some-other-account'):
        _ = b2.create_seed(root=_root(api), seeds=kit, seed_entry=SEED_ENTRY)

    # The first B2 credential of a bring-up is the one whose account is worth
    # the most: everything else here is minted from it.
    assert _mints(api) == 0
    assert not kit.has(SEED_ENTRY)


def test_a_rotation_in_another_account_deletes_nothing(
    api: FakeApi, kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_id = _seeded(api, kit)
    stranger = api.add_key(b2.SEED.name)
    _elsewhere(monkeypatch)

    with pytest.raises(CredentialRejected, match=f'{ACCOUNT_ID}.*some-other-account'):
        _ = b2.rotate_seed(kit, seed_entry=SEED_ENTRY)

    # A rotation's second act is to delete every other key of the seed's name.
    # Run against an account this installation does not own, that is a sweep
    # through somebody else's keys.
    assert api.named(b2.SEED.name) == sorted([key_id, stranger.key_id])
    assert kit.get(SEED_ENTRY, attribute='UserName') == key_id


def test_resuming_a_rotation_in_another_account_deletes_nothing(
    api: FakeApi, kit: KdbxStore, memory_kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other way into a rotation: a successor kit already holding its row
    # is resumed rather than minted into, and the resume's first act is the
    # same sweep, authorized as the successor's key instead of the seed's.
    previous = _seeded(api, kit)
    stored = api.add_key(b2.SEED.name)
    memory_kit.put(SEED_ENTRY, stored.key_id, stored.secret)
    stranger = api.add_key(b2.SEED.name)
    _elsewhere(monkeypatch)

    with pytest.raises(CredentialRejected, match=f'{ACCOUNT_ID}.*some-other-account'):
        _ = b2.rotate_seed(kit, seed_entry=SEED_ENTRY, into=memory_kit)

    assert api.named(b2.SEED.name) == sorted([previous, stored.key_id, stranger.key_id])
    assert memory_kit.get(SEED_ENTRY, attribute='UserName') == stored.key_id


def test_a_dump_key_is_not_minted_in_another_account(
    api: FakeApi, kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    before = _mints(api)
    _elsewhere(monkeypatch)

    # The same check as the three mints above, on the one mint that takes its
    # session ready-made: a caller cannot hand this a session for an account
    # `conventions` does not record and get a key out of it.
    with pytest.raises(CredentialRejected, match=f'{ACCOUNT_ID}.*some-other-account'):
        _ = b2.mint_dump_key(session, bucket_id=bucket_id)

    assert _mints(api) == before
    assert not api.named(b2.DUMPS_NAME)


def _commands_named(message: str) -> list[list[str]]:
    """Every `credentials …` command a refusal quotes, as argv without the program name."""
    return [quoted.split()[1:] for quoted in re.findall(r'`(credentials [^`]+)`', message)]


def _conventions_named(message: str) -> list[str]:
    return re.findall(r'`conventions\.(\w+)`', message)


def _mint_management_as_seed(kit: KdbxStore, api: FakeApi) -> object:
    return b2.mint_management(kit, seed_entry=SEED_ENTRY)


def _create_seed_as_root(kit: KdbxStore, api: FakeApi) -> object:
    return b2.create_seed(root=_root(api), seeds=kit, seed_entry=SEED_ENTRY)


@pytest.mark.parametrize(
    ('refused_by', 'refused_command'),
    [
        pytest.param(_mint_management_as_seed, ['derived', 'b2-management', 'mint'], id='seed'),
        pytest.param(_create_seed_as_root, ['seed', 'b2', 'create'], id='root'),
    ],
)
def test_a_refusal_names_both_repairs_and_each_is_a_command_that_exists(
    api: FakeApi,
    kit: KdbxStore,
    monkeypatch: pytest.MonkeyPatch,
    refused_by: Callable[[KdbxStore, FakeApi], object],
    refused_command: list[str],
) -> None:
    """Which of the two accounts is stale decides the repair, so both are spelled out.

    Held against the parser and against `conventions` rather than against the
    words: the command a refusal names has to be one `credentials` accepts, and
    the constant it names has to be one `conventions` has. And the command
    named must not be the one that just refused — on the root path the seed's
    repair would send the operator round in a circle, holding the same wrong
    master key.
    """
    _ = _seeded(api, kit)
    _elsewhere(monkeypatch)

    with pytest.raises(CredentialRejected) as refused:
        _ = refused_by(kit, api)
    message = str(refused.value)

    named = _conventions_named(message)
    assert named, message
    for name in named:
        assert hasattr(getattr(conventions, name), 'account_id'), f'{name} records no account_id'

    commands = _commands_named(message)
    assert commands, message
    parser = cli.build_parser()
    for argv in commands:
        parsed = vars(parser.parse_args(argv))
        assert [parsed['subject'], parsed['member'], parsed['action']] != refused_command, (
            f'the refusal sends the operator back to the command that refused: {argv}'
        )


def test_an_installation_that_records_no_account_is_refused_and_told_which_one_to_record(
    api: FakeApi, kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _seeded(api, kit)
    before = _mints(api)
    _unrecorded(monkeypatch)

    # A missing fact is a refusal rather than a skip: waved through, the check
    # would be absent exactly where nothing has ever pinned the account down.
    # The message carries the identifier, because recording it is the one-line
    # commit that clears the refusal.
    with pytest.raises(CredentialRejected, match=f'{ACCOUNT_ID}.*account_id'):
        _ = b2.mint_management(kit, seed_entry=SEED_ENTRY)

    assert _mints(api) == before


# -- the management key -----------------------------------------------------


def test_the_management_key_is_handed_back_rather_than_stored(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)

    minted = _delivered(b2.mint_management(kit, seed_entry=SEED_ENTRY))
    key_id, key = minted.key_id, minted.key

    # The offline store holds seeds, never the credentials automation
    # consumes (§1 rule 2), so the only copy is the one returned here.
    assert api.keys[key_id].secret == key
    assert kit.entries('seeds') == [SEED_ENTRY]
    assert api.keys[key_id].capabilities == b2.CAPABILITIES


def test_minting_the_management_key_retires_its_predecessors(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    previous = _delivered(b2.mint_management(kit, seed_entry=SEED_ENTRY)).key_id
    orphan = api.add_key(b2.MANAGEMENT.name)

    key_id = _delivered(b2.mint_management(kit, seed_entry=SEED_ENTRY)).key_id

    # The seed signs this retirement and survives it, so both stale keys go.
    assert api.named(b2.MANAGEMENT.name) == [key_id]
    assert previous not in api.keys
    assert orphan.key_id not in api.keys


def test_a_mint_retires_nothing_until_the_credential_has_been_delivered(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    previous = _delivered(b2.mint_management(kit, seed_entry=SEED_ENTRY)).key_id

    pending = b2.mint_management(kit, seed_entry=SEED_ENTRY)

    # A B2 key's secret is disclosed once, at creation, so between here and the
    # caller's push the new secret exists in this process alone. Retired inside
    # the mint, a push that then failed would leave the stack's config naming a
    # key the account no longer has and no working key written down anywhere.
    # Asserted against the account rather than against the new key's id,
    # because reading that id is delivering it.
    standing = api.named(b2.MANAGEMENT.name)
    assert previous in standing
    assert len(standing) == 2

    current = _delivered(pending)

    assert api.named(b2.MANAGEMENT.name) == [current.key_id]


# -- the bucket and its write-only key --------------------------------------


def _bucket(api: FakeApi, kit: KdbxStore) -> tuple[b2.Session, str]:
    session = _session(api, kit)
    return session, b2.ensure_bucket(session, BUCKET, prefix=PREFIX, retention_days=RETENTION_DAYS)


def test_the_bucket_is_created_private_with_the_retention_the_prefix_wants(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)

    _, bucket_id = _bucket(api, kit)

    bucket = api.buckets[bucket_id]
    assert bucket['bucketType'] == 'allPrivate'
    # Retention is a lifecycle rule so that nothing needs a delete capability
    # to keep the bucket from growing forever.
    assert bucket['lifecycleRules'] == [RETENTION]


def test_converging_the_bucket_twice_creates_one_bucket(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    _, first = _bucket(api, kit)

    _, second = _bucket(api, kit)

    assert (first, second) == (second, first)
    assert list(api.buckets) == [first]
    assert 'b2_update_bucket' not in api.calls


@pytest.mark.parametrize(
    'drifted',
    [
        [],
        [RETENTION | {'daysFromUploadingToHiding': RETENTION_DAYS * 10}],
        [RETENTION | {'daysFromHidingToDeleting': None}],
    ],
    ids=['removed', 'hidden-later', 'never-deleted'],
)
def test_a_retention_someone_changed_is_put_back(api: FakeApi, kit: KdbxStore, drifted: list[dict[str, Any]]) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    api.buckets[bucket_id]['lifecycleRules'] = drifted

    _ = b2.ensure_bucket(session, BUCKET, prefix=PREFIX, retention_days=RETENTION_DAYS)

    # The rule is the whole reason a compromised appliance cannot walk the
    # dump history, so drift in it is corrected rather than reported: a rule
    # that still governs the prefix but keeps files longer is drift too.
    assert api.buckets[bucket_id]['lifecycleRules'] == [RETENTION]


def test_the_uploader_keeps_the_name_the_running_appliance_s_key_carries() -> None:
    """The name is not free to change, which is why it is written down once.

    Retirement matches on it and so does the currency check, so a key the
    account already holds under the old name would read as "not the intended
    one" -- a box rebuilt for a rename -- while the key itself outlived every
    sweep that was supposed to reach it.
    """
    assert b2.DUMPS_NAME == 'kluster-state-dump'


def test_the_dump_key_is_confined_to_one_prefix_of_one_bucket(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)

    key_id = _delivered(b2.mint_dump_key(session, bucket_id=bucket_id)).key_id

    minted = api.keys[key_id]
    assert (minted.capabilities, minted.bucket_id, minted.name_prefix) == (
        b2.DUMP_CAPABILITIES,
        bucket_id,
        f'{PREFIX}/',
    )


def test_the_dump_key_can_write_and_nothing_else(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    minted = _delivered(b2.mint_dump_key(session, bucket_id=bucket_id))
    key_id, key = minted.key_id, minted.key

    uploader = b2.Session.authorize(key_id, key)

    # An appliance that could list would be an appliance that could walk the
    # dump history; the API refuses it rather than the code declining to ask.
    with pytest.raises(requests.HTTPError):
        _ = uploader.keys()
    with pytest.raises(requests.HTTPError):
        _ = uploader.buckets()


def test_minting_a_dump_key_retires_the_one_the_old_box_held(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    previous = _delivered(b2.mint_dump_key(session, bucket_id=bucket_id)).key_id

    key_id = _delivered(b2.mint_dump_key(session, bucket_id=bucket_id)).key_id

    # The box's copy cannot be read back, so a replacement box means a
    # replacement key and the old one is spent.
    assert api.named(b2.DUMPS_NAME) == [key_id]
    assert previous not in api.keys


def test_the_dump_key_retires_nothing_until_the_credential_has_been_delivered(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    previous = _delivered(b2.mint_dump_key(session, bucket_id=bucket_id)).key_id

    pending = b2.mint_dump_key(session, bucket_id=bucket_id)

    # The order every mint in this package has: the successor's secret is
    # disclosed once, so between here and the caller's push it exists in this
    # process alone, and the push is a box being launched with it inside the
    # Ignition. Retired here, a launch that then failed would leave the bucket
    # with no uploader key at all.
    # Asserted against the account rather than against the new key's id,
    # because reading that id is delivering it.
    standing = api.named(b2.DUMPS_NAME)
    assert previous in standing
    assert len(standing) == 2

    current = _delivered(pending)

    assert api.named(b2.DUMPS_NAME) == [current.key_id]


def _current(session: b2.Session, key_id: str, bucket_id: str) -> bool:
    return b2.dump_key_is_current(session, key_id, bucket_id=bucket_id)


def test_the_key_the_box_holds_is_the_intended_one(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    key_id = _delivered(b2.mint_dump_key(session, bucket_id=bucket_id)).key_id

    assert _current(session, key_id, bucket_id)


def _deleted(api: FakeApi, key: Key) -> None:
    del api.keys[key.key_id]


def _rescoped_to_another_bucket(_api: FakeApi, key: Key) -> None:
    key.bucket_id = 'bucket-elsewhere'


def _rescoped_to_another_prefix(_api: FakeApi, key: Key) -> None:
    key.name_prefix = 'elsewhere/'


def _widened(_api: FakeApi, key: Key) -> None:
    key.capabilities = ('writeFiles', 'readFiles')


def _renamed(_api: FakeApi, key: Key) -> None:
    key.name = 'someone-elses-key'


@pytest.mark.parametrize(
    ('description', 'mutate'),
    [
        ('deleted in the console', _deleted),
        ('scoped to another bucket', _rescoped_to_another_bucket),
        ('scoped to another prefix', _rescoped_to_another_prefix),
        ('given a capability it should not have', _widened),
        ('minted under another name', _renamed),
    ],
)
def test_a_key_that_is_no_longer_what_the_box_needs_is_not_current(
    api: FakeApi, kit: KdbxStore, description: str, mutate: Callable[[FakeApi, Key], None]
) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    key_id = _delivered(b2.mint_dump_key(session, bucket_id=bucket_id)).key_id

    mutate(api, api.keys[key_id])

    # The box's secret cannot be read back, so "is it holding the right
    # credential" is answered by identity — and a no here means a new box.
    assert not _current(session, key_id, bucket_id), description


def test_a_box_that_records_no_key_is_not_current(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)

    assert not _current(session, '', bucket_id)


# -- the drill's reader -----------------------------------------------------


#: The B2 capability families a read-only key must carry nothing from. Named
#: by what a capability does rather than listed, so a capability B2 adds to a
#: family is held to it without being named here.
MUTATING = ('write', 'delete', 'share')


def test_the_drill_reader_keeps_its_name() -> None:
    # Retirement matches on it (`retire_others`), so a rename would leave the
    # key the Environment holds outside every later sweep.
    assert b2.DRILL_READ_NAME == 'kluster-drill-read'


def test_the_drill_reader_is_confined_to_the_prefix_the_uploader_writes() -> None:
    reader, writer = b2.drill_reads('bucket-x'), b2.dumps('bucket-x')

    # The same bucket and the same prefix, separator included: a reader
    # confined to `pulumi-state` where the writer writes `pulumi-state/` would
    # read that prefix and every sibling that shares its letters.
    assert reader.bucket_id == writer.bucket_id == 'bucket-x'
    assert reader.name_prefix == writer.name_prefix == f'{PREFIX}/'


def test_the_drill_reader_may_do_nothing_the_uploader_may_and_nothing_administrative() -> None:
    reader = b2.drill_reads('bucket-x')

    assert reader.capabilities == ('listFiles', 'readFiles')
    # Disjoint from the writer's and from the administrative set: a key that
    # could write would be a second uploader, one that could administer would
    # be a second management key, and neither is what an exposed drill buys.
    assert not set(reader.capabilities) & set(b2.dumps('bucket-x').capabilities)
    assert not set(reader.capabilities) & set(b2.CAPABILITIES)
    assert not [capability for capability in reader.capabilities if capability.startswith(MUTATING)]
    assert not [capability for capability in reader.capabilities if capability.endswith('Keys')]


def test_the_drill_key_is_minted_as_the_role_and_not_a_wider_one(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)

    key_id = _delivered(b2.mint_drill_read_key(session, bucket_id=bucket_id)).key_id

    # What was asked of B2 is the role, whole: the mint takes a bucket and
    # nothing else, so no caller can hand it a capability or a prefix.
    minted = api.keys[key_id]
    assert (minted.name, minted.capabilities, minted.bucket_id, minted.name_prefix) == (
        b2.DRILL_READ_NAME,
        b2.DRILL_READ_CAPABILITIES,
        bucket_id,
        f'{PREFIX}/',
    )
    assert b2.drill_reads(bucket_id).describes(
        b2.ListedKey(key_id, minted.name, minted.capabilities, bucket_id, minted.name_prefix)
    )


def test_the_drill_key_is_verified_by_listing_the_prefix_as_itself(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    api.objects[bucket_id] = [f'{PREFIX}/20260101T000000Z.dump.age', 'etcd/snapshot']

    pending = b2.mint_drill_read_key(session, bucket_id=bucket_id)

    # One listing, served to the new key and not to the seed, of the prefix
    # the role names -- the one act the drill performs, done before anything
    # is delivered, so a key the platform would refuse that act is refused
    # here rather than on the drill's first scheduled run.
    (key_id,) = api.named(b2.DRILL_READ_NAME)
    assert api.listings == [(key_id, bucket_id, f'{PREFIX}/')]
    assert api.calls.index(LIST_FILE_NAMES) > api.calls.index('b2_create_key')
    _ = _delivered(pending)


def test_the_drill_key_can_list_and_read_its_prefix_and_nothing_else(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    api.objects[bucket_id] = [f'{PREFIX}/a.dump.age', f'{PREFIX}/b.dump.age', 'etcd/snapshot']
    minted = _delivered(b2.mint_drill_read_key(session, bucket_id=bucket_id))

    reader = b2.Session.authorize(minted.key_id, minted.key)

    assert reader.file_names(bucket_id, prefix=f'{PREFIX}/') == (f'{PREFIX}/a.dump.age', f'{PREFIX}/b.dump.age')
    # The platform refuses the rest rather than the code declining to ask:
    # another prefix of the same bucket, and every administrative call.
    with pytest.raises(requests.HTTPError):
        _ = reader.file_names(bucket_id, prefix='etcd/')
    with pytest.raises(requests.HTTPError):
        _ = reader.keys()
    with pytest.raises(requests.HTTPError):
        _ = reader.buckets()


def test_a_prefix_larger_than_one_page_is_listed_whole(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    api.objects[bucket_id] = [f'{PREFIX}/{day:02d}.dump.age' for day in range(1, 8)]
    api.file_page_limit = 3
    minted = _delivered(b2.mint_drill_read_key(session, bucket_id=bucket_id))

    # B2 pages `b2_list_file_names` at a size it chooses, so a caller that
    # read the first page only would call an old dump the newest.
    listed = b2.Session.authorize(minted.key_id, minted.key).file_names(bucket_id, prefix=f'{PREFIX}/')

    assert listed == tuple(api.objects[bucket_id])


def test_minting_the_drill_key_retires_its_predecessor(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    previous = _delivered(b2.mint_drill_read_key(session, bucket_id=bucket_id)).key_id

    key_id = _delivered(b2.mint_drill_read_key(session, bucket_id=bucket_id)).key_id

    # Re-running is the rotation: one live key of the name afterwards, and the
    # uploader beside it untouched -- the two roles retire by name, and the
    # names differ.
    assert api.named(b2.DRILL_READ_NAME) == [key_id]
    assert previous not in api.keys


def test_the_drill_key_retires_nothing_until_the_credential_has_been_delivered(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    previous = _delivered(b2.mint_drill_read_key(session, bucket_id=bucket_id)).key_id

    pending = b2.mint_drill_read_key(session, bucket_id=bucket_id)

    # The order every mint in this package has: the push is the caller's, and
    # until it returns the successor exists in this process alone. Retired
    # here, a push that then failed would leave the Environment naming a key
    # the account has deleted.
    standing = api.named(b2.DRILL_READ_NAME)
    assert previous in standing
    assert len(standing) == 2

    current = _delivered(pending)

    assert api.named(b2.DRILL_READ_NAME) == [current.key_id]


def test_a_drill_key_is_not_minted_in_another_account(
    api: FakeApi, kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    before = _mints(api)
    _elsewhere(monkeypatch)

    with pytest.raises(CredentialRejected, match=f'{ACCOUNT_ID}.*some-other-account'):
        _ = b2.mint_drill_read_key(session, bucket_id=bucket_id)

    assert _mints(api) == before
    assert not api.named(b2.DRILL_READ_NAME)


# -- the freshness probe's key -------------------------------------------------


def test_the_freshness_key_keeps_its_name() -> None:
    # Retirement matches on it (`retire_others`), so a rename would leave the
    # key the ops repository holds outside every later sweep.
    assert b2.FRESHNESS_DUMPS_NAME == 'kluster-freshness-dumps'


def test_the_freshness_key_is_confined_to_the_prefix_the_uploader_writes() -> None:
    lister, writer = b2.freshness_dumps('bucket-x'), b2.dumps('bucket-x')

    # The same bucket and the same prefix, separator included, for the reason
    # the drill's reader is held to them: a key confined to `pulumi-state`
    # would list that prefix and every sibling that shares its letters.
    assert lister.bucket_id == writer.bucket_id == 'bucket-x'
    assert lister.name_prefix == writer.name_prefix == f'{PREFIX}/'


def test_the_freshness_key_may_list_and_nothing_more() -> None:
    lister = b2.freshness_dumps('bucket-x')

    assert lister.capabilities == ('listFiles',)
    # Narrower than the drill's reader and disjoint from the writer: the probe
    # asks what the newest object is called, so a key that could read one
    # would be a second drill reader held where only a listing is needed, and
    # one that could write would be a second uploader.
    assert set(lister.capabilities) < set(b2.drill_reads('bucket-x').capabilities)
    assert not set(lister.capabilities) & set(b2.dumps('bucket-x').capabilities)
    assert not set(lister.capabilities) & set(b2.CAPABILITIES)
    assert not [capability for capability in lister.capabilities if capability.startswith(MUTATING)]


def test_the_freshness_key_is_minted_as_the_role_and_not_a_wider_one(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)

    key_id = _delivered(b2.mint_freshness_dumps_key(session, bucket_id=bucket_id)).key_id

    minted = api.keys[key_id]
    assert (minted.name, minted.capabilities, minted.bucket_id, minted.name_prefix) == (
        b2.FRESHNESS_DUMPS_NAME,
        b2.FRESHNESS_CAPABILITIES,
        bucket_id,
        f'{PREFIX}/',
    )


def test_the_freshness_key_is_verified_by_listing_the_prefix_as_itself(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    api.objects[bucket_id] = [f'{PREFIX}/20260101T000000Z.dump.age', 'etcd/snapshot']

    pending = b2.mint_freshness_dumps_key(session, bucket_id=bucket_id)

    # The one act the probe performs, done as the new key before anything is
    # delivered, so a key the platform would refuse that act is refused here
    # rather than on the probe's first scheduled run.
    (key_id,) = api.named(b2.FRESHNESS_DUMPS_NAME)
    assert api.listings == [(key_id, bucket_id, f'{PREFIX}/')]
    assert api.calls.index(LIST_FILE_NAMES) > api.calls.index('b2_create_key')
    _ = _delivered(pending)


def test_a_confined_key_learns_its_bucket_from_the_authorization(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    api.objects[bucket_id] = [f'{PREFIX}/a.dump.age']
    minted = _delivered(b2.mint_freshness_dumps_key(session, bucket_id=bucket_id))

    lister, confined = b2.Session.authorize_confined(minted.key_id, minted.key)

    # No `listBuckets` on this key, so the bucket id every file call takes has
    # to come back with the authorization -- and the session it comes with
    # lists the prefix and can do nothing else the account offers.
    assert confined == bucket_id
    assert lister.file_names(bucket_id, prefix=f'{PREFIX}/') == (f'{PREFIX}/a.dump.age',)
    with pytest.raises(requests.HTTPError):
        _ = lister.buckets()
    with pytest.raises(requests.HTTPError):
        _ = lister.keys()


def test_a_key_confined_to_no_bucket_is_refused_as_the_wrong_key(api: FakeApi, kit: KdbxStore) -> None:
    key_id = _seeded(api, kit)

    # The seed is account-wide; handed over where a prefix-scoped key was
    # expected, the refusal says so and names where the intended key comes
    # from, rather than failing later on a bucket id nothing supplied.
    with pytest.raises(CredentialRejected, match='confined to no bucket'):
        _ = b2.Session.authorize_confined(key_id, kit.get(SEED_ENTRY))


def test_minting_the_freshness_key_retires_its_predecessor(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    previous = _delivered(b2.mint_freshness_dumps_key(session, bucket_id=bucket_id)).key_id
    drill = _delivered(b2.mint_drill_read_key(session, bucket_id=bucket_id)).key_id

    key_id = _delivered(b2.mint_freshness_dumps_key(session, bucket_id=bucket_id)).key_id

    # Re-running is the rotation: one live key of the name afterwards, and the
    # drill's reader on the same prefix untouched -- the roles retire by name,
    # and the names differ.
    assert api.named(b2.FRESHNESS_DUMPS_NAME) == [key_id]
    assert previous not in api.keys
    assert api.named(b2.DRILL_READ_NAME) == [drill]


def test_the_freshness_key_retires_nothing_until_the_credential_has_been_delivered(
    api: FakeApi, kit: KdbxStore
) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    previous = _delivered(b2.mint_freshness_dumps_key(session, bucket_id=bucket_id)).key_id

    pending = b2.mint_freshness_dumps_key(session, bucket_id=bucket_id)

    # The order every mint in this package has: until the caller's push
    # returns, the successor exists in this process alone, and a push that
    # then failed would leave the ops repository naming a key the account has
    # deleted.
    standing = api.named(b2.FRESHNESS_DUMPS_NAME)
    assert previous in standing
    assert len(standing) == 2

    current = _delivered(pending)

    assert api.named(b2.FRESHNESS_DUMPS_NAME) == [current.key_id]


def test_a_freshness_key_is_not_minted_in_another_account(
    api: FakeApi, kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    before = _mints(api)
    _elsewhere(monkeypatch)

    with pytest.raises(CredentialRejected, match=f'{ACCOUNT_ID}.*some-other-account'):
        _ = b2.mint_freshness_dumps_key(session, bucket_id=bucket_id)

    assert _mints(api) == before
    assert not api.named(b2.FRESHNESS_DUMPS_NAME)


# -- the freshness row: the mint above, delivered through the GitHub sink ------

OPS_REPOSITORY = conventions.forge.OPS.full_name


def _freshness_bucket(api: FakeApi, kit: KdbxStore) -> str:
    """The dump bucket, converged the way `state-backend provision` converges it, by its settings' name."""
    session = b2.Session.from_entry(kit, SEED_ENTRY)
    return b2.ensure_bucket(session, appliance_settings.B2_BUCKET, prefix=PREFIX, retention_days=RETENTION_DAYS)


def test_the_freshness_row_pushes_both_halves_as_repository_secrets_of_the_ops_repository(
    api: FakeApi, kit: KdbxStore
) -> None:
    _ = _seeded(api, kit)
    bucket_id = _freshness_bucket(api, kit)
    gh = RecordedGh()

    key_id = derived.b2_freshness_dumps(kit, Forge(token='admin-token', run=gh))

    # The addresses are the slot map's own: two repository secrets of the ops
    # repository, no `--env`, holding the key the account now lists under the
    # role -- and that key is the minted one, confined as the role says.
    assert list(gh.values) == [
        (OPS_REPOSITORY, None, 'B2_FRESHNESS_DUMPS_KEY_ID'),
        (OPS_REPOSITORY, None, 'B2_FRESHNESS_DUMPS_KEY'),
    ]
    assert gh.values[(OPS_REPOSITORY, None, 'B2_FRESHNESS_DUMPS_KEY_ID')] == key_id
    assert gh.values[(OPS_REPOSITORY, None, 'B2_FRESHNESS_DUMPS_KEY')] == api.keys[key_id].secret
    assert all(['secret', 'set', name, '--repo', OPS_REPOSITORY] in gh.invocations for _, _, name in gh.values)
    minted = api.keys[key_id]
    assert (minted.capabilities, minted.bucket_id, minted.name_prefix) == (('listFiles',), bucket_id, f'{PREFIX}/')


def test_the_freshness_row_retires_its_predecessor_only_after_both_carriers_landed(
    api: FakeApi, kit: KdbxStore
) -> None:
    _ = _seeded(api, kit)
    _ = _freshness_bucket(api, kit)
    gh = RecordedGh()
    previous = derived.b2_freshness_dumps(kit, Forge(token='admin-token', run=gh))
    order: list[str] = []

    def timed(args: Sequence[str], *, token: str, stdin: str | None) -> str:
        if list(args[:2]) == ['secret', 'set']:
            order.append(f'push {args[2]}')
        return gh(args, token=token, stdin=stdin)

    def timed_post(url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: int) -> requests.Response:
        api_name = url.rsplit('/', 1)[-1]
        if api_name == 'b2_delete_key':
            order.append('retire')
        return api.post(url, json=json, headers=headers, timeout=timeout)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(b2.requests, 'post', timed_post)
        current = derived.b2_freshness_dumps(kit, Forge(token='admin-token', run=timed))

    # The order every mint in this package has, held across the sink: both
    # pushes, then the deletion of the key the workflow held before.
    assert order == ['push B2_FRESHNESS_DUMPS_KEY_ID', 'push B2_FRESHNESS_DUMPS_KEY', 'retire']
    assert api.named(b2.FRESHNESS_DUMPS_NAME) == [current]
    assert previous not in api.keys


def test_the_freshness_row_refuses_before_minting_where_the_dump_bucket_does_not_exist(
    api: FakeApi, kit: KdbxStore
) -> None:
    _ = _seeded(api, kit)
    before = _mints(api)

    with pytest.raises(SlotRefused, match='no bucket named'):
        _ = derived.b2_freshness_dumps(kit, Forge(token='admin-token', run=RecordedGh()))

    assert _mints(api) == before


def test_the_freshness_row_mints_nothing_in_another_account(
    api: FakeApi, kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _seeded(api, kit)
    _ = _freshness_bucket(api, kit)
    before = _mints(api)
    _elsewhere(monkeypatch)

    with pytest.raises(CredentialRejected, match=f'{ACCOUNT_ID}.*some-other-account'):
        _ = derived.b2_freshness_dumps(kit, Forge(token='admin-token', run=RecordedGh()))

    assert _mints(api) == before


def test_an_account_larger_than_one_page_is_listed_whole(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    key_id = _delivered(b2.mint_dump_key(session, bucket_id=bucket_id)).key_id
    api.page_limit = 2
    for _ in range(5):
        _ = api.add_key('unrelated')

    # B2 pages `b2_list_keys` at a size it chooses, so a caller that reads the
    # first page only would call a live key gone — and rebuild a box that was
    # fine, while leaving every key it failed to see behind.
    assert {listed.key_id for listed in session.keys()} == set(api.keys)
    assert _current(session, key_id, bucket_id)


# -- the response boundary --------------------------------------------------


def test_a_listed_key_missing_a_field_is_refused_naming_the_entry() -> None:
    answer = {
        'keys': [
            {'applicationKeyId': 'key-1', 'keyName': b2.DUMPS_NAME, 'capabilities': ['writeFiles']},
            {'applicationKeyId': 'key-2', 'capabilities': ['writeFiles']},
        ]
    }

    # Which call, which row, which field: a listing read one field at a time
    # is what keeps a changed response from surfacing as a KeyError inside a
    # retirement that has already deleted something.
    with pytest.raises(payload.ResponseRejected, match=r'b2_list_keys\.keys\[1\]: the answer carries no keyName'):
        _ = b2._key_page(answer)  # pyright: ignore[reportPrivateUsage]


def test_a_retention_of_the_wrong_type_is_refused_rather_than_compared() -> None:
    answer = {
        'buckets': [
            {
                'bucketId': 'bucket-1',
                'lifecycleRules': [
                    {
                        'fileNamePrefix': f'{PREFIX}/',
                        'daysFromUploadingToHiding': 'thirty',
                        'daysFromHidingToDeleting': 1,
                    }
                ],
            }
        ]
    }

    with pytest.raises(payload.ResponseRejected, match='daysFromUploadingToHiding'):
        _ = b2._listed_buckets(answer)  # pyright: ignore[reportPrivateUsage]


def test_an_answer_that_is_not_an_object_is_refused_rather_than_indexed() -> None:
    with pytest.raises(payload.ResponseRejected, match='b2_create_key: expected an object'):
        _ = b2._created_key(['key-1', 'secret-of-key-1'])  # pyright: ignore[reportPrivateUsage]


#: Shaped like the secret half of a key: short enough that a refusal which
#: quoted small values would quote this one whole.
STRAY_SECRET = 'K004secret-fbb1a7'


@pytest.mark.parametrize(
    ('answer', 'described_as'),
    [
        pytest.param(
            STRAY_SECRET, f'a string of {len(STRAY_SECRET)} characters', id='a string where an object was expected'
        ),
        pytest.param([STRAY_SECRET], 'a list of 1 entries', id='a list where an object was expected'),
        pytest.param(
            {'applicationKeyId': 'key-1', 'applicationKey': [STRAY_SECRET, STRAY_SECRET]},
            'a list of 2 entries',
            id='a field of the wrong type',
        ),
        pytest.param(
            {'applicationKeyId': 'key-1', 'applicationKey': {'value': STRAY_SECRET}},
            'an object of 1 fields',
            id='a field nested',
        ),
    ],
)
def test_a_refusal_describes_the_answer_and_never_quotes_it(answer: object, described_as: str) -> None:
    """The one place a provider's raw answer meets a log line.

    `b2_create_key` answers with the credential it just made, and a refusal of
    that answer is logged. So what a refusal says about a value is its kind and
    its size -- what the operator has to fix is the shape -- and never its
    content, however short: the cap that used to decide was one a key fits
    under.
    """
    with pytest.raises(payload.ResponseRejected) as refused:
        _ = b2._created_key(answer)  # pyright: ignore[reportPrivateUsage]

    message = str(refused.value)
    assert STRAY_SECRET not in message, message
    assert 'b2_create_key' in message
    # Kind and size are what is said instead, so the message is still an answer.
    assert described_as in message, message


def test_a_retention_that_already_says_this_is_not_rewritten(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    api.buckets[bucket_id]['lifecycleRules'] = [RETENTION | {'somethingB2Added': True}]

    _ = b2.ensure_bucket(session, BUCKET, prefix=PREFIX, retention_days=RETENTION_DAYS)

    # Rules are compared as rules -- the three fields a B2 lifecycle rule is
    # made of -- so anything else the answer carries is not drift, and does not
    # become a bucket rewritten on every run.
    assert 'b2_update_bucket' not in api.calls


# -- the fault sweep --------------------------------------------------------


class Interrupted(RuntimeError):
    """A run that stopped mid-flight, the way a lost process or a 500 does."""


@dataclass
class Faulty:
    """The fake API, counting calls and stopping the run at the k-th.

    Two ways to stop, because they leave different worlds behind: `before`
    never reaches the API, `after` lets the API act and loses the answer —
    which is the one that strands a key nobody holds the secret of.
    """

    api: FakeApi
    fail_at: int | None = None
    when: str = 'after'
    counted: int = 0

    def _guard(self, target: Callable[..., requests.Response], *args: Any, **kwargs: Any) -> requests.Response:
        self.counted += 1
        fatal = self.counted == self.fail_at
        if fatal and self.when == 'before':
            raise Interrupted(f'call {self.fail_at} never reached the API')
        result = target(*args, **kwargs)
        if fatal:
            raise Interrupted(f'call {self.fail_at} reached the API; the answer was lost')
        return result

    def get(self, url: str, *, auth: tuple[str, str], timeout: int) -> requests.Response:
        return self._guard(self.api.get, url, auth=auth, timeout=timeout)

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: int) -> requests.Response:
        return self._guard(self.api.post, url, json=json, headers=headers, timeout=timeout)

    def attach(self, monkeypatch: pytest.MonkeyPatch) -> Faulty:
        monkeypatch.setattr(b2.requests, 'get', self.get)
        monkeypatch.setattr(b2.requests, 'post', self.post)
        return self


#: The names the register mints under, and the invariant below counts.
MANAGED = (b2.SEED.name, b2.MANAGEMENT.name, b2.DUMPS_NAME, b2.DRILL_READ_NAME, b2.FRESHNESS_DUMPS_NAME)


def _kit_never_lies(kit: KdbxStore, api: FakeApi) -> None:
    """Whatever the kit holds must be a key the account would still accept.

    The invariant an interrupted run is most likely to break: a row written
    before the key works, or left behind after the key was deleted, is a kit
    that answers a question wrongly rather than not at all.
    """
    if not kit.has(SEED_ENTRY):
        return
    key_id = kit.get(SEED_ENTRY, attribute='UserName')
    held = api.keys.get(key_id)
    assert held is not None, 'the kit holds a key the account no longer has'
    assert held.secret == kit.get(SEED_ENTRY), 'the stored secret is not this key'
    assert held.name == b2.SEED.name


def _keys_are_bounded(api: FakeApi) -> None:
    """A crash strands at most the one key its own mint had just created."""
    for name in MANAGED:
        assert len(api.named(name)) <= 2, f'{name} left {len(api.named(name))} keys behind'


def _survived(kit: KdbxStore, api: FakeApi) -> None:
    """Checked after the crash and again after the re-run: a kit that lies is
    just as wrong while the operator is still deciding whether to re-run."""
    _kit_never_lies(kit, api)
    _keys_are_bounded(api)


def _create(api: FakeApi, kit: KdbxStore) -> None:
    _ = b2.create_seed(root=_root(api), seeds=kit, seed_entry=SEED_ENTRY)


def _rotate(api: FakeApi, kit: KdbxStore) -> None:
    _ = b2.rotate_seed(kit, seed_entry=SEED_ENTRY)


def _manage(api: FakeApi, kit: KdbxStore) -> None:
    """The whole of the management row: mint, push, retire.

    The delivery is what makes the retirement part of the operation being
    swept. A bare `mint_management` retires nothing by design, so an operation
    that stopped there would sweep part of the calls and read the accumulation
    it leaves behind as healthy.
    """
    _ = _delivered(b2.mint_management(kit, seed_entry=SEED_ENTRY))


def _provision(api: FakeApi, kit: KdbxStore) -> None:
    """The state-backend bring-up stage: converge the bucket, mint the key."""
    session = b2.Session.from_entry(kit, SEED_ENTRY)
    bucket_id = b2.ensure_bucket(session, BUCKET, prefix=PREFIX, retention_days=RETENTION_DAYS)
    if not b2.dump_key_is_current(session, '', bucket_id=bucket_id):
        # Delivered, not merely minted: a bare `mint_dump_key` retires nothing
        # by design, so a stage that stopped there would sweep part of the
        # calls and read the accumulation it leaves behind as healthy.
        _ = _delivered(b2.mint_dump_key(session, bucket_id=bucket_id))


def _drill(api: FakeApi, kit: KdbxStore) -> None:
    """The drill row's B2 half: find the bucket, mint the reader, deliver it.

    The bucket is converged here so the stage has one to confine the key to;
    the row itself looks it up (`derived.py`), because a drill that finds no
    bucket has nothing to read.
    """
    session = b2.Session.from_entry(kit, SEED_ENTRY)
    bucket_id = b2.ensure_bucket(session, BUCKET, prefix=PREFIX, retention_days=RETENTION_DAYS)
    _ = _delivered(b2.mint_drill_read_key(session, bucket_id=bucket_id))


def _freshness(api: FakeApi, kit: KdbxStore) -> None:
    """The freshness row: find the bucket, mint the list-only key, deliver it.

    The bucket is converged here for the reason `_drill` converges it.
    """
    session = b2.Session.from_entry(kit, SEED_ENTRY)
    bucket_id = b2.ensure_bucket(session, BUCKET, prefix=PREFIX, retention_days=RETENTION_DAYS)
    _ = _delivered(b2.mint_freshness_dumps_key(session, bucket_id=bucket_id))


Stage = Callable[[FakeApi, KdbxStore], None]


def _calls_made(operation: Stage, *, prepared: bool, monkeypatch: pytest.MonkeyPatch) -> int:
    """How many remote calls one uninterrupted run of `operation` makes.

    Measured rather than written down, so the sweep covers exactly the calls
    the stage makes today and widens by itself when the stage grows one.
    """
    api = FakeApi()
    kit = MemoryKit()
    _record_account(monkeypatch)
    faulty = Faulty(api).attach(monkeypatch)
    if prepared:
        _create(api, kit)
    before = faulty.counted
    operation(api, kit)
    return faulty.counted - before


def _rotate_into(api: FakeApi, kit: KdbxStore, successor: KdbxStore) -> None:
    _ = b2.rotate_seed(kit, seed_entry=SEED_ENTRY, into=successor)


def _rotate_into_a_fresh_kit(api: FakeApi, kit: KdbxStore) -> None:
    _rotate_into(api, kit, MemoryKit())


#: Each b2-touching stage, as (name, stage, whether a seed must exist first).
STAGES: tuple[tuple[str, Stage, bool], ...] = (
    ('create', _create, False),
    ('rotate', _rotate, True),
    ('management', _manage, True),
    ('provision', _provision, True),
    ('drill', _drill, True),
    ('freshness', _freshness, True),
)

#: Both ways a run can stop at call k (see `Faulty`).
CRASH_POINTS = 'before', 'after'


def _stage_calls() -> dict[str, int]:
    counts: dict[str, int] = {}
    with pytest.MonkeyPatch.context() as patch:
        for name, stage, prepared in STAGES:
            counts[name] = _calls_made(stage, prepared=prepared, monkeypatch=patch)
    return counts


def _rotate_into_calls() -> int:
    """The rotation into a second kit, measured the way `_stage_calls` measures.

    Its own sweep rather than a row of `STAGES`: the stage sweep holds the
    kit it rotates to `_kit_never_lies`, and a whole-kit rotation retires
    the key that kit holds by design (§4.2).
    """
    with pytest.MonkeyPatch.context() as patch:
        return _calls_made(_rotate_into_a_fresh_kit, prepared=True, monkeypatch=patch)


CALLS = _stage_calls()
ROTATE_INTO_CALLS = _rotate_into_calls()
# A stage that measures zero calls loses its entire sweep below and takes no
# case with it -- the parametrization simply collects fewer, and nothing
# anywhere reports the stage as unswept. Measuring is what keeps the sweep from
# being a number anyone maintains; this is what keeps a measurement of zero
# from reading as a stage with nothing to check. `all` of nothing is true, so
# the table's own emptiness is stated too.
assert CALLS and all(CALLS.values()), CALLS
assert ROTATE_INTO_CALLS, ROTATE_INTO_CALLS

#: One case per (stage, call, crash point): the whole sweep, enumerated from
#: the measurement above rather than from a number anyone maintains.
SWEEP = [
    (name, stage, prepared, failing_call, when)
    for name, stage, prepared in STAGES
    for failing_call in range(1, CALLS[name] + 1)
    for when in CRASH_POINTS
]


@pytest.mark.parametrize(
    ('name', 'stage', 'prepared', 'failing_call', 'when'),
    SWEEP,
    ids=[f'{name}-{failing_call}-{when}' for name, _, _, failing_call, when in SWEEP],
)
def test_a_stage_heals_from_a_failure_at_any_call(
    name: str,
    stage: Stage,
    prepared: bool,
    failing_call: int,
    when: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi()
    kit = MemoryKit()
    if prepared:
        _ = Faulty(api).attach(monkeypatch)
        _create(api, kit)
    # A counter of its own, so the call the sweep names is the k-th call of
    # the stage rather than of everything that had to happen before it.
    _ = Faulty(api, fail_at=failing_call, when=when).attach(monkeypatch)

    with pytest.raises(Interrupted):
        stage(api, kit)
    _survived(kit, api)

    # 'Idempotent by probing' (docs/credentials.md) means exactly this: the
    # repair is the same command, with nothing remembered about where it
    # stopped.
    _ = Faulty(api).attach(monkeypatch)
    stage(api, kit)

    _survived(kit, api)
    # And the key a lost run left behind is gone, not merely tolerated: one
    # key stands under each name the stage mints, and the kit holds the seed.
    for minted in MANAGED:
        assert len(api.named(minted)) <= 1, f'{name} left an orphaned {minted}'
    assert kit.has(SEED_ENTRY)


@pytest.mark.parametrize('when', CRASH_POINTS)
@pytest.mark.parametrize('failing_call', range(1, ROTATE_INTO_CALLS + 1))
def test_rotating_into_a_second_kit_heals_from_a_failure_at_any_call(
    failing_call: int, when: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi()
    kit = MemoryKit()
    successor = MemoryKit()
    _ = Faulty(api).attach(monkeypatch)
    _create(api, kit)
    retired = kit.get(SEED_ENTRY, attribute='UserName')
    _ = Faulty(api, fail_at=failing_call, when=when).attach(monkeypatch)

    with pytest.raises(Interrupted):
        _rotate_into(api, kit, successor)
    # Whichever kit the interruption left the live key in, that kit does not
    # lie about it: the successor names a key the account has or nothing, and
    # where it names nothing the retired kit's key still authorizes.
    _survived(successor, api)
    stored = successor.get(SEED_ENTRY, attribute='UserName') if b2.holds_seed(successor, SEED_ENTRY) else None
    if stored is None:
        _kit_never_lies(kit, api)
    api.calls.clear()

    # The repair is the same command into the same successor.
    _ = Faulty(api).attach(monkeypatch)
    _rotate_into(api, kit, successor)

    _survived(successor, api)
    key_id = successor.get(SEED_ENTRY, attribute='UserName')
    assert api.named(b2.SEED.name) == [key_id]
    # A successor that already held its key keeps it, and the re-run mints
    # nothing: a second mint would leave the key the first run stored as a
    # stray, or refuse outright once the predecessor is gone.
    if stored is not None:
        assert 'b2_create_key' not in api.calls
        assert key_id == stored
    else:
        assert api.calls.count('b2_create_key') == 1
    # §4.2: the retired kit is never written, whichever path the re-run took.
    assert kit.get(SEED_ENTRY, attribute='UserName') == retired
