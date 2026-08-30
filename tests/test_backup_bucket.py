"""The backup bucket and its writer keys.

Every assertion here is about a rule that is invisible in a later diff because
it is a rule about *absence*: that no key can delete a file, that no key can
read a prefix that is not its own, and that the lifecycle rule removes old
versions without ever hiding current ones. A backup regime fails silently when
any of those three slips, and it fails at exactly the moment it is needed.
"""

import inspect
from typing import Any

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.backup import (
    CLUSTER_SCOPES,
    FORBIDDEN_CAPABILITY,
    UNFINISHED_UPLOAD_DAYS,
    WRITER_CAPABILITIES,
    BackupBucket,
    Scope,
    barman_scope,
    etcd_scope,
    s3_endpoint,
    volsync_scope,
)

BUCKET_ID = 'b2-bucket-id'
REGION = 'us-west-004'


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


def build(scopes: list[Scope] = list(CLUSTER_SCOPES)) -> BackupBucket:
    return BackupBucket('kluster', region=REGION, scopes=scopes)


@pytest.mark.asyncio
async def test_no_key_can_delete_a_file() -> None:
    bucket = build([etcd_scope(), volsync_scope('immich'), barman_scope('immich')])
    for key in bucket.keys.values():
        capabilities = await key.capabilities.future()
        assert capabilities is not None
        # This is the rule the whole backup-integrity design rests on: without
        # the capability a deletion is a hide, and a hide is recoverable.
        assert FORBIDDEN_CAPABILITY not in capabilities


@pytest.mark.asyncio
async def test_a_writer_key_can_still_read_its_own_index() -> None:
    bucket = build()
    for key in bucket.keys.values():
        capabilities = await key.capabilities.future()
        assert capabilities is not None
        # A literally write-only key cannot back anything up: restic and
        # barman read their own index before they write.
        assert set(capabilities) == set(WRITER_CAPABILITIES)


@pytest.mark.asyncio
async def test_each_key_reaches_one_prefix_and_one_bucket() -> None:
    bucket = build([etcd_scope(), volsync_scope('immich'), barman_scope('immich')])

    prefixes = {name: await key.name_prefix.future() for name, key in bucket.keys.items()}
    assert prefixes == {
        'etcd': f'{conventions.ETCD_SNAPSHOT_PREFIX}/',
        'volsync-immich': conventions.volsync_repo_path('immich', ''),
        'cnpg-immich': conventions.barman_repo_path('immich', ''),
    }

    for key in bucket.keys.values():
        # Scoped to this bucket, not to the account: a key that could reach
        # another bucket is not prefix-scoped, it is merely prefix-flavoured.
        assert await key.bucket_ids.future() == [BUCKET_ID]


def test_the_prefixes_come_from_the_one_bucket_layout() -> None:
    # Not a restatement of the layout: if `conventions` moves the repository
    # path, the key that guards it moves with it rather than silently guarding
    # a directory nothing writes to any more.
    assert volsync_scope('media').prefix == conventions.volsync_repo_path('media', '')


@pytest.mark.asyncio
async def test_old_versions_age_out_and_current_ones_never_do() -> None:
    bucket = build()
    rules = await bucket.bucket.lifecycle_rules.future()
    assert rules is not None
    assert len(rules) == 1
    rule = rules[0]

    assert rule.days_from_hiding_to_deleting == conventions.BACKUP_VERSION_RETENTION_DAYS
    # The dangerous knob: `daysFromUploadingToHiding` hides *current* files on
    # a timer, which in a backup bucket is a scheduled deletion of live
    # backups. It must stay unset.
    assert rule.days_from_uploading_to_hiding is None
    # The floor is a property of the bucket, so it covers a consumer nobody
    # remembered to add a rule for.
    assert rule.file_name_prefix == ''


@pytest.mark.asyncio
async def test_abandoned_multipart_uploads_are_cancelled() -> None:
    bucket = build()
    rules = await bucket.bucket.lifecycle_rules.future()
    assert rules is not None
    # Parts of a killed upload bill forever and show up in no listing.
    assert rules[0].days_from_starting_to_canceling_unfinished_large_files == UNFINISHED_UPLOAD_DAYS


@pytest.mark.asyncio
async def test_the_bucket_is_private_and_protected() -> None:
    bucket = build()
    assert await bucket.bucket.bucket_type.future() == 'allPrivate'
    assert await bucket.bucket.bucket_name.future() == conventions.BUCKET_BACKUP
    assert bucket.bucket._protect is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_keys_stay_unprotected_so_they_can_rotate() -> None:
    bucket = build()
    for key in bucket.keys.values():
        # Rotation is a mint and a retire; protection would make the routine
        # act require an unprotect diff and thus tempt nobody to do it.
        assert key._protect is not True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_the_consumers_that_exist_without_any_app() -> None:
    bucket = build()
    # etcd is backed up whether or not a single application is declared; every
    # other scope is per namespace and arrives from the caller.
    assert set(bucket.keys) == {scope.name for scope in CLUSTER_SCOPES} == {'etcd'}


@pytest.mark.asyncio
async def test_the_credential_halves_are_reachable_by_scope() -> None:
    bucket = build()
    assert await bucket.key_id('etcd').future() == 'kluster-backup-etcd-key-id'
    assert await bucket.key_secret('etcd').future() == 'kluster-backup-etcd-secret'
    assert await bucket.bucket_id.future() == BUCKET_ID


def test_a_prefix_without_a_separator_is_refused() -> None:
    # `volsync/app` also matches `volsync/apple/…`, so a key scoped that way
    # reads a neighbour's backups.
    with pytest.raises(ValueError, match='trailing separator'):
        build([Scope(name='sloppy', prefix='volsync/app')])


def test_two_scopes_may_not_share_a_name() -> None:
    with pytest.raises(ValueError, match='declared twice'):
        build([Scope(name='etcd', prefix='etcd/'), Scope(name='etcd', prefix='other/')])


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

    assert set(bucket.keys) == {'etcd'}
    for name in ('kluster-backup', 'kluster-backup-etcd'):
        assert 'kluster-b2' in monitor.provider_of(name), f'{name} is not signed by the bucket provider'
