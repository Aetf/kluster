"""The backup bucket, and the keys that may fill it but not empty it.

The bucket lives on Backblaze B2 rather than beside the cluster, and that is
the whole point: a backup kept at the provider whose loss it insures is not a
backup, and tenancy termination is an enumerated risk (storage.md §4,
nodes.md §3.1). Everything the cluster's durability rests on lands here —
etcd snapshots, VolSync's restic repositories, CNPG's barman archives
(storage.md §5).

**Backups are the HA mechanism, so their deletability is a threat.** A
compromised app namespace or a runaway CI job must not be able to destroy the
safety net it lives inside, and the design answers that with two rules which
only work together (storage.md §4, backup-integrity rules):

-   **No key in automation carries `deleteFiles`.** A writer key is
    `listFiles + readFiles + writeFiles` — list and read included because
    restic and barman cannot *back up* without reading their own index, so a
    literally write-only key is not an option. Without the delete capability
    a B2 deletion degrades to a **hide**: the version stops being current and
    stays recoverable.
-   **A lifecycle rule is what finally removes a hidden version**, after
    `conventions.BACKUP_VERSION_RETENTION_DAYS`. That is the floor under both
    ransomware and a fat finger, and it is also what lets each consumer keep
    running its own prune on its own retention class — the prune's deletions
    are hides, and the hides age out. Retention semantics survive; nothing in
    automation can destroy a version before its time.

**Each key reaches one prefix.** An app's restic key sees
`volsync/<its-namespace>/` and nothing else; barman and the etcd snapshots
likewise. The prefixes are not restated here — they come from the one bucket
layout in `conventions`, so the inventory of what is backed up stays derivable
from the program rather than from a key list.

The **management key** this component authenticates as is minted by the
`credentials` family from the B2 seed (docs/credentials.md §3) and read here,
at the line that builds the provider — this component is the account's only
consumer, so the connection is its own (rfc-002 §8.1). Not here at all: the
state-backend appliance's write-only dump key, which belongs to a different
bucket entirely (`kluster-state-backend`) because the dumps must have
somewhere to land before Pulumi exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pulumi
import pulumi_b2 as b2

from kluster import conventions
from putils import Component, own_provider_opts, with_provider

#: What a backup mover needs, and nothing beyond it. `readFiles` and
#: `listFiles` are not generosity: restic and barman read their own index
#: before every write, so a key without them cannot take a backup at all.
WRITER_CAPABILITIES: tuple[str, ...] = ('listFiles', 'readFiles', 'writeFiles')

#: The capability no key in any automation carries. Named rather than merely
#: absent, so the rule can be asserted instead of remembered.
FORBIDDEN_CAPABILITY = 'deleteFiles'

#: Unfinished large files are the invisible half of an object-storage bill: a
#: multipart upload killed mid-flight bills for its parts forever and appears
#: in no listing. A day is far longer than any mover's retry window.
UNFINISHED_UPLOAD_DAYS = 1

#: Where the backup account's key pair is read — here, at the line that builds
#: the provider, and nowhere else (rfc-002 §8.1). Both halves are secrets: the
#: id is not the key, but it names the one key of that name the account holds,
#: and the pair is one credential.
#:
#: The provider is built inside this component because this component is its
#: only consumer. If a second one ever declares against the account — a second
#: bucket, a restore drill with a credential of its own — it moves up to the
#: stack program, by the same test that puts the cloud provider there.
APPLICATION_KEY_ID = 'b2ApplicationKeyId'
APPLICATION_KEY = 'b2ApplicationKey'


@dataclass(frozen=True)
class Scope:
    """One consumer's reach into the bucket: a key name, and a prefix.

    The prefix ends in a separator, and that is load-bearing rather than
    cosmetic — B2 matches a name prefix as plain text, so `volsync/app` also
    matches `volsync/apple/…`. The trailing slash is what makes a prefix mean
    a directory.
    """

    name: str
    prefix: str


def etcd_scope() -> Scope:
    """The control plane's hourly snapshots, shipped by the ops-repo workflow."""
    return Scope(name='etcd', prefix=f'{conventions.ETCD_SNAPSHOT_PREFIX}/')


def volsync_scope(namespace: str) -> Scope:
    """One namespace's restic repositories — every backed-up PVC it owns."""
    return Scope(name=f'volsync-{namespace}', prefix=conventions.volsync_repo_path(namespace, ''))


