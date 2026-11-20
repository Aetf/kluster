"""The application base's database: one CNPG instance, its archive in the backup bucket, and its restore drill.

One call, `database` (declarative/workloads.md §4, rfc-008 §4.3), which emits:

-   **The `Cluster`**, `instances: 1` and no standby: every database is pinned
    to one site, and a second instance there would share the first's node,
    disk and failure domain. Its data is a claim the operator makes under the
    class it is handed, `local-path` unless a node volume's, placed by the
    node affinity it is handed, which is how a database follows its
    application's site. It is protected, as every data-bearing resource is
    (storage.md §3.3), and it has no disruption budget: over one instance,
    CNPG's budget would refuse every eviction of it and hang each drain of
    its node, and it protects nothing the archive does not.
-   **Its archive**: the Barman Cloud plugin as its WAL archiver, to the
    backup bucket at `conventions.barman_repo_path` of its namespace and its
    server name, through an `ObjectStore` whose credential is the namespace's
    sealed writer key of the barman family (`conventions.sealed.writer_key`),
    and a `ScheduledBackup` on its retention class's schedule. The plugin's
    sidecar in each instance and each recovery is held to
    `SIDECAR_RESOURCES`. **The server name carries a generation**
    (`server_name`): barman refuses to archive a new cluster into a server's
    path that already holds an archive, so a land retried after a revert
    raises the generation and archives to a path of its own (rfc-008 §3.6).
-   **Its bootstrap**: an empty database, or **a recovery, once, from a named
    object store** (`Recovery`), the legacy store with its ported credential,
    after which it archives to the bucket like any other. The legacy store is
    read by nothing after that.
-   **Its restore drill**: a CronJob that recovers a throwaway cluster from
    the newest backup in the bucket, checks that it has left recovery and
    that its rows and extensions are there, and deletes it, monthly
    (operations.md §4). The drill is `restore_drill.py` beside this module,
    which the drill image's `python3` runs. **It is watched by the
    scheduled-job family**, and its facts are spelled once, here: `drill_job`
    is its `ScheduledJob`, which names the CronJob, sets its deadlines and
    gives the family's two rules (`components.monitoring.rules
    .scheduled_job_rules`), which the shape declares beside the CronJob as a
    `VMRule` in the database's namespace. vmalert selects rules in every
    namespace, which the monitoring component declares.
-   **The drill's admission parameters**: the drill's role creates
    `Cluster`s under any name, and a `Cluster`'s labels, annotations and
    `inheritedMetadata` reach the pods the operator starts for it, so the
    role alone would let a poisoned drill image start a pod carrying any
    label a grant is keyed on (threat-model.md §2.6). One policy for every
    database, `drill_admission`, holds the name, the labels, the
    annotations and the `inheritedMetadata` of each `Cluster` a drill's
    account creates or changes to the ones the drill's manifest gives, and
    not the rest of its `spec` (Aetf/kluster-ops#889). The shape declares
    what the policy reads, a `ConfigMap` derived from the manifest
    (`_drill_parameters`) and labelled `DRILL_ADMISSION_LABEL`. The policy
    and its one binding are cluster-scoped, and an application declares no
    cluster-scoped object (declarative/workloads.md §1), so `k8s-base`
    declares them, with the database operator
    (`kluster.components.postgres.PostgresOperator`).
-   **The operator's reach to its instances**: the namespace's default-deny
    admits no other namespace, and the operator reads each instance's status
    on the instance manager's port (`STATUS_PORT`).

**The instances, the drill's own recovered instance and the drill's pod call
the API**, so each carries `conventions.API_CLIENT_LABEL`: the instances
through the `Cluster`'s `inheritedMetadata`, which the operator copies onto
every pod it creates, the drill's pod through its template. The label opens
the API only in a namespace that declares its clients, which
`API_CLIENTS` is the declaration of (`kluster.lib.k8s.application_namespace`).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.backup import s3_endpoint
from kluster.components.monitoring.operator_objects import operator_object
from kluster.components.monitoring.rules import RuleGroup, rule_group_spec, scheduled_job_rules
from kluster.conventions.alert import Playbook
from kluster.conventions.backup import RetentionClass
from kluster.conventions.cluster import RETENTION_LABEL, WRITER_KEY_FIELDS
from kluster.conventions.schedule import ScheduledJob
from kluster.conventions.sealed import SealingScope
from kluster.lib import templates
from kluster.lib.k8s import ApiClients, SecretTemplate, sealed_secret
from kluster.lib.versions import ImagePin

__all__ = (
    'API_CLIENTS',
    'DATABASE_LABEL',
    'DRILL_ADMISSION_LABEL',
    'DRILL_PLAYBOOK',
    'DRILL_POLICY',
    'DRILL_USER',
    'PLUGIN',
    'SIDECAR_RESOURCES',
    'STATUS_PORT',
    'Database',
    'DatabaseRefused',
    'Drill',
    'DrillAdmission',
    'Recovery',
    'Refusal',
    'Resources',
    'barman_schedule',
    'database',
    'drill_admission',
    'drill_job',
    'recovery_window',
    'server_name',
)

#: The Barman Cloud plugin, by the name a `Cluster` and a `ScheduledBackup`
#: name it under.
PLUGIN = 'barman-cloud.cloudnative-pg.io'

#: The label every pod of a database carries, with the database's name: its
#: instances and the drill's recovered instance, through `inheritedMetadata`.
#: What the operator's reach selects.
DATABASE_LABEL = f'{conventions.LABEL_DOMAIN}/database'

#: The admission policy that holds the name, labels, annotations and
#: `inheritedMetadata` of every database drill's cluster to the drill's
#: manifest, and its one binding, which shares its name (`drill_admission`).
DRILL_POLICY = 'database-restore-drill'

#: The label a database's drill admission parameters carry, with the
#: database's name: what the binding of `DRILL_POLICY` selects them by, in
#: the namespace of the request it judges.
DRILL_ADMISSION_LABEL = f'{conventions.LABEL_DOMAIN}/restore-drill'

#: The instance manager's port, where the operator reads each instance's
#: status: "The operator needs to communicate to each instance on TCP port
#: 8000" (CloudNativePG's `docs/src/security.md`, "Network Policies", at
#: v1.30.0).
STATUS_PORT = 8000

#: The namespace's declaration of its API clients: the instance manager in
#: each instance keeps its `Cluster`'s status, and the drill drives a cluster
#: of its own (CloudNativePG's `docs/src/security.md`, "Calls to the API
#: server made by the instance manager").
API_CLIENTS = ApiClients(
    reason="a database's instances keep their cluster's status, and its restore drill recovers a cluster of its own"
)

#: The user and group the drill's pod runs as: `nobody`. The image names no
#: user, and the pod writes nothing but its own scratch directory.
DRILL_USER = 65534

#: A drill monthly, on the first of the month, at operations.md §4's cadence.
#: Its period is the longest month, so a drill that succeeds on the first of
#: every month is never reported stale.
_DRILL_SCHEDULE = '0 4 1 * *'
_DRILL_PERIOD = dt.timedelta(days=31)
#: A run recovers a throwaway cluster from the newest base backup and the WAL
#: after it, so it is cut off at an hour; a run the controller could not start
#: within the hour is left to the next month's.
_DRILL_DEADLINE = dt.timedelta(hours=1)
_DRILL_STARTING_DEADLINE = dt.timedelta(hours=1)
#: Where the response to a drill's alerts is written.
DRILL_PLAYBOOK = Playbook('docs/operations.md', '9.1')
#: What the drill keeps of its deadline for its checks and its teardown, past
#: the wait for the recovered cluster.
_DRILL_CHECKS = dt.timedelta(minutes=10)

#: What the drill's pod asks for and is held to: one `kubectl` at a time.
_DRILL_RESOURCES: Mapping[str, Mapping[str, str]] = {
    'requests': {'cpu': '50m', 'memory': '64Mi'},
    'limits': {'cpu': '500m', 'memory': '256Mi'},
}

#: Where the drill's ConfigMap is mounted, and its scratch directory, which
#: is also its `HOME`, where `kubectl` keeps its cache.
_DRILL_DIR = '/drill'
_SCRATCH_DIR = '/scratch'
_DRILL_SCRIPT = 'drill.py'
#: The interpreter the drill runs under, the drill image's own.
_PYTHON = '/usr/bin/python3'
_DRILL_MANIFEST = 'cluster.json'

#: What a database's name takes to name its drill's CronJob, and the
#: service account the drill runs as, which `DRILL_POLICY` matches by it.
_DRILL_SUFFIX = '-restore-drill'
#: The user name of a service account, ahead of `<namespace>:<name>`: "the
#: service account username, system:serviceaccount:(NAMESPACE):(SERVICEACCOUNT)"
#: (Kubernetes' `docs/reference/access-authn-authz/authentication.md`).
_SERVICE_ACCOUNT_USER = 'system:serviceaccount:'
#: What a drill's cluster carries that reaches the pods the operator starts
#: for it, each held exactly to the drill's manifest by `DRILL_POLICY`: the
#: parameter's key, and its path in the `Cluster`. The operator copies
#: `inheritedMetadata` onto every pod, and the `Cluster`'s own labels and
#: annotations onto them by the patterns its `INHERITED_LABELS` and
#: `INHERITED_ANNOTATIONS` name (CloudNativePG's `docs/src/operator_conf.md`);
#: and it applies the `Cluster`'s `cnpg.io/podPatch` annotation to each
#: instance pod as a JSON patch, which writes the pod's labels as freely as
#: `inheritedMetadata` does (`buildInstance` in CloudNativePG's
#: `pkg/specs/pods.go`, at v1.30.0).
_HELD: Mapping[str, tuple[str, ...]] = {
    'labels': ('metadata', 'labels'),
    'annotations': ('metadata', 'annotations'),
    'inheritedLabels': ('spec', 'inheritedMetadata', 'labels'),
    'inheritedAnnotations': ('spec', 'inheritedMetadata', 'annotations'),
}
#: The annotation client-side `kubectl apply` writes the configuration it
#: applied under, which the drill's apply of its manifest therefore carries.
_LAST_APPLIED = 'kubectl.kubernetes.io/last-applied-configuration'
#: What a held map may carry beside the manifest's own entries, by its key in
#: `_HELD`: the annotation the drill's own `kubectl apply` writes, with any
#: value. CloudNativePG's defaulting webhook, which runs before the policy,
#: writes no annotation and no label (`api/v1/cluster_defaults.go`, at
#: v1.30.0).
_TOLERATED: Mapping[str, tuple[str, ...]] = {'annotations': (_LAST_APPLIED,)}

#: The name the drill's recovered cluster gives the store it recovers from.
_DRILL_SOURCE = 'backup'
#: The name a migrated database gives the store it recovers from.
_RECOVERY_SOURCE = 'legacy'

#: The key the bucket's Secret carries beside the writer key's halves: the
#: account's region, which the archiver signs its requests for.
_REGION_KEY = 'AWS_REGION'

#: A name the drill's checks may hold in SQL: a plain identifier.
_IDENTIFIER = re.compile(r'[a-z_][a-z0-9_]*', re.IGNORECASE)

#: The drill's script, run by the drill image's `python3` (`restore_drill`).
_DRILL_PY = templates.load('kluster.components.apps', 'restore_drill.py')

#: What the Barman Cloud plugin's sidecar asks for and is held to, in every
#: instance and every recovery: its CPU is limited because an instance may
#: run on a cloud node beside etcd (declarative/workloads.md §1). A bound
#: rather than a measurement: the first base backup reads what an upload
#: takes.
SIDECAR_RESOURCES: Mapping[str, Mapping[str, str]] = {
    'requests': {'cpu': '25m', 'memory': '64Mi'},
    'limits': {'cpu': '250m', 'memory': '256Mi'},
}


class Refusal(StrEnum):
    """What a database's arguments say that no database can be, which `database` refuses before it declares anything."""

    SIZE = 'size'
    """A claim smaller than a GiB."""
    GENERATION = 'generation'
    """A generation below the first, 0."""
    WRITER_KEY = 'writer-key'
    """A writer key whose fields are not the two the archive reads."""
    IDENTIFIER = 'identifier'
    """A table or an extension for the drill that is no plain SQL name, which its queries would quote wrongly."""
    STORE = 'store'
    """A store to recover from that the Google credential cannot read."""
    HELD_VALUE = 'held-value'
    """A label or annotation the drill's cluster carries whose value holds a newline, which its parameters cannot carry."""


