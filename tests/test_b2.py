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

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests
from b2_api import FakeApi, Key
from memory_kit import MemoryKit

from kluster.scripts.credentials import b2, entries, masters
from kluster.scripts.credentials.kdbx import KdbxStore
from kluster.scripts.credentials.masters import CredentialRejected

PASSWORD = 'kit-password'
SEED_ENTRY = entries.SEEDS['b2'].entry

BUCKET = 'kluster-state'
PREFIX = 'dumps'
RETENTION_DAYS = 30
DUMP_KEY_NAME = 'kluster-state-dump'


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(b2.requests, 'get', fake.get)
    monkeypatch.setattr(b2.requests, 'post', fake.post)
    return fake


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


def _session(api: FakeApi, kit: KdbxStore) -> b2.Session:
    return b2.Session.from_entry(kit, SEED_ENTRY)


# -- the seed ---------------------------------------------------------------


def test_the_row_holds_the_key_id_and_the_key(api: FakeApi, kit: KdbxStore) -> None:
    key_id = _seeded(api, kit)

    # UserName is the public half everywhere in the kit (§2); the secret is
    # the application key, which B2 shows once.
    assert kit.get(SEED_ENTRY, attribute='UserName') == key_id
    assert kit.get(SEED_ENTRY) == api.keys[key_id].secret
    assert api.keys[key_id].name == b2.SEED_KEY_NAME


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
    orphan = api.add_key(b2.SEED_KEY_NAME)

    key_id = _seeded(api, kit)

    # A key nobody holds the secret of is a live permission, not a spare: the
    # run that mints the seed is the one that can see it and clear it.
    assert api.named(b2.SEED_KEY_NAME) == [key_id]
    assert orphan.key_id not in api.keys


def test_rotation_stores_the_successor_and_retires_the_predecessor(api: FakeApi, kit: KdbxStore) -> None:
    previous = _seeded(api, kit)

    key_id = b2.rotate_seed(kit, seed_entry=SEED_ENTRY)

    assert key_id != previous
    assert previous not in api.keys
    assert kit.get(SEED_ENTRY, attribute='UserName') == key_id
    assert api.named(b2.SEED_KEY_NAME) == [key_id]


def test_rotation_writes_a_new_kit_and_leaves_the_retired_one_untouched(
    api: FakeApi, kit: KdbxStore, memory_kit: KdbxStore
) -> None:
    previous = _seeded(api, kit)

    key_id = b2.rotate_seed(kit, seed_entry=SEED_ENTRY, into=memory_kit)

    # §4.2: the retired kit stays exactly as it was, because what was derived
    # under it has not expired yet.
    assert memory_kit.get(SEED_ENTRY, attribute='UserName') == key_id
    assert kit.get(SEED_ENTRY, attribute='UserName') == previous


