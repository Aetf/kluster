"""The backup bucket and its writer keys.

Every assertion here is about a rule that is invisible in a later diff because
it is a rule about *absence*: that no key can delete a file, that no key can
read a prefix that is not its own, and that the lifecycle rules remove old
versions without hiding a current one anywhere but where its consumer leaves
retention to the bucket. A backup regime fails silently when any of those
slips, and it fails at exactly the moment it is needed.
"""

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import pulumi
import pytest
import pytest_asyncio
from b2_api import API_URL, AUTHORIZE_URL, DOWNLOAD_URL, FakeApi
from mock_monitor import Recorder, declaring, run_with
from pulumi_b2.outputs import BucketLifecycleRule

from kluster import conventions
from kluster.components.backup import (
    BackupBucket,
    DuplicateScope,
    NonPositiveHideAge,
    OverlappingHide,
    Scope,
    UnseparatedPrefix,
    barman_scope,
    etcd_reader_scope,
    etcd_scope,
    s3_endpoint,
    volsync_scope,
)
from kluster.conventions.backup import BACKED_NAMESPACES, ETCD_SNAPSHOT_RETENTION_DAYS, BackupFamily, metrics_repo_path
from kluster.stacks import physical

BUCKET_ID = 'b2-bucket-id'
REGION = 'us-west-004'

#: The bucket's name and its version-retention floor, both the caller's to
#: state. Neither is the fleet's own, so a component that read either off
#: `conventions` instead of its parameter would fail the cases that look.
BUCKET_NAME = 'a-backup-bucket'
VERSION_RETENTION_DAYS = 7


#: The key pair the component builds its provider from. Both are invented:
#: what the suite checks is that they are read here at all, and that every
#: resource below is signed with the provider they configure.
KEY_ID = 'an-application-key-id'
KEY = 'an-application-key'