class DatabaseRefused(ValueError):
    """A database refused, and which of its arguments refused it."""

    def __init__(self, refusal: Refusal, message: str) -> None:
        super().__init__(message)
        self.refusal = refusal


def drill_job(namespace: str, name: str) -> ScheduledJob:
    """A database's drill as the scheduled-job family reads it: its CronJob, its cadence, its deadlines and its playbook."""
    return ScheduledJob(
        alert='DatabaseRestoreDrill',
        namespace=namespace,
        cronjob=f'{name}{_DRILL_SUFFIX}',
        period=_DRILL_PERIOD,
        deadline=_DRILL_DEADLINE,
        starting_deadline=_DRILL_STARTING_DEADLINE,
        playbook=DRILL_PLAYBOOK,
    )


def server_name(name: str, generation: int) -> str:
    """The server name a database of one generation archives under, the last segment of its archive's path."""
    return f'{name}-g{generation}'


def barman_schedule(retention: RetentionClass) -> str:
    """A retention class's schedule as a `ScheduledBackup` reads it, with a seconds field ahead of the cron line's.

    Its schedule "does not follow the same format used in Kubernetes
    CronJobs as it includes an additional seconds specifier" (the definition
    of `ScheduledBackup.spec.schedule`).
    """
    return f'0 {retention.schedule}'