def test_rotation_retires_every_superseded_seed_key(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    orphan = api.add_key(b2.SEED_KEY_NAME)

    key_id = b2.rotate_seed(kit, seed_entry=SEED_ENTRY)

    # More than one key to delete, and one of them is the credential the
    # rotation started as: a retirement signed with the predecessor stops
    # working the moment it deletes the predecessor, so it runs as the
    # successor instead.
    assert api.named(b2.SEED_KEY_NAME) == [key_id]
    assert orphan.key_id not in api.keys


def test_a_key_stops_working_the_moment_it_is_deleted(api: FakeApi, kit: KdbxStore) -> None:
    key_id = _seeded(api, kit)
    session = _session(api, kit)
    doomed = api.add_key(b2.SEED_KEY_NAME)

    session.delete_key(key_id)

    # The assumption the retirement order rests on, stated where it can be
    # seen: a token is a token *of a key*.
    with pytest.raises(requests.HTTPError):
        session.delete_key(doomed.key_id)


# -- the management key -----------------------------------------------------


def test_the_management_key_is_handed_back_rather_than_stored(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)

    key_id, key = b2.mint_management(kit, seed_entry=SEED_ENTRY)

    # The offline store holds seeds, never the credentials automation
    # consumes (§1 rule 2), so the only copy is the one returned here.
    assert api.keys[key_id].secret == key
    assert kit.entries('seeds') == [SEED_ENTRY]
    assert api.keys[key_id].capabilities == b2.CAPABILITIES


def test_minting_the_management_key_retires_its_predecessors(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    previous, _ = b2.mint_management(kit, seed_entry=SEED_ENTRY)
    orphan = api.add_key(b2.MANAGEMENT_KEY_NAME)

    key_id, _ = b2.mint_management(kit, seed_entry=SEED_ENTRY)

    # The seed signs this retirement and survives it, so both stale keys go.
    assert api.named(b2.MANAGEMENT_KEY_NAME) == [key_id]
    assert previous not in api.keys
    assert orphan.key_id not in api.keys


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
    assert bucket['lifecycleRules'] == [
        {
            'fileNamePrefix': f'{PREFIX}/',
            'daysFromUploadingToHiding': RETENTION_DAYS,
            'daysFromHidingToDeleting': 1,
        }
    ]


def test_converging_the_bucket_twice_creates_one_bucket(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    _, first = _bucket(api, kit)

    _, second = _bucket(api, kit)

    assert (first, second) == (second, first)
    assert list(api.buckets) == [first]
    assert 'b2_update_bucket' not in api.calls


def test_a_retention_someone_changed_is_put_back(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    api.buckets[bucket_id]['lifecycleRules'] = []

    _ = b2.ensure_bucket(session, BUCKET, prefix=PREFIX, retention_days=RETENTION_DAYS)

    # The rule is the whole reason a compromised appliance cannot walk the
    # dump history, so drift in it is corrected rather than reported.
    assert api.buckets[bucket_id]['lifecycleRules'][0]['daysFromUploadingToHiding'] == RETENTION_DAYS


def test_the_dump_key_is_confined_to_one_prefix_of_one_bucket(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)

    key_id, _ = b2.mint_dump_key(session, bucket_id=bucket_id, prefix=PREFIX, name=DUMP_KEY_NAME)

    minted = api.keys[key_id]
    assert (minted.capabilities, minted.bucket_id, minted.name_prefix) == (
        b2.DUMP_CAPABILITIES,
        bucket_id,
        f'{PREFIX}/',
    )


def test_the_dump_key_can_write_and_nothing_else(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    key_id, key = b2.mint_dump_key(session, bucket_id=bucket_id, prefix=PREFIX, name=DUMP_KEY_NAME)

    uploader = b2.Session.authorize(key_id, key)

    # An appliance that could list would be an appliance that could walk the
    # dump history; the API refuses it rather than the code declining to ask.
    with pytest.raises(requests.HTTPError):
        _ = uploader.keys()
    with pytest.raises(requests.HTTPError):
        _ = uploader.post('b2_list_buckets', {'accountId': uploader.account_id})


def test_minting_a_dump_key_retires_the_one_the_old_box_held(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    previous, _ = b2.mint_dump_key(session, bucket_id=bucket_id, prefix=PREFIX, name=DUMP_KEY_NAME)

    key_id, _ = b2.mint_dump_key(session, bucket_id=bucket_id, prefix=PREFIX, name=DUMP_KEY_NAME)

    # The box's copy cannot be read back, so a replacement box means a
    # replacement key and the old one is spent.
    assert api.named(DUMP_KEY_NAME) == [key_id]
    assert previous not in api.keys


def _current(session: b2.Session, key_id: str, bucket_id: str) -> bool:
    return b2.dump_key_is_current(session, key_id, bucket_id=bucket_id, prefix=PREFIX, name=DUMP_KEY_NAME)


def test_the_key_the_box_holds_is_the_intended_one(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    key_id, _ = b2.mint_dump_key(session, bucket_id=bucket_id, prefix=PREFIX, name=DUMP_KEY_NAME)

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
    key_id, _ = b2.mint_dump_key(session, bucket_id=bucket_id, prefix=PREFIX, name=DUMP_KEY_NAME)

    mutate(api, api.keys[key_id])

    # The box's secret cannot be read back, so "is it holding the right
    # credential" is answered by identity — and a no here means a new box.
    assert not _current(session, key_id, bucket_id), description


def test_a_box_that_records_no_key_is_not_current(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)

    assert not _current(session, '', bucket_id)


def test_an_account_larger_than_one_page_is_listed_whole(api: FakeApi, kit: KdbxStore) -> None:
    _ = _seeded(api, kit)
    session, bucket_id = _bucket(api, kit)
    key_id, _ = b2.mint_dump_key(session, bucket_id=bucket_id, prefix=PREFIX, name=DUMP_KEY_NAME)
    api.page_limit = 2
    for _ in range(5):
        _ = api.add_key('unrelated')

    # B2 pages `b2_list_keys` at a size it chooses, so a caller that reads the
    # first page only would call a live key gone — and rebuild a box that was
    # fine, while leaving every key it failed to see behind.
    assert {str(listed['applicationKeyId']) for listed in session.keys()} == set(api.keys)
    assert _current(session, key_id, bucket_id)


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
MANAGED = (b2.SEED_KEY_NAME, b2.MANAGEMENT_KEY_NAME, DUMP_KEY_NAME)


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
    assert held.name == b2.SEED_KEY_NAME


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
    _ = b2.mint_management(kit, seed_entry=SEED_ENTRY)


def _provision(api: FakeApi, kit: KdbxStore) -> None:
    """The state-backend bring-up stage: converge the bucket, mint the key."""
    session = b2.Session.from_entry(kit, SEED_ENTRY)
    bucket_id = b2.ensure_bucket(session, BUCKET, prefix=PREFIX, retention_days=RETENTION_DAYS)
    if not b2.dump_key_is_current(session, '', bucket_id=bucket_id, prefix=PREFIX, name=DUMP_KEY_NAME):
        _ = b2.mint_dump_key(session, bucket_id=bucket_id, prefix=PREFIX, name=DUMP_KEY_NAME)


Stage = Callable[[FakeApi, KdbxStore], None]


def _calls_made(operation: Stage, *, prepared: bool, monkeypatch: pytest.MonkeyPatch) -> int:
    """How many remote calls one uninterrupted run of `operation` makes.

    Measured rather than written down, so the sweep covers exactly the calls
    the stage makes today and widens by itself when the stage grows one.
    """
    api = FakeApi()
    kit = MemoryKit()
    faulty = Faulty(api).attach(monkeypatch)
    if prepared:
        _create(api, kit)
    before = faulty.counted
    operation(api, kit)
    return faulty.counted - before


#: Each b2-touching stage, as (name, stage, whether a seed must exist first).
STAGES: tuple[tuple[str, Stage, bool], ...] = (
    ('create', _create, False),
    ('rotate', _rotate, True),
    ('management', _manage, True),
    ('provision', _provision, True),
)

#: Both ways a run can stop at call k (see `Faulty`).
CRASH_POINTS = 'before', 'after'


def _stage_calls() -> dict[str, int]:
    counts: dict[str, int] = {}
    with pytest.MonkeyPatch.context() as patch:
        for name, stage, prepared in STAGES:
            counts[name] = _calls_made(stage, prepared=prepared, monkeypatch=patch)
    return counts


CALLS = _stage_calls()

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