def barman_scope(namespace: str) -> Scope:
    """One namespace's database archives, base backups and WAL alike."""
    return Scope(name=f'cnpg-{namespace}', prefix=conventions.barman_repo_path(namespace, ''))


class BackupBucket(Component):
    """The backup bucket, its version-retention rule, and one key per consumer.

    `scopes` is the roll of consumers, and it arrives from the caller: which
    namespaces exist is not this module's to know, and the consumers that exist
    whether or not any application does are no more this module's than the
    rest.
    """

    def __init__(
        self,
        name: str,
        *,
        region: str,
        bucket_name: str = conventions.BUCKET_BACKUP,
        retention_days: int = conventions.BACKUP_VERSION_RETENTION_DAYS,
        unfinished_upload_days: int = UNFINISHED_UPLOAD_DAYS,
        scopes: Sequence[Scope],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        _check(scopes)
        config = pulumi.Config()
        provider = b2.Provider(
            f'{name}-b2',
            application_key_id=config.require_secret(APPLICATION_KEY_ID),
            application_key=config.require_secret(APPLICATION_KEY),
            opts=own_provider_opts(opts),
        )
        super().__init__(name, opts=with_provider(opts, provider))
        self.provider = provider
        self.region = region
        self.bucket_name = bucket_name
        self.scopes = tuple(scopes)

        self.bucket = b2.Bucket(
            f'{name}-backup',
            bucket_name=bucket_name,
            bucket_type='allPrivate',
            lifecycle_rules=[
                b2.BucketLifecycleRuleArgs(
                    # The whole bucket: every consumer's versions age out on
                    # the same floor, because the floor is a property of the
                    # bucket rather than a favour each consumer does itself.
                    file_name_prefix='',
                    # Deliberately no `days_from_uploading_to_hiding`: that
                    # one hides *current* files on a timer, which for a backup
                    # bucket means deleting live backups on a schedule.
                    days_from_hiding_to_deleting=retention_days,
                    days_from_starting_to_canceling_unfinished_large_files=unfinished_upload_days,
                )
            ],
            # The safety net every other resource's destroy path assumes.
            opts=self.child_opts(protect=True),
        )

        # Keys are not protected. They are the one thing here meant to be
        # replaced freely — rotation is a mint and a retire, and a protected
        # key would turn routine rotation into an unprotect diff.
        self.keys = {
            scope.name: b2.ApplicationKey(
                f'{name}-backup-{scope.name}',
                key_name=f'{bucket_name}-{scope.name}',
                capabilities=list(WRITER_CAPABILITIES),
                # `bucket_ids` rather than the deprecated singular field; the
                # key sees this bucket and no other in the account.
                bucket_ids=[self.bucket.bucket_id],
                name_prefix=scope.prefix,
                opts=self.child_opts(),
            )
            for scope in scopes
        }

        self.register_outputs({})

    @property
    def bucket_id(self) -> pulumi.Output[str]:
        """The bucket's id, which is what a key is scoped against."""
        return self.bucket.bucket_id

    @property
    def endpoint(self) -> str:
        """The S3-compatible endpoint restic, barman and the mover speak."""
        return s3_endpoint(self.region)

    def key_id(self, scope: str) -> pulumi.Output[str]:
        """The public half of one consumer's credential."""
        return self.keys[scope].application_key_id

    def key_secret(self, scope: str) -> pulumi.Output[str]:
        """The secret half. The provider classifies it; this only passes it on."""
        return self.keys[scope].application_key


def s3_endpoint(region: str) -> str:
    """B2's S3-compatible endpoint for an account's region.

    The region is a property of the account rather than of the bucket, and no
    API call returns it in this form — it is read off the bucket's own page in
    the console (`us-west-004` and the like) and carried as configuration.
    """
    return f'https://s3.{region}.backblazeb2.com'


def _check(scopes: Sequence[Scope]) -> None:
    """Refuse a scope set that would hand a key more than its own prefix."""
    seen: set[str] = set()
    for scope in scopes:
        if not scope.prefix.endswith('/'):
            raise ValueError(
                f'scope {scope.name!r} has prefix {scope.prefix!r}: a prefix without a trailing '
                'separator also matches its siblings, which is the opposite of prefix-scoping'
            )
        if scope.name in seen:
            raise ValueError(f'scope {scope.name!r} is declared twice')
        seen.add(scope.name)