def recovery_window(retention: RetentionClass) -> str:
    """A retention class as barman keeps backups: a recovery window as deep as the class's deepest tier.

    barman keeps every backup inside its window and thins nothing, so the
    window is how far back the class reaches, in the unit of its deepest
    tier: `12m` for a year of monthlies, `4w` for four weeklies, `30d` for
    thirty dailies. A class reaching back only hours keeps a day.
    """
    if retention.monthly:
        return f'{retention.monthly}m'
    if retention.weekly:
        return f'{retention.weekly}w'
    if retention.daily:
        return f'{retention.daily}d'
    if retention.hourly:
        return '1d'
    raise ValueError(f'the retention class {retention.name!r} keeps nothing')


@dataclass(frozen=True, kw_only=True)
class Recovery:
    """The object store a migrated database recovers from once, at its land, and the credential that reads it.

    The legacy databases archive to Google Cloud Storage, one bucket each, so
    the store is a `gs://` path and its credential a Google service account's
    key, the legacy Secret's ciphertext ported as it is.
    """

    destination_path: str
    """The store's path, `gs://<bucket>`."""
    server_name: str
    """The legacy cluster's server name in the store: its `Cluster`'s name."""
    key: str
    """The data key the legacy Secret holds the service account's key under."""
    ciphertext: pulumi.Input[str]
    """The ported ciphertext of that key."""


@dataclass(frozen=True, kw_only=True)
class Resources:
    """What an instance asks for and is held to: on the cloud pool, its CPU limit is required (declarative/workloads.md §1)."""

    requests: Mapping[str, str]
    limits: Mapping[str, str]


@dataclass(frozen=True, kw_only=True)
class Drill:
    """What the restore drill checks on the recovered database, beside its having left recovery."""

    table: str
    """A table of the application's that holds rows on any database in use."""
    extensions: Sequence[str] = ()
    """The extensions the application needs, each present once recovered."""


@dataclass(frozen=True)
class Database:
    """What `database` emitted.

    `name` is the `Cluster`'s, which its Services are named after: the
    application reaches its database at `<name>-rw`.
    """

    name: str
    cluster: crds.postgresql.v1.Cluster
    credential: crds.bitnami.v1alpha1.SealedSecret
    """The writer key the archive is written with, and the account's region."""
    store: crds.barmancloud.v1.ObjectStore
    """The archive in the backup bucket, which the drill recovers from too."""
    backup: crds.postgresql.v1.ScheduledBackup
    recovery: tuple[crds.bitnami.v1alpha1.SealedSecret, crds.barmancloud.v1.ObjectStore] | None
    """The ported credential and the store a migrated database recovers from."""
    operator_reach: crds.cilium.v2.CiliumNetworkPolicy
    drill: k8s.batch.v1.CronJob
    drill_role: k8s.rbac.v1.Role
    drill_parameters: k8s.core.v1.ConfigMap
    """What `DRILL_POLICY` holds the drill's account to, derived from its manifest."""
    drill_rules: k8s.apiextensions.CustomResource
    """The scheduled-job family's two rules over the drill."""