class B2(Recorder):
    """The identifiers the account assigns: a bucket id, and each key's pair."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'b2:index/bucket:Bucket':
            return {'bucketId': BUCKET_ID}
        if args.typ == 'b2:index/applicationKey:ApplicationKey':
            return {'applicationKeyId': args.name + '-key-id', 'applicationKey': args.name + '-secret'}
        return {}


@pytest_asyncio.fixture(autouse=True)
async def monitor() -> B2:
    from kluster.components.backup import APPLICATION_KEY, APPLICATION_KEY_ID

    pulumi.runtime.set_all_config({f'kluster:{APPLICATION_KEY_ID}': KEY_ID, f'kluster:{APPLICATION_KEY}': KEY})
    return await run_with(B2(), stack='physical')


#: One consumer, which is the shape the assertions below are written against:
#: a bucket whose whole key set is known, so that "every key" can be checked
#: against a set stated in the test rather than read back off the component.
#: The roll is the caller's, and this suite is one of the callers.
SCOPES: tuple[Scope, ...] = (etcd_scope(),)


def build(scopes: Sequence[Scope] = SCOPES) -> BackupBucket:
    return BackupBucket(
        'kluster', region=REGION, bucket_name=BUCKET_NAME, retention_days=VERSION_RETENTION_DAYS, scopes=scopes
    )


@pytest.mark.asyncio
async def test_no_key_can_delete_a_file() -> None:
    scopes = [etcd_scope(), etcd_reader_scope(), volsync_scope('immich'), barman_scope('immich')]
    bucket = build(scopes)
    # The rule is about every key there is, so which keys there are is pinned
    # before the loop: a bucket that minted none would satisfy the loop below
    # without any key having been checked, and the rule would read as held.
    assert set(bucket.keys) == {scope.name for scope in scopes}

    for key in bucket.keys.values():
        capabilities = await key.capabilities.future()
        assert capabilities is not None
        # This is the rule the whole backup-integrity design rests on: without
        # the capability a deletion is a hide, and a hide is recoverable.
        # Written as B2 spells it, so a constant that misspelled it could not
        # wave the real capability through.
        assert 'deleteFiles' not in capabilities


@pytest.mark.asyncio
async def test_a_store_keeping_writer_key_can_still_read_its_own_index() -> None:
    """Every family's key lists, reads and writes: a literally write-only key cannot back a store up.

    restic and barman list and read their own index before they write, and
    `vmbackup` lists the destination to upload only what it lacks. Over every
    family `physical` mints a key for, which is where that requirement lives;
    the etcd snapshots' uploader keeps no store and is held to its own calls
    below. B2's names for the three, so the requirement is held against what a
    mover does rather than against the component's own tuple.
    """
    scopes = [scope('a-namespace') for scope in physical.FAMILY_SCOPES.values()]
    bucket = build(scopes)
    assert set(bucket.keys) == {scope.name for scope in scopes}
    assert scopes

    for name, key in bucket.keys.items():
        capabilities = await key.capabilities.future()
        assert capabilities is not None
        assert {'listFiles', 'readFiles', 'writeFiles'} <= set(capabilities), name


#: A snapshot already in the bucket, and a file of another consumer beside it.
STORED_SNAPSHOT = f'{conventions.ETCD_SNAPSHOT_PREFIX}/kluster-2026-11-17T06:05:00Z.snap.age'
STORED_ELSEWHERE = metrics_repo_path('monitoring', 'recent') + '/backup_complete.ignore'

#: One call as a key: the API's name and the body, answered with the status.
Call = Callable[[str, dict[str, Any]], int]


async def as_declared(scope: Scope) -> tuple[FakeApi, Call]:
    """An account holding the bucket and a key minted as the component declares `scope`'s, and a call as that key.

    The key's capabilities, bucket and prefix are read off the declared
    `ApplicationKey`, and the account answers each endpoint as the API
    reference states its capability (`b2_api`), so what is held is what B2
    lets the declared key do rather than what the declaration lists.
    """
    bucket = build([scope])
    key = bucket.keys[scope.name]
    capabilities = await key.capabilities.future()
    bucket_ids = await key.bucket_ids.future()
    assert capabilities is not None
    assert bucket_ids is not None
    (bucket_id,) = bucket_ids
    api = FakeApi()
    api.buckets[bucket_id] = {'bucketId': bucket_id, 'bucketName': BUCKET_NAME, 'bucketType': 'allPrivate'}
    api.objects[bucket_id] = [STORED_SNAPSHOT, STORED_ELSEWHERE]
    held = api.add_key(scope.name, tuple(capabilities), bucket_id=bucket_id, name_prefix=await key.name_prefix.future())
    token = api.get(AUTHORIZE_URL, auth=(held.key_id, held.secret), timeout=10).json()['authorizationToken']

    def call(endpoint: str, body: dict[str, Any]) -> int:
        host = DOWNLOAD_URL if endpoint == 'b2_download_file_by_id' else API_URL
        url = f'{host}/b2api/v3/{endpoint}'
        return api.post(url, json=body, headers={'Authorization': token}, timeout=10).status_code

    return api, call


def snapshot_named(name: str) -> dict[str, Any]:
    return {'bucketId': BUCKET_ID, 'fileName': name}


@pytest.mark.asyncio
async def test_the_etcd_reader_lists_and_downloads_the_snapshots_and_can_do_nothing_else() -> None:
    """The restore-verify's key reads the newest snapshot back, and cannot write one or reach another prefix.

    Both a recovery and the restore-verify take the newest name under the
    prefix, so a reading key that could upload would let its consumer plant
    the snapshot a recovery bootstraps from.
    """
    api, call = await as_declared(etcd_reader_scope())
    prefix = f'{conventions.ETCD_SNAPSHOT_PREFIX}/'

    assert call('b2_list_file_names', {'bucketId': BUCKET_ID, 'prefix': prefix, 'maxFileCount': 100}) == 200
    assert call('b2_download_file_by_id', {'fileId': api.file_id(BUCKET_ID, STORED_SNAPSHOT)}) == 200

    planted = f'{prefix}kluster-2099-01-01T00:00:00Z.snap.age'
    assert call('b2_get_upload_url', {'bucketId': BUCKET_ID}) == 401
    assert call('b2_start_large_file', snapshot_named(planted) | {'contentType': 'b2/x-auto'}) == 401
    assert call('b2_hide_file', snapshot_named(STORED_SNAPSHOT)) == 401
    assert call('b2_list_file_names', {'bucketId': BUCKET_ID, 'prefix': 'vm/', 'maxFileCount': 100}) == 401
    assert call('b2_download_file_by_id', {'fileId': api.file_id(BUCKET_ID, STORED_ELSEWHERE)}) == 401
    assert api.objects[BUCKET_ID] == [STORED_SNAPSHOT, STORED_ELSEWHERE]


@pytest.mark.asyncio
async def test_the_etcd_uploader_can_upload_a_snapshot_whole_or_in_parts_and_cannot_list_one() -> None:
    """The snapshot job's key makes the calls its uploader makes, and the reading is another key's.

    `talos-backup` puts a snapshot in one request or, past the transfer
    manager's part size, as a multipart upload it completes or aborts
    (`PutObject`, the multipart create, part, complete and abort); in the
    native API those are an upload address and the large-file calls.
    """
    api, call = await as_declared(etcd_scope())
    upload = f'{conventions.ETCD_SNAPSHOT_PREFIX}/kluster-2026-11-18T06:05:00Z.snap.age'

    assert call('b2_get_upload_url', {'bucketId': BUCKET_ID}) == 200
    assert call('b2_start_large_file', snapshot_named(upload) | {'contentType': 'b2/x-auto'}) == 200
    (whole,) = api.large_files
    assert call('b2_get_upload_part_url', {'fileId': whole}) == 200
    assert call('b2_finish_large_file', {'fileId': whole, 'partSha1Array': []}) == 200
    assert upload in api.objects[BUCKET_ID]

    aborted = upload.replace('06:05', '07:05')
    assert call('b2_start_large_file', snapshot_named(aborted) | {'contentType': 'b2/x-auto'}) == 200
    (unfinished,) = api.large_files
    assert call('b2_cancel_large_file', {'fileId': unfinished}) == 200

    prefix = f'{conventions.ETCD_SNAPSHOT_PREFIX}/'
    assert call('b2_list_file_names', {'bucketId': BUCKET_ID, 'prefix': prefix, 'maxFileCount': 100}) == 401
    assert call('b2_download_file_by_id', {'fileId': api.file_id(BUCKET_ID, STORED_SNAPSHOT)}) == 401


@pytest.mark.asyncio
async def test_a_store_keeping_writer_can_learn_that_a_name_is_missing() -> None:
    """Every family's key lets B2 answer a read of a missing name with 404.

    Each family's tool keeps a store in the bucket and reads names that may
    not exist yet as part of an ordinary run -- restic's `init`, barman's
    object checks, `vmbackup`'s completion marker -- taking only a 404 for
    absence. B2's S3 API answers such a read with 403 unless the key holds
    `listBuckets` (kopia/kopia#5329; Aetf/kluster-ops#864), and the store's
    first backup then fails. Written in B2's spelling, and over every family
    `physical` mints a key for, so a family added later is held to it too.
    """
    scopes = [scope('a-namespace') for scope in physical.FAMILY_SCOPES.values()]
    bucket = build(scopes)
    assert set(bucket.keys) == {scope.name for scope in scopes}
    assert scopes

    for name, key in bucket.keys.items():
        capabilities = await key.capabilities.future()
        assert capabilities is not None
        assert 'listBuckets' in capabilities, name


@pytest.mark.asyncio
async def test_each_key_reaches_one_prefix_and_one_bucket() -> None:
    etcd, volsync, barman = etcd_scope(), volsync_scope('immich'), barman_scope('immich')
    bucket = build([etcd, volsync, barman])

    # Each key guards the prefix its own scope names, and no two keys share one.
    prefixes = {name: await key.name_prefix.future() for name, key in bucket.keys.items()}
    assert prefixes == {scope.name: scope.prefix for scope in (etcd, volsync, barman)}
    assert len(set(prefixes.values())) == len(prefixes)

    for key in bucket.keys.values():
        # Scoped to this bucket, not to the account: a key that could reach
        # another bucket is not prefix-scoped, it is merely prefix-flavored.
        assert await key.bucket_ids.future() == [BUCKET_ID]


@dataclass(frozen=True)
class Applied:
    """What the bucket does to one file: the three timers, each `None` where no rule sets it."""

    hide: float | None
    purge: float | None
    cancel: float | None


def applied(rules: Sequence[BucketLifecycleRule], name: str) -> Applied:
    """The lifecycle B2 applies to the file `name`, by Backblaze's own statement of overlapping rules.

    Every rule whose `fileNamePrefix` the name starts with governs the file,
    and for each property "the system evaluates all matching rules and
    applies the smallest non-null value"
    (https://www.backblaze.com/docs/cloud-storage-lifecycle-rules, "Overlapping
    Prefixes"). That is the account's reading of the rules rather than the
    component's, so the cases below hold the component to what B2 will do
    with what it declares.
    """
    matching = [rule for rule in rules if name.startswith(rule.file_name_prefix)]

    def smallest(values: Sequence[float | None]) -> float | None:
        present = [value for value in values if value is not None]
        return min(present) if present else None

    return Applied(
        hide=smallest([rule.days_from_uploading_to_hiding for rule in matching]),
        purge=smallest([rule.days_from_hiding_to_deleting for rule in matching]),
        cancel=smallest([rule.days_from_starting_to_canceling_unfinished_large_files for rule in matching]),
    )


def written(namespace: str) -> dict[BackupFamily, str]:
    """A file each family writes for `namespace`, at the path its own consumer writes it."""
    return {
        BackupFamily.VOLSYNC: conventions.volsync_repo_path(namespace, 'a-claim') + '/data/00/a-pack',
        BackupFamily.BARMAN: conventions.barman_repo_path(namespace, 'a-database') + '/base/a-backup/data.tar',
        BackupFamily.VM: metrics_repo_path(namespace, 'a-store') + '/data/small/a-part/values.bin',
    }


#: A file under the etcd snapshots' prefix.
ETCD_SNAPSHOT = f'{conventions.ETCD_SNAPSHOT_PREFIX}/a-snapshot'


async def fleet_rules() -> Sequence[BucketLifecycleRule]:
    """The lifecycle rules of a bucket carrying the `physical` stack's own roll of consumers."""
    bucket = build(physical.backup_scopes(BACKED_NAMESPACES))
    rules = await bucket.bucket.lifecycle_rules.future()
    assert rules is not None
    return rules


@pytest.mark.asyncio
async def test_a_pruning_consumers_files_are_never_hidden_on_a_timer_and_age_out_once_hidden() -> None:
    """Every store a newer backup builds on keeps its current files, and its hides age out on the floor.

    `days_from_uploading_to_hiding` hides *current* files on a timer, which
    on a restic repository, a barman archive or a `vmbackup` copy is a
    scheduled deletion of live backups: an old pack or base backup is still
    part of the newest one. So no file any family writes is hidden on a timer,
    for every namespace the census backs up, while each is purged once its
    own prune or an overwrite hides it.
    """
    assert VERSION_RETENTION_DAYS != conventions.BACKUP_VERSION_RETENTION_DAYS
    rules = await fleet_rules()
    files = [written(namespace)[family] for namespace, families in BACKED_NAMESPACES.items() for family in families]
    assert files

    for name in files:
        kept = applied(rules, name)
        assert (kept.hide, kept.purge) == (None, VERSION_RETENTION_DAYS), name


@pytest.mark.asyncio
async def test_the_floor_covers_a_file_no_consumer_claims() -> None:
    # The floor is a property of the bucket, so it covers a consumer nobody
    # remembered to add a rule for.
    rules = await fleet_rules()
    stray = applied(rules, 'a-prefix-nobody-declared/a-file')
    assert (stray.hide, stray.purge) == (None, VERSION_RETENTION_DAYS)


@pytest.mark.asyncio
async def test_an_etcd_snapshot_is_hidden_at_its_retention_and_then_purged_on_the_floor() -> None:
    """The snapshots' retention is the bucket's: listed for their retention, kept for the floor after it.

    Nothing prunes a snapshot -- the writer key deletes nothing, and no code
    hides one -- so the bucket's hide is the only thing that retires it, and
    the whole-bucket floor still reaches it once hidden, which is what keeps a
    hidden snapshot recoverable for as long as any other version.
    """
    rules = await fleet_rules()
    snapshot = applied(rules, ETCD_SNAPSHOT)
    assert snapshot.hide == ETCD_SNAPSHOT_RETENTION_DAYS
    assert snapshot.purge == VERSION_RETENTION_DAYS


@pytest.mark.asyncio
async def test_a_scope_that_leaves_retention_to_the_bucket_is_hidden_on_its_own_timer() -> None:
    whole = Scope(name='whole', prefix='whole/', hide_after_days=3)
    bucket = build([whole, volsync_scope('immich')])
    rules = await bucket.bucket.lifecycle_rules.future()
    assert rules is not None

    retired = applied(rules, 'whole/a-file')
    assert (retired.hide, retired.purge) == (3, VERSION_RETENTION_DAYS)
    assert applied(rules, written('immich')[BackupFamily.VOLSYNC]).hide is None


@pytest.mark.parametrize('days', [0, -1])
def test_a_hide_age_of_no_days_is_refused(days: int) -> None:
    # The provider sends a zero as an unset value, so a scope asking to be
    # hidden at once would be kept forever without a word.
    with pytest.raises(NonPositiveHideAge) as refused:
        build([Scope(name='eager', prefix='eager/', hide_after_days=days)])
    assert (refused.value.scope, refused.value.days) == ('eager', days)


#: A scope whose files the bucket retires, and a pruning one whose prefix lies
#: inside it: a hide on the outer prefix reaches the inner consumer's files.
OUTER_HIDING = Scope(name='outer', prefix='volsync/', hide_after_days=3)
INNER_PRUNING = volsync_scope('immich')


@pytest.mark.parametrize(
    'scopes',
    [[OUTER_HIDING, INNER_PRUNING], [INNER_PRUNING, OUTER_HIDING]],
    ids=['hiding-first', 'hiding-last'],
)
def test_a_hide_enclosing_another_scope_is_refused(scopes: list[Scope]) -> None:
    with pytest.raises(OverlappingHide) as refused:
        build(scopes)
    assert (refused.value.scope, refused.value.other) == (OUTER_HIDING.name, INNER_PRUNING.name)


def test_a_hide_repeating_another_scopes_prefix_is_refused() -> None:
    hiding = Scope(name='hiding', prefix='shared/', hide_after_days=3)
    pruning = Scope(name='pruning', prefix='shared/')
    with pytest.raises(OverlappingHide) as refused:
        build([pruning, hiding])
    assert (refused.value.scope, refused.value.other) == ('hiding', 'pruning')


@pytest.mark.asyncio
async def test_a_scope_that_cannot_write_may_read_a_hiding_scopes_prefix() -> None:
    """A key that writes nothing puts no file under the prefix, so the hide there retires nothing of its.

    The etcd snapshots' reader is this case: it reads the prefix whose hide
    retires the snapshots, and each key reaches the prefix it was declared on.
    """
    hiding = Scope(name='hiding', prefix='shared/', hide_after_days=3)
    reading = Scope(name='reading', prefix='shared/', capabilities=('listFiles', 'readFiles'))

    bucket = build([hiding, reading])

    assert {name: await key.name_prefix.future() for name, key in bucket.keys.items()} == {
        'hiding': 'shared/',
        'reading': 'shared/',
    }


@pytest.mark.parametrize('capabilities', [('writeFiles',), ('listFiles', 'readFiles', 'writeFiles')])
def test_a_scope_that_writes_under_a_hiding_scopes_prefix_is_refused_whatever_else_it_may_do(
    capabilities: tuple[str, ...],
) -> None:
    # Its uploads are files the hide reaches, whether or not it reads them.
    hiding = Scope(name='hiding', prefix='shared/', hide_after_days=3)
    writing = Scope(name='writing', prefix='shared/', capabilities=capabilities)

    with pytest.raises(OverlappingHide) as refused:
        build([writing, hiding])
    assert (refused.value.scope, refused.value.other) == ('hiding', 'writing')


@pytest.mark.asyncio
async def test_a_hide_inside_another_scopes_prefix_stays_on_its_own_files() -> None:
    inner = Scope(name='inner', prefix='volsync/immich/snapshots/', hide_after_days=3)
    bucket = build([INNER_PRUNING, inner])
    rules = await bucket.bucket.lifecycle_rules.future()
    assert rules is not None

    assert applied(rules, 'volsync/immich/snapshots/a-file').hide == 3
    assert applied(rules, 'volsync/immich/a-claim/data/00/a-pack').hide is None


@pytest.mark.asyncio
async def test_abandoned_multipart_uploads_are_canceled() -> None:
    rules = await fleet_rules()
    # Parts of a killed upload bill forever and show up in no listing. How
    # soon is the component's to choose; the rule is that a cancellation is
    # scheduled at all, for every file, a positive number of days after the
    # upload started. Whole days, compared by value: the mock monitor hands
    # numbers back as floats.
    for name in (ETCD_SNAPSHOT, written('immich')[BackupFamily.BARMAN], 'a-prefix-nobody-declared/a-file'):
        days = applied(rules, name).cancel
        assert days is not None
        assert days > 0
        assert days == int(days)


@pytest.mark.asyncio
async def test_the_bucket_is_private_and_protected() -> None:
    assert BUCKET_NAME != conventions.BUCKET_BACKUP
    bucket = build()
    assert await bucket.bucket.bucket_type.future() == 'allPrivate'
    assert await bucket.bucket.bucket_name.future() == BUCKET_NAME
    assert bucket.bucket._protect is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_keys_stay_unprotected_so_they_can_rotate() -> None:
    bucket = build()
    assert set(bucket.keys) == {scope.name for scope in SCOPES}

    for key in bucket.keys.values():
        # Rotation is a mint and a retire; protection would make the routine
        # act require an unprotect diff and thus tempt nobody to do it.
        assert key._protect is not True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_the_credential_halves_are_reachable_by_scope() -> None:
    bucket = build()
    key = bucket.keys['etcd']
    assert await bucket.key_id('etcd').future() == await key.application_key_id.future()
    assert await bucket.key_secret('etcd').future() == await key.application_key.future()
    assert await bucket.bucket_id.future() == BUCKET_ID


def test_a_prefix_without_a_separator_is_refused() -> None:
    # `volsync/app` also matches `volsync/apple/…`, so a key scoped that way
    # reads a neighbor's backups.
    with pytest.raises(UnseparatedPrefix) as refused:
        build([Scope(name='sloppy', prefix='volsync/app')])
    assert (refused.value.scope, refused.value.prefix) == ('sloppy', 'volsync/app')


def test_two_scopes_may_not_share_a_name() -> None:
    with pytest.raises(DuplicateScope) as refused:
        build([Scope(name='etcd', prefix='etcd/'), Scope(name='etcd', prefix='other/')])
    assert refused.value.scope == 'etcd'


def test_the_endpoint_names_the_account_region() -> None:
    assert s3_endpoint(REGION) == f'https://s3.{REGION}.backblazeb2.com'
    assert build().endpoint == f'https://s3.{REGION}.backblazeb2.com'


@pytest.mark.asyncio
async def test_the_account_key_is_read_where_the_provider_is_built() -> None:
    """The credential reaches no signature - not this component's, not below it.

    A provider and the secret that opens it are one thing (rfc-002 §8.1), and
    this account has exactly one consumer, so the provider is built here rather
    than by the stack program. What that buys is the property below: the key
    pair is a configuration read at one line, and a reader answering "what does
    this authenticate as" has one file to open.
    """
    bucket = build()

    assert await bucket.provider.application_key_id.future() == KEY_ID
    assert await bucket.provider.application_key.future() == KEY
    # Nothing threads it in: the constructor has no parameter that could.
    assert 'application_key' not in inspect.signature(BackupBucket.__init__).parameters


@pytest.mark.asyncio
async def test_every_resource_is_signed_by_that_provider(monitor: B2) -> None:
    """Inheritance, not re-plumbing: no child names the provider and all of them carry it.

    The bucket and its keys are children of the component, and a component's
    provider map is what a child inherits - so setting the provider once, on
    the component, reaches the whole subtree.
    """
    async with declaring():
        bucket = build()

    component = await bucket.urn.future()
    provider = await bucket.provider.urn.future()
    children = [request for request in monitor.registrations.values() if request.parent == component]

    assert children
    assert [child.name for child in children if not child.provider.startswith(f'{provider}::')] == []


@pytest.mark.parametrize('family', list(BackupFamily), ids=str)
def test_a_familys_scope_reaches_where_that_family_writes_and_nothing_beside_it(family: BackupFamily) -> None:
    """A family's key reaches the repositories or archives its namespace writes, and no other family's or namespace's.

    The prefix is held to where that family's repositories and archives are
    written, so the key reaches what it backs up and nothing beside it.
    """
    namespace = 'a-namespace'
    scope = physical.FAMILY_SCOPES[family](namespace)

    assert written(namespace)[family].startswith(scope.prefix)
    # Neither the other family's prefix nor a namespace whose name extends
    # this one's.
    assert not any(path.startswith(scope.prefix) for other, path in written(namespace).items() if other is not family)
    assert not written(f'{namespace}-other')[family].startswith(scope.prefix)
