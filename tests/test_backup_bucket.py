"""The backup bucket and its writer keys.

Every assertion here is about a rule that is invisible in a later diff because
it is a rule about *absence*: that no key can delete a file, that no key can
read a prefix that is not its own, and that the lifecycle rule removes old
versions without ever hiding current ones. A backup regime fails silently when
any of those three slips, and it fails at exactly the moment it is needed.
"""

from typing import Any, cast

import pulumi
import pulumi.runtime.settings
import pytest
import pytest_asyncio

from kluster import conventions

BUCKET_ID = 'b2-bucket-id'
REGION = 'us-west-004'


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        if args.typ == 'b2:index/bucket:Bucket':
            outputs['bucketId'] = BUCKET_ID
        if args.typ == 'b2:index/applicationKey:ApplicationKey':
            outputs['applicationKeyId'] = args.name + '-key-id'
            outputs['applicationKey'] = args.name + '-secret'
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        return {}, []


@pytest_asyncio.fixture(autouse=True)
async def setup_mocks() -> None:
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='physical', preview=False)
    # A bridged SDK is a *parameterized* package: before it may register a
    # resource it registers its own package, and the SDK gates that on a
    # feature flag it reads out of a synchronous cache. The mock monitor
    # answers the feature and serves the registration, but nothing on the mock
    # path performs the async negotiation that fills the cache, so a bridged
    # provider refuses under mocks until it is primed once.
    _ = await pulumi.runtime.settings.monitor_supports_feature('parameterization')


def build(scopes: Any = None) -> Any:
    from kluster.physical.backup import CLUSTER_SCOPES, BackupBucket

    return BackupBucket(
        'kluster',
        region=REGION,
        scopes=CLUSTER_SCOPES if scopes is None else scopes,
    )


@pytest.mark.asyncio
async def test_no_key_can_delete_a_file() -> None:
    from kluster.physical.backup import FORBIDDEN_CAPABILITY, barman_scope, etcd_scope, volsync_scope

    bucket = build([etcd_scope(), volsync_scope('immich'), barman_scope('immich')])
    for key in bucket.keys.values():
        capabilities = await key.capabilities.future()
        assert capabilities is not None
        # This is the rule the whole backup-integrity design rests on: without
        # the capability a deletion is a hide, and a hide is recoverable.
        assert FORBIDDEN_CAPABILITY not in capabilities


@pytest.mark.asyncio
async def test_a_writer_key_can_still_read_its_own_index() -> None:
    from kluster.physical.backup import WRITER_CAPABILITIES

    bucket = build()
    for key in bucket.keys.values():
        capabilities = await key.capabilities.future()
        assert capabilities is not None
        # A literally write-only key cannot back anything up: restic and
        # barman read their own index before they write.
        assert set(capabilities) == set(WRITER_CAPABILITIES)


@pytest.mark.asyncio
async def test_each_key_reaches_one_prefix_and_one_bucket() -> None:
    from kluster.physical.backup import barman_scope, etcd_scope, volsync_scope

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


@pytest.mark.asyncio
async def test_the_prefixes_come_from_the_one_bucket_layout() -> None:
    from kluster.physical.backup import volsync_scope

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
    from kluster.physical.backup import UNFINISHED_UPLOAD_DAYS

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
    from kluster.physical.backup import CLUSTER_SCOPES

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
    from kluster.physical.backup import Scope

    # `volsync/app` also matches `volsync/apple/…`, so a key scoped that way
    # reads a neighbour's backups.
    with pytest.raises(ValueError, match='trailing separator'):
        build([Scope(name='sloppy', prefix='volsync/app')])


def test_two_scopes_may_not_share_a_name() -> None:
    from kluster.physical.backup import Scope

    with pytest.raises(ValueError, match='declared twice'):
        build([Scope(name='etcd', prefix='etcd/'), Scope(name='etcd', prefix='other/')])


def test_the_endpoint_names_the_account_region() -> None:
    from kluster.physical.backup import s3_endpoint

    assert s3_endpoint(REGION) == f'https://s3.{REGION}.backblazeb2.com'
    assert build().endpoint == f'https://s3.{REGION}.backblazeb2.com'