def database(
    resource_name: str,
    name: str,
    size: int,
    retention: RetentionClass,
    *,
    namespace: str,
    image: ImagePin,
    dbname: str,
    resources: Resources,
    node_affinity: Mapping[str, Any],
    writer_key: Mapping[str, pulumi.Input[str]],
    drill: Drill,
    drill_image: ImagePin,
    generation: int = 0,
    recovery: Recovery | None = None,
    shared_preload_libraries: Sequence[str] = (),
    parameters: Mapping[str, str] | None = None,
    post_init_application_sql: Sequence[str] = (),
    post_init_template_sql: Sequence[str] = (),
    storage_class: str = conventions.SC_LOCAL_PATH,
    opts: pulumi.ResourceOptions | None = None,
) -> Database:
    """One database instance, archived to the backup bucket by its class, recovered once where it migrates, and drilled.

    :param resource_name: The logical name, which carries the declaring
        component's name.
    :param name: The `Cluster`'s name, which its Services, its archive's
        server name and every object beside it are derived from.
    :param size: The instance's claim, in GiB.
    :param retention: The retention class its base backups are scheduled
        and kept by (`barman_schedule`, `recovery_window`).
    :param namespace: The application's namespace, which the archive's path
        is under and which declares `API_CLIENTS`; the caller's `opts`
        carries the dependency on the Namespace resource, since this is a
        plain name.
    :param image: The operand, the image every instance runs, the drill's
        recovered one included.
    :param dbname: The application's database, and its owner.
    :param resources: The instance's requests and limits, which the drill's
        recovered instance takes too.
    :param node_affinity: Kubernetes' `NodeAffinity` as JSON, the site's pool
        the instance and the drill's recovered instance are placed in.
    :param writer_key: The ciphertext of the namespace's sealed writer key of
        the barman family, as the program read it
        (`kluster.lib.k8s.sealed_ciphertext` over
        `conventions.sealed.writer_key(namespace, BackupFamily.BARMAN)`).
    :param drill: What the restore drill checks once the database recovered.
    :param drill_image: The image the drill runs in, whose `python3` runs its
        script and whose `kubectl` it calls.
    :param generation: The archive's generation, raised for each land retried
        after a revert.
    :param recovery: The store a migrated database recovers from at its land;
        `None` initializes an empty database.
    :param shared_preload_libraries: The libraries every instance preloads,
        which its extensions need.
    :param parameters: PostgreSQL's settings beside the operator's own.
    :param post_init_application_sql: What `initdb` runs in the application's
        database as the superuser, on an empty database alone.
    :param post_init_template_sql: What `initdb` runs in `template1`, on an
        empty database alone.
    :param storage_class: `local-path`, or a node volume's class.
    """
    if size < 1:
        raise DatabaseRefused(
            Refusal.SIZE, f'{resource_name}: a database is at least 1 GiB, and {name!r} asks for {size}'
        )
    if generation < 0:
        raise DatabaseRefused(
            Refusal.GENERATION, f'{resource_name}: a generation counts from 0, and {name!r} is at {generation}'
        )
    if frozenset(writer_key) != WRITER_KEY_FIELDS:
        raise DatabaseRefused(
            Refusal.WRITER_KEY,
            f'{resource_name}: the writer key of {namespace!r} holds {sorted(writer_key)}, '
            f'and the archive reads {sorted(WRITER_KEY_FIELDS)}',
        )
    if recovery is not None:
        _check_recovery(resource_name, name, recovery)
    for identifier in (drill.table, *drill.extensions):
        if not _IDENTIFIER.fullmatch(identifier):
            raise DatabaseRefused(
                Refusal.IDENTIFIER, f'{resource_name}: the drill checks {identifier!r}, which is no plain SQL name'
            )

    server = server_name(name, generation)
    labels = {**conventions.API_CLIENT_LABEL, DATABASE_LABEL: name}
    instance = _Instance(
        image=str(image),
        size=size,
        resources=resources,
        node_affinity=node_affinity,
        labels=labels,
        shared_preload_libraries=tuple(shared_preload_libraries),
        parameters=dict(parameters or {}),
    )
    job = drill_job(namespace, name)
    manifest = _drill_manifest(name, instance, dbname=dbname, server=server, namespace=namespace)
    admitted = _drill_parameters(resource_name, manifest, account=f'{_SERVICE_ACCOUNT_USER}{namespace}:{job.cronjob}')

    credential = _credential(resource_name, name, namespace=namespace, writer_key=writer_key, opts=opts)
    store = _store(resource_name, name, retention, namespace=namespace, opts=opts)
    recovered = (
        _recovery_store(resource_name, name, recovery, namespace=namespace, opts=opts) if recovery is not None else None
    )
    cluster = crds.postgresql.v1.Cluster(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(name=name, namespace=namespace),
        spec=crds.postgresql.v1.ClusterSpecArgs(
            instances=1,
            # No disruption budget: over one instance it refuses every
            # eviction, so a drain of the node hangs, and it protects
            # nothing the archive does not.
            enable_pdb=False,
            image_name=instance.image,
            storage=crds.postgresql.v1.ClusterSpecStorageArgs(storage_class=storage_class, size=f'{size}Gi'),
            resources=crds.postgresql.v1.ClusterSpecResourcesArgs(
                requests=dict(resources.requests), limits=dict(resources.limits)
            ),
            affinity=crds.postgresql.v1.ClusterSpecAffinityArgs(node_affinity=_as_given(node_affinity)),
            inherited_metadata=crds.postgresql.v1.ClusterSpecInheritedMetadataArgs(labels=labels),
            postgresql=_postgresql(instance),
            bootstrap=_bootstrap(
                dbname,
                recovery=recovery,
                post_init_application_sql=post_init_application_sql,
                post_init_template_sql=post_init_template_sql,
            ),
            external_clusters=(
                [_external(_RECOVERY_SOURCE, f'{name}-recovery', recovery.server_name)]
                if recovery is not None
                else None
            ),
            plugins=[
                crds.postgresql.v1.ClusterSpecPluginsArgs(
                    name=PLUGIN,
                    is_wal_archiver=True,
                    parameters={'barmanObjectName': _store_name(name), 'serverName': server},
                )
            ],
        ),
        opts=pulumi.ResourceOptions.merge(
            opts,
            # Protected, as every data-bearing resource is (storage.md §3.3):
            # its claim goes with it, under a class that reclaims `Delete`.
            pulumi.ResourceOptions(protect=True, depends_on=[credential, store, *(recovered or ())]),
        ),
    )
    backup = crds.postgresql.v1.ScheduledBackup(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(name=name, namespace=namespace, labels={RETENTION_LABEL: retention.name}),
        spec=crds.postgresql.v1.ScheduledBackupSpecArgs(
            schedule=barman_schedule(retention),
            # A new archive holds WAL and no base backup until the first one,
            # and WAL alone restores nothing.
            immediate=True,
            backup_owner_reference='self',
            method='plugin',
            plugin_configuration=crds.postgresql.v1.ScheduledBackupSpecPluginConfigurationArgs(name=PLUGIN),
            cluster=crds.postgresql.v1.ScheduledBackupSpecClusterArgs(name=name),
        ),
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=[cluster])),
    )
    reach = _operator_reach(resource_name, name, namespace=namespace, opts=opts)
    role, cronjob, drill_parameters = _drill(
        resource_name,
        name,
        job,
        drill,
        manifest,
        admitted,
        image=drill_image,
        dbname=dbname,
        server=server,
        opts=opts,
    )
    rules = operator_object(
        f'{resource_name}-restore-drill',
        kind='VMRule',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=job.cronjob, namespace=namespace),
        spec={'groups': [rule_group_spec(RuleGroup(job.cronjob, scheduled_job_rules(job)))]},
        opts=opts,
    )
    return Database(
        name=name,
        cluster=cluster,
        credential=credential,
        store=store,
        backup=backup,
        recovery=recovered,
        operator_reach=reach,
        drill=cronjob,
        drill_role=role,
        drill_parameters=drill_parameters,
        drill_rules=rules,
    )


@dataclass(frozen=True, kw_only=True)
class _Instance:
    """What a database's instance and the drill's recovered instance share, so the recovered one starts as it does."""

    image: str
    size: int
    resources: Resources
    node_affinity: Mapping[str, Any]
    labels: Mapping[str, str]
    shared_preload_libraries: tuple[str, ...]
    parameters: Mapping[str, str]


def _as_given(value: Mapping[str, Any]) -> Any:
    """Kubernetes' own JSON, which the provider sends as it is, in place of the SDK's type for it."""
    return dict(value)


def _postgresql(instance: _Instance) -> crds.postgresql.v1.ClusterSpecPostgresqlArgs | None:
    if not instance.shared_preload_libraries and not instance.parameters:
        return None
    return crds.postgresql.v1.ClusterSpecPostgresqlArgs(
        shared_preload_libraries=list(instance.shared_preload_libraries) or None,
        parameters=dict(instance.parameters) or None,
    )


def _bootstrap(
    dbname: str,
    *,
    recovery: Recovery | None,
    post_init_application_sql: Sequence[str],
    post_init_template_sql: Sequence[str],
) -> crds.postgresql.v1.ClusterSpecBootstrapArgs:
    """An empty database, or a recovery from the store a migrated one is handed."""
    if recovery is not None:
        return crds.postgresql.v1.ClusterSpecBootstrapArgs(
            recovery=crds.postgresql.v1.ClusterSpecBootstrapRecoveryArgs(
                source=_RECOVERY_SOURCE, database=dbname, owner=dbname
            )
        )
    return crds.postgresql.v1.ClusterSpecBootstrapArgs(
        initdb=crds.postgresql.v1.ClusterSpecBootstrapInitdbArgs(
            database=dbname,
            owner=dbname,
            post_init_application_sql=list(post_init_application_sql) or None,
            post_init_template_sql=list(post_init_template_sql) or None,
        )
    )


def _external(source: str, store: str, server: str) -> crds.postgresql.v1.ClusterSpecExternalClustersArgs:
    """A store a cluster recovers from, through the plugin, by its `ObjectStore` and the server name in it."""
    return crds.postgresql.v1.ClusterSpecExternalClustersArgs(
        name=source,
        plugin=crds.postgresql.v1.ClusterSpecExternalClustersPluginArgs(
            name=PLUGIN, parameters={'barmanObjectName': store, 'serverName': server}
        ),
    )


def _store_name(name: str) -> str:
    """The `ObjectStore` of a database's archive in the bucket, and the Secret it reads its credential from."""
    return f'{name}-backup'


def _credential(
    resource_name: str,
    name: str,
    *,
    namespace: str,
    writer_key: Mapping[str, pulumi.Input[str]],
    opts: pulumi.ResourceOptions | None,
) -> crds.bitnami.v1alpha1.SealedSecret:
    """The namespace's writer key, sealed namespace-wide, under the store's name, with the account's region beside it."""
    return sealed_secret(
        f'{resource_name}-credential',
        name=_store_name(name),
        namespace=namespace,
        encrypted_data=writer_key,
        template=SecretTemplate(data={_REGION_KEY: conventions.B2_ACCOUNT.region}),
        scope=SealingScope.NAMESPACE_WIDE,
        opts=opts,
    )


def _secret_ref(cls: Any, secret: str, key: str) -> Any:
    return cls(name=secret, key=key)


def _sidecar() -> crds.barmancloud.v1.ObjectStoreSpecInstanceSidecarConfigurationArgs:
    """The plugin's sidecar in every pod that reads or writes a store: held to `SIDECAR_RESOURCES`."""
    b = crds.barmancloud.v1
    return b.ObjectStoreSpecInstanceSidecarConfigurationArgs(
        resources=b.ObjectStoreSpecInstanceSidecarConfigurationResourcesArgs(
            requests=dict(SIDECAR_RESOURCES['requests']), limits=dict(SIDECAR_RESOURCES['limits'])
        )
    )


def _store(
    resource_name: str,
    name: str,
    retention: RetentionClass,
    *,
    namespace: str,
    opts: pulumi.ResourceOptions | None,
) -> crds.barmancloud.v1.ObjectStore:
    """The archive in the bucket: the namespace's path of the barman layout, kept by the class's window.

    The destination is the namespace's directory, and each server name is a
    directory under it, so a server's archive is `barman_repo_path` of the
    namespace and the server name. The server name is the plugin's parameter
    on the `Cluster`, not the store's: the store's field is kept for its
    API's compatibility and "must always be left empty" (the plugin's
    `web/docs/parameters.md`).
    """
    b = crds.barmancloud.v1
    secret = _store_name(name)
    namespace_path = conventions.barman_repo_path(namespace, '').rstrip('/')
    return b.ObjectStore(
        f'{resource_name}-store',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=secret, namespace=namespace),
        spec=b.ObjectStoreSpecArgs(
            configuration=b.ObjectStoreSpecConfigurationArgs(
                destination_path=f's3://{conventions.BUCKET_BACKUP}/{namespace_path}',
                endpoint_url=s3_endpoint(conventions.B2_ACCOUNT.region),
                s3_credentials=b.ObjectStoreSpecConfigurationS3CredentialsArgs(
                    access_key_id=_secret_ref(
                        b.ObjectStoreSpecConfigurationS3CredentialsAccessKeyIdArgs, secret, 'AWS_ACCESS_KEY_ID'
                    ),
                    secret_access_key=_secret_ref(
                        b.ObjectStoreSpecConfigurationS3CredentialsSecretAccessKeyArgs, secret, 'AWS_SECRET_ACCESS_KEY'
                    ),
                    region=_secret_ref(b.ObjectStoreSpecConfigurationS3CredentialsRegionArgs, secret, _REGION_KEY),
                ),
                wal=b.ObjectStoreSpecConfigurationWalArgs(compression='snappy'),
            ),
            retention_policy=recovery_window(retention),
            instance_sidecar_configuration=_sidecar(),
        ),
        opts=opts,
    )


def _check_recovery(resource_name: str, name: str, recovery: Recovery) -> None:
    """Refuse a store the credential cannot read."""
    if not recovery.destination_path.startswith('gs://'):
        raise DatabaseRefused(
            Refusal.STORE,
            f'{resource_name}: {name!r} recovers with a Google credential, which reads a `gs://` store, '
            f'not {recovery.destination_path!r}',
        )


def _recovery_store(
    resource_name: str,
    name: str,
    recovery: Recovery,
    *,
    namespace: str,
    opts: pulumi.ResourceOptions | None,
) -> tuple[crds.bitnami.v1alpha1.SealedSecret, crds.barmancloud.v1.ObjectStore]:
    """The legacy store, read once at the land, and its ported credential, which opens under any name in the namespace."""
    b = crds.barmancloud.v1
    store = f'{name}-recovery'
    credential = sealed_secret(
        f'{resource_name}-recovery',
        name=store,
        namespace=namespace,
        encrypted_data={recovery.key: recovery.ciphertext},
        opts=opts,
    )
    source = b.ObjectStore(
        f'{resource_name}-recovery',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=store, namespace=namespace),
        spec=b.ObjectStoreSpecArgs(
            configuration=b.ObjectStoreSpecConfigurationArgs(
                destination_path=recovery.destination_path,
                google_credentials=b.ObjectStoreSpecConfigurationGoogleCredentialsArgs(
                    application_credentials=_secret_ref(
                        b.ObjectStoreSpecConfigurationGoogleCredentialsApplicationCredentialsArgs, store, recovery.key
                    )
                ),
            ),
            instance_sidecar_configuration=_sidecar(),
        ),
        opts=opts,
    )
    return credential, source


def _operator_reach(
    resource_name: str, name: str, *, namespace: str, opts: pulumi.ResourceOptions | None
) -> crds.cilium.v2.CiliumNetworkPolicy:
    """The operator's namespace, admitted to the database's pods on the instance manager's port alone.

    The namespace's default-deny admits no other namespace, and the operator
    reads each instance's status there; its namespace runs the operator and
    its backup plugin, and nothing an application declares.
    """
    c = crds.cilium.v2
    return c.CiliumNetworkPolicy(
        f'{resource_name}-operator',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=f'{name}-operator', namespace=namespace),
        spec=c.CiliumNetworkPolicySpecArgs(
            description=f"Admit the database operator to the instances of {name!r} on the instance manager's port.",
            endpoint_selector=c.CiliumNetworkPolicySpecEndpointSelectorArgs(match_labels={DATABASE_LABEL: name}),
            ingress=[
                c.CiliumNetworkPolicySpecIngressArgs(
                    from_endpoints=[
                        c.CiliumNetworkPolicySpecIngressFromEndpointsArgs(
                            match_labels={'io.kubernetes.pod.namespace': conventions.CNPG_NAMESPACE}
                        )
                    ],
                    to_ports=[
                        c.CiliumNetworkPolicySpecIngressToPortsArgs(
                            ports=[
                                c.CiliumNetworkPolicySpecIngressToPortsPortsArgs(port=str(STATUS_PORT), protocol='TCP')
                            ]
                        )
                    ],
                )
            ],
        ),
        opts=opts,
    )


def _drill_name(name: str) -> str:
    """The throwaway cluster a database's drill recovers."""
    return f'{name}-drill'


def _drill_manifest(name: str, instance: _Instance, *, dbname: str, server: str, namespace: str) -> dict[str, Any]:
    """The drill's recovered cluster: the database's instance, recovering from its archive, archiving nowhere.

    It recovers from the store the database archives to, under the
    database's server name, and has no WAL archiver: one would archive into
    an archive of its own, and a recovered cluster archiving under the
    database's server name would write over the database's. It takes
    `local-path` whatever the database's class, since its claim is removed
    with it and that class reclaims `Delete`.
    """
    postgresql = {
        **(
            {'shared_preload_libraries': list(instance.shared_preload_libraries)}
            if instance.shared_preload_libraries
            else {}
        ),
        **({'parameters': dict(instance.parameters)} if instance.parameters else {}),
    }
    return {
        'apiVersion': 'postgresql.cnpg.io/v1',
        'kind': 'Cluster',
        'metadata': {'name': _drill_name(name), 'namespace': namespace},
        'spec': {
            'instances': 1,
            # No disruption budget, as the database has none: over one
            # instance it would hang a drain of the node for the whole run.
            'enablePDB': False,
            'imageName': instance.image,
            'storage': {'storageClass': conventions.SC_LOCAL_PATH, 'size': f'{instance.size}Gi'},
            'resources': {'requests': dict(instance.resources.requests), 'limits': dict(instance.resources.limits)},
            'affinity': {'nodeAffinity': dict(instance.node_affinity)},
            'inheritedMetadata': {'labels': dict(instance.labels)},
            **({'postgresql': postgresql} if postgresql else {}),
            'bootstrap': {'recovery': {'source': _DRILL_SOURCE, 'database': dbname, 'owner': dbname}},
            'externalClusters': [
                {
                    'name': _DRILL_SOURCE,
                    'plugin': {
                        'name': PLUGIN,
                        'parameters': {'barmanObjectName': _store_name(name), 'serverName': server},
                    },
                }
            ],
        },
    }


def _drill_rules(drill: str) -> list[k8s.rbac.v1.PolicyRuleArgs]:
    """What the drill's role allows in its namespace: its own cluster applied, waited for and removed, and `psql` in it.

    Every verb that changes or enters an object names the drill's own, the
    cluster and its one instance's pod, `<drill>-1` as the operator names
    it; `create` of a cluster cannot name one, since the object does not
    exist yet, so `DRILL_POLICY` holds the name, labels, annotations and
    `inheritedMetadata` of what it creates to the drill's manifest at
    admission (`drill_admission`). The cluster's claims go with it by the
    foreground cascade, as the operator's dependents, so the role holds no
    verb on claims.
    """

    def rule(
        group: str, kind: str, verbs: Sequence[str], names: Sequence[str] | None = None
    ) -> k8s.rbac.v1.PolicyRuleArgs:
        return k8s.rbac.v1.PolicyRuleArgs(
            api_groups=[group], resources=[kind], verbs=list(verbs), resource_names=list(names) if names else None
        )

    return [
        rule('postgresql.cnpg.io', 'clusters', ('get', 'list', 'watch')),
        rule('postgresql.cnpg.io', 'clusters', ('create',)),
        rule('postgresql.cnpg.io', 'clusters', ('patch', 'delete'), (drill,)),
        rule('', 'pods', ('get', 'list')),
        rule('', 'pods/exec', ('create',), (f'{drill}-1',)),
    ]


def _held(manifest: Mapping[str, Any], path: Sequence[str]) -> Mapping[str, str]:
    """The map at `path` in the drill's manifest, empty where the manifest sets none."""
    found: Any = manifest
    for key in path:
        found = found.get(key, {})
    return found


def _drill_parameters(resource_name: str, manifest: Mapping[str, Any], *, account: str) -> dict[str, str]:
    """What `DRILL_POLICY` holds `account` to, read off the drill's manifest.

    The account; the manifest's name; and each map `_HELD` names, as one
    `<key>=<value>` line per entry, since a `ConfigMap` holds strings and a
    label's key holds a `/`. A key holds no `=` and no newline, so a line's
    first `=` ends its key; an annotation's value may hold a newline, which
    a line cannot carry, so a held entry that does is refused here.
    """
    for key, path in _HELD.items():
        for entry, value in _held(manifest, path).items():
            if '\n' in value:
                raise DatabaseRefused(
                    Refusal.HELD_VALUE,
                    f"{resource_name}: the drill's {'.'.join(path)}, its parameter {key!r}, holds {entry!r} "
                    'with a newline in its value, which a parameter line cannot carry',
                )
    return {
        'account': account,
        'name': manifest['metadata']['name'],
        **{
            key: '\n'.join(sorted(f'{label}={value}' for label, value in _held(manifest, path).items()))
            for key, path in _HELD.items()
        },
    }


@dataclass(frozen=True)
class DrillAdmission:
    """What `drill_admission` emitted: the policy every database's drill is judged by, and its one binding."""

    policy: k8s.admissionregistration.v1.ValidatingAdmissionPolicy
    binding: k8s.admissionregistration.v1.ValidatingAdmissionPolicyBinding


def drill_admission(resource_name: str, *, opts: pulumi.ResourceOptions | None = None) -> DrillAdmission:
    """The policy that holds a drill's cluster's name, labels, annotations and `inheritedMetadata` to its manifest, and its binding.

    A drill's account creates `Cluster`s under any name, since `create`
    names no object, and patches its own. The policy judges every create
    and update of a `Cluster` by a service account named as a drill's
    (`_DRILL_SUFFIX`), against each `ConfigMap` labelled
    `DRILL_ADMISSION_LABEL` in the request's namespace: the parameters a
    database's shape derives from its drill's manifest. Under the
    parameters of the requesting account the cluster is admitted only by
    the manifest's name and with exactly its labels, annotations and
    `inheritedMetadata`, the maps `_HELD` names, beside the annotation
    `kubectl apply` writes (`_TOLERATED`); under another database's
    parameters the request is that database's no business. The binding
    ANDs every parameter it selects, so a namespace holding two databases
    holds each drill to its own manifest. The rest of the `Cluster`'s
    `spec` is not held (Aetf/kluster-ops#889).

    **It fails closed, on drills alone.** An expression that cannot be
    evaluated refuses the request (`failurePolicy: Fail`), and the drill
    fails, reported as any failed run is (operations.md §9.1). The match on
    the account's name keeps that to drills: every other writer of a
    `Cluster`, the operator and the program included, passes unjudged.

    **A namespace with no parameters is not judged.** The server looks the
    parameters up before it reads the match on the account, so parameters
    not found, were they refused, would refuse every writer of a `Cluster`
    there, the program's first create of a database among them, which
    nothing orders behind the parameters beside it. A drill runs only
    beside its parameters: the shape declares its CronJob behind them, and
    the drill's role holds no verb on a `ConfigMap`.
    """
    a = k8s.admissionregistration.v1
    requester = 'request.userInfo.username'

    def expected(key: str) -> str:
        return f"params.data.?{key}.orValue('').split('\\n').filter(entry, entry != '')"

    def actual(path: Sequence[str]) -> str:
        return f'object{"".join(f".?{key}" for key in path)}.orValue({{}})'

    def exact(key: str) -> str:
        """The map `key` holds the manifest's entries and no others, beside those `_TOLERATED` lets through."""
        held, wanted = f'variables.{key}', f'variables.{key}Expected'
        entry = f"k + '=' + {held}[k] in {wanted}"
        tolerated = _TOLERATED.get(key, ())
        if not tolerated:
            return f'!variables.drill || (size({held}) == size({wanted}) && {held}.all(k, {entry}))'
        listed = json.dumps(list(tolerated))
        return (
            f'!variables.drill || (size({held}.filter(k, !(k in {listed}))) == size({wanted})'
            f' && {held}.all(k, k in {listed} || {entry}))'
        )

    def held_message(key: str, path: Sequence[str]) -> str:
        beside = ''.join(f', beside {it}' for it in _TOLERATED.get(key, ()))
        return f"the restore drill's cluster carries its manifest's {'.'.join(path)}, exactly{beside}"

    variables = [
        a.VariableArgs(name='drill', expression=f'{requester} == params.data.account'),
        *(a.VariableArgs(name=key, expression=actual(path)) for key, path in _HELD.items()),
        *(a.VariableArgs(name=f'{key}Expected', expression=expected(key)) for key in _HELD),
    ]
    validations = [
        a.ValidationArgs(
            expression='!variables.drill || object.metadata.name == params.data.name',
            message_expression="'the restore drill creates ' + params.data.name + ' alone, not ' + object.metadata.name",
            reason='Forbidden',
        ),
        *(
            a.ValidationArgs(expression=exact(key), message=held_message(key, path), reason='Forbidden')
            for key, path in _HELD.items()
        ),
    ]
    policy = a.ValidatingAdmissionPolicy(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(name=DRILL_POLICY),
        spec=a.ValidatingAdmissionPolicySpecArgs(
            failure_policy='Fail',
            param_kind=a.ParamKindArgs(api_version='v1', kind='ConfigMap'),
            match_constraints=a.MatchResourcesArgs(
                resource_rules=[
                    a.NamedRuleWithOperationsArgs(
                        api_groups=['postgresql.cnpg.io'],
                        api_versions=['*'],
                        operations=['CREATE', 'UPDATE'],
                        resources=['clusters'],
                    )
                ]
            ),
            match_conditions=[
                a.MatchConditionArgs(
                    name='restore-drill-account',
                    expression=(
                        f"{requester}.startsWith('{_SERVICE_ACCOUNT_USER}') && {requester}.endsWith('{_DRILL_SUFFIX}')"
                    ),
                )
            ],
            variables=variables,
            validations=validations,
        ),
        opts=opts,
    )
    binding = a.ValidatingAdmissionPolicyBinding(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(name=DRILL_POLICY),
        spec=a.ValidatingAdmissionPolicyBindingSpecArgs(
            policy_name=DRILL_POLICY,
            validation_actions=['Deny'],
            # No namespace: the parameters are read in the request's own.
            # Parameters not found are let through, not refused: the API
            # server looks the parameters up before it evaluates the
            # policy's `matchConditions`, so `Deny` here refuses every
            # writer of a `Cluster` in a namespace with no parameters --
            # the program and the operator as well as a drill, and the
            # program's first create of a database, which nothing orders
            # behind its parameters, among them (measured on the
            # `kube-apiserver` the cluster runs, which
            # tests/test_apps_database.py runs). A drill runs only beside
            # its parameters.
            param_ref=a.ParamRefArgs(
                selector=k8s.meta.v1.LabelSelectorArgs(
                    match_expressions=[
                        k8s.meta.v1.LabelSelectorRequirementArgs(key=DRILL_ADMISSION_LABEL, operator='Exists')
                    ]
                ),
                parameter_not_found_action='Allow',
            ),
        ),
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=[policy])),
    )
    return DrillAdmission(policy=policy, binding=binding)


def _drill(
    resource_name: str,
    name: str,
    job: ScheduledJob,
    drill: Drill,
    manifest: Mapping[str, Any],
    admitted: Mapping[str, str],
    *,
    image: ImagePin,
    dbname: str,
    server: str,
    opts: pulumi.ResourceOptions | None,
) -> tuple[k8s.rbac.v1.Role, k8s.batch.v1.CronJob, k8s.core.v1.ConfigMap]:
    """The drill's CronJob, the ConfigMap holding its script and its cluster, the role it runs as, and its admission's parameters."""
    namespace = job.namespace
    resource = f'{resource_name}-restore-drill'
    metadata = k8s.meta.v1.ObjectMetaArgs(name=job.cronjob, namespace=namespace)
    files = k8s.core.v1.ConfigMap(
        resource,
        metadata=metadata,
        data={_DRILL_SCRIPT: _DRILL_PY, _DRILL_MANIFEST: json.dumps(manifest, indent=2, sort_keys=True)},
        opts=opts,
    )
    account = k8s.core.v1.ServiceAccount(resource, metadata=metadata, opts=opts)
    role = k8s.rbac.v1.Role(
        resource,
        metadata=metadata,
        rules=_drill_rules(_drill_name(name)),
        opts=opts,
    )
    binding = k8s.rbac.v1.RoleBinding(
        resource,
        metadata=metadata,
        role_ref=k8s.rbac.v1.RoleRefArgs(api_group='rbac.authorization.k8s.io', kind='Role', name=job.cronjob),
        subjects=[k8s.rbac.v1.SubjectArgs(kind='ServiceAccount', name=job.cronjob, namespace=namespace)],
        opts=opts,
    )
    parameters = k8s.core.v1.ConfigMap(
        f'{resource}-admission',
        metadata=k8s.meta.v1.ObjectMetaArgs(
            name=f'{job.cronjob}-admission', namespace=namespace, labels={DRILL_ADMISSION_LABEL: name}
        ),
        data=dict(admitted),
        opts=opts,
    )
    environment = {
        'NAMESPACE': namespace,
        'DRILL': _drill_name(name),
        'SERVER': server,
        'DATABASE': dbname,
        'TABLE': drill.table,
        'EXTENSIONS': ' '.join(drill.extensions),
        'MANIFEST': f'{_DRILL_DIR}/{_DRILL_MANIFEST}',
        'READY_TIMEOUT': str(int((job.deadline - _DRILL_CHECKS).total_seconds())),
        'HOME': _SCRATCH_DIR,
    }
    pod = k8s.core.v1.PodSpecArgs(
        service_account_name=job.cronjob,
        restart_policy='Never',
        security_context=k8s.core.v1.PodSecurityContextArgs(
            run_as_user=DRILL_USER,
            run_as_group=DRILL_USER,
            run_as_non_root=True,
            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
        ),
        containers=[
            k8s.core.v1.ContainerArgs(
                name='drill',
                image=str(image),
                command=[_PYTHON, f'{_DRILL_DIR}/{_DRILL_SCRIPT}'],
                env=[k8s.core.v1.EnvVarArgs(name=key, value=value) for key, value in environment.items()],
                resources=k8s.core.v1.ResourceRequirementsArgs(
                    requests=dict(_DRILL_RESOURCES['requests']), limits=dict(_DRILL_RESOURCES['limits'])
                ),
                security_context=k8s.core.v1.SecurityContextArgs(
                    allow_privilege_escalation=False,
                    read_only_root_filesystem=True,
                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                ),
                volume_mounts=[
                    k8s.core.v1.VolumeMountArgs(name='drill', mount_path=_DRILL_DIR, read_only=True),
                    k8s.core.v1.VolumeMountArgs(name='scratch', mount_path=_SCRATCH_DIR),
                ],
            )
        ],
        volumes=[
            k8s.core.v1.VolumeArgs(
                name='drill', config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=job.cronjob, default_mode=0o444)
            ),
            k8s.core.v1.VolumeArgs(name='scratch', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs(size_limit='64Mi')),
        ],
    )
    starting = job.starting_deadline
    cronjob = k8s.batch.v1.CronJob(
        resource,
        metadata=metadata,
        spec=k8s.batch.v1.CronJobSpecArgs(
            schedule=_DRILL_SCHEDULE,
            concurrency_policy='Forbid',
            starting_deadline_seconds=int(starting.total_seconds()) if starting is not None else None,
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=1,
            job_template=k8s.batch.v1.JobTemplateSpecArgs(
                spec=k8s.batch.v1.JobSpecArgs(
                    # A failed drill is reported, not retried behind the alert.
                    backoff_limit=0,
                    active_deadline_seconds=int(job.deadline.total_seconds()),
                    template=k8s.core.v1.PodTemplateSpecArgs(
                        metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(conventions.API_CLIENT_LABEL)),
                        spec=pod,
                    ),
                )
            ),
        ),
        opts=pulumi.ResourceOptions.merge(
            opts, pulumi.ResourceOptions(depends_on=[files, account, role, binding, parameters])
        ),
    )
    return role, cronjob, parameters
