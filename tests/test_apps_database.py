"""The application base's database, declared against mocks, its restore drill run under its image's `python3`, and the drill's admission judged by the cluster's API server.

Every database here is the test's own, each in a namespace of its own beside
an application namespace that declares the database's API clients, so what is
held is how a database's arguments decide what is emitted. The retention
class is the test's own too, with a schedule and counts no class of
`conventions` carries.

Literals written here are the upstream projects' contracts: CloudNativePG's
and the Barman Cloud plugin's fields, the definition's six-field schedule,
Cilium's policy fields, the operator's name for a cluster's first pod,
`kubectl`'s and `psql`'s command lines, which the fake `kubectl` answers, and
the API server's flags, a service account's user name and the words the
server refuses a request in.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import image_busybox
import pinned_image
import process_sessions
import pytest
import pytest_asyncio
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from mock_monitor import Recorder, declaring, run_with
from talos_images import default_images

from kluster import conventions
from kluster.components.apps.database import (
    API_CLIENTS,
    DATABASE_LABEL,
    DRILL_ADMISSION_LABEL,
    DRILL_POLICY,
    SIDECAR_RESOURCES,
    DatabaseRefused,
    Drill,
    Recovery,
    Refusal,
    Resources,
    database,
    drill_admission,
    drill_job,
    recovery_window,
)
from kluster.components.monitoring.operator_objects import type_token
from kluster.conventions.backup import RetentionClass, duration
from kluster.lib.k8s import application_namespace
from kluster.lib.versions import ImagePin

CLUSTER = 'crds:postgresql.cnpg.io/v1:Cluster'
SCHEDULED_BACKUP = 'crds:postgresql.cnpg.io/v1:ScheduledBackup'
OBJECT_STORE = 'crds:barmancloud.cnpg.io/v1:ObjectStore'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
POLICY = 'crds:cilium.io/v2:CiliumNetworkPolicy'
CRONJOB = 'kubernetes:batch/v1:CronJob'
CONFIGMAP = 'kubernetes:core/v1:ConfigMap'
ROLE = 'kubernetes:rbac.authorization.k8s.io/v1:Role'
ROLE_BINDING = 'kubernetes:rbac.authorization.k8s.io/v1:RoleBinding'
CLUSTER_ROLE = 'kubernetes:rbac.authorization.k8s.io/v1:ClusterRole'
CLUSTER_ROLE_BINDING = 'kubernetes:rbac.authorization.k8s.io/v1:ClusterRoleBinding'
VMRULE = type_token('VMRule')


#: A class of the test's own, unlike any of `conventions`.
WEEKDAY = RetentionClass(name='weekday', schedule='17 5 * * 2', period=dt.timedelta(weeks=1), daily=9, monthly=2)

OPERAND = ImagePin('registry.example.invalid/postgres', '17.9', f'sha256:{"a" * 64}')
#: The pin the drill's image is configured from, whose `python3` runs its script.
DRILL_PIN = 'database-restore-drill'
DRILL_IMAGE = image_busybox.pinned(DRILL_PIN)

RESOURCES = Resources(requests={'cpu': '100m', 'memory': '256Mi'}, limits={'cpu': '1', 'memory': '512Mi'})
#: A node volume's class, which a database may be claimed under.
NODE_VOLUME_CLASS = 'cloud-block-example'

AFFINITY = {
    'requiredDuringSchedulingIgnoredDuringExecution': {
        'nodeSelectorTerms': [{'matchExpressions': [{'key': 'example.com/site', 'operator': 'In', 'values': ['a']}]}]
    }
}

#: The legacy store a migrated database recovers from, as the land hands it.
LEGACY = Recovery(
    destination_path='gs://legacy-photos-backup',
    server_name='photos-db-1a2b3c',
    key='gcs_credentials',
    ciphertext='AgB-ported-photos',
)


def writer_key(namespace: str) -> dict[str, str]:
    """A namespace's sealed writer key, its ciphertext naming the namespace."""
    return {'AWS_ACCESS_KEY_ID': f'{namespace}-id', 'AWS_SECRET_ACCESS_KEY': f'{namespace}-secret'}


def declare(name: str, namespace: str, cluster: str | None = None, **overrides: Any) -> None:
    """A database of the test's own, named `cluster` or after its namespace, beside the namespace, which declares its API clients."""
    _ = application_namespace(f'{name}-namespace', name=namespace, api_clients=API_CLIENTS)
    arguments: dict[str, Any] = {
        'namespace': namespace,
        'image': OPERAND,
        'dbname': 'app',
        'resources': RESOURCES,
        'node_affinity': AFFINITY,
        'writer_key': writer_key(namespace),
        'drill': Drill(table='Expense', extensions=('pg_cron', 'cube')),
        'drill_image': DRILL_IMAGE,
        **overrides,
    }
    _ = database(name, cluster or namespace, 3, WEEKDAY, **arguments)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """An empty database, a migrated one, one on a node volume, one at two generations, two in one namespace, and the drills' policy."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        declare(
            'ledger',
            'ledger',
            shared_preload_libraries=('pg_cron',),
            parameters={'cron.database_name': 'app'},
            post_init_application_sql=('CREATE EXTENSION pg_cron;',),
            post_init_template_sql=('CREATE EXTENSION cube;',),
        )
        declare('photos', 'photos', recovery=LEGACY)
        declare('volume', 'volume', storage_class=NODE_VOLUME_CLASS)
        declare('retry-g1', 'retry', generation=1)
        declare('retry-g2', 'retry', generation=2)
        declare('pair-a', 'pair', 'alpha')
        declare('pair-b', 'pair', 'beta')
        _ = drill_admission('drill-admission')
    return monitor


def of(monitor: Recorder, typ: str, namespace: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == namespace]


def one(monitor: Recorder, typ: str, namespace: str) -> dict[str, Any]:
    (found,) = of(monitor, typ, namespace)
    return found


def named(monitor: Recorder, typ: str, namespace: str, name: str) -> dict[str, Any]:
    (found,) = (it for it in of(monitor, typ, namespace) if it['metadata']['name'] == name)
    return found


def archiver(cluster: dict[str, Any]) -> dict[str, str]:
    """The parameters of the cluster's one WAL archiver."""
    (plugin,) = (it for it in cluster['spec'].get('plugins', []) if it.get('isWALArchiver'))
    return plugin['parameters']


def archive_path(monitor: Recorder, namespace: str, parameters: Mapping[str, str]) -> str:
    """Where a plugin's parameters point: its store's destination, and the server name under it."""
    store = named(monitor, OBJECT_STORE, namespace, parameters['barmanObjectName'])
    return f'{store["spec"]["configuration"]["destinationPath"]}/{parameters["serverName"]}'


def drill_manifest(monitor: Recorder, namespace: str, database: str | None = None) -> dict[str, Any]:
    """The cluster the drill of the namespace's one database recovers, or of `database` where the namespace holds two."""
    cronjob = drill_cronjob(monitor, namespace, database)
    files = named(monitor, CONFIGMAP, namespace, cronjob['metadata']['name'])
    container = drill_container(cronjob)
    manifest = environment(container)['MANIFEST']
    (mount,) = (it for it in container['volumeMounts'] if manifest.startswith(f'{it["mountPath"]}/'))
    return json.loads(files['data'][manifest.removeprefix(f'{mount["mountPath"]}/')])


def drill_cronjob(monitor: Recorder, namespace: str, database: str | None = None) -> dict[str, Any]:
    if database is None:
        return one(monitor, CRONJOB, namespace)
    return named(monitor, CRONJOB, namespace, drill_job(namespace, database).cronjob)


def drill_pod(cronjob: dict[str, Any]) -> dict[str, Any]:
    return cronjob['spec']['jobTemplate']['spec']['template']


def drill_container(cronjob: dict[str, Any]) -> dict[str, Any]:
    (container,) = drill_pod(cronjob)['spec']['containers']
    return container


def environment(container: dict[str, Any]) -> dict[str, str]:
    return {it['name']: it['value'] for it in container['env']}


def selects(selector: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    """Whether a label selector selects a pod carrying `labels`."""
    if any(labels.get(key) != value for key, value in selector.get('matchLabels', {}).items()):
        return False
    for expression in selector.get('matchExpressions', []):
        key, values = expression['key'], expression.get('values', [])
        held = {
            'In': labels.get(key) in values,
            'NotIn': labels.get(key) not in values,
            'Exists': key in labels,
            'DoesNotExist': key not in labels,
        }[expression['operator']]
        if not held:
            return False
    return True


# -- The archive -----------------------------------------------------------------------


def test_the_archive_is_the_namespaces_barman_path_of_the_server_name_and_each_generation_has_its_own(
    declared: Recorder,
) -> None:
    """barman refuses to archive a new cluster where an archive is, so a land retried after a revert archives apart."""
    paths: list[str] = []
    for generation in ('retry-g1', 'retry-g2'):
        cluster = declared.inputs_of(generation, CLUSTER)
        parameters = archiver(cluster)
        store = declared.inputs_of(f'{generation}-store', OBJECT_STORE)
        destination = store['spec']['configuration']['destinationPath']

        assert cluster['metadata']['name'] == 'retry'
        assert store['metadata']['name'] == parameters['barmanObjectName']
        assert f'{destination}/{parameters["serverName"]}' == (
            f's3://{conventions.BUCKET_BACKUP}/{conventions.barman_repo_path("retry", parameters["serverName"])}'
        )
        paths.append(parameters['serverName'])

    assert len(set(paths)) == len(paths)


def test_the_archive_is_written_with_the_namespaces_writer_key_at_the_buckets_endpoint(declared: Recorder) -> None:
    """The store reads both halves of the writer key, and the region, from the one Secret the key is sealed into."""
    store = one(declared, OBJECT_STORE, 'ledger')
    configuration = store['spec']['configuration']
    credentials = configuration['s3Credentials']
    secret = named(declared, SEALED_SECRET, 'ledger', credentials['accessKeyId']['name'])

    assert configuration['endpointURL'].endswith('.backblazeb2.com')
    assert {credentials[it]['name'] for it in ('accessKeyId', 'secretAccessKey', 'region')} == {
        secret['metadata']['name']
    }
    assert secret['spec']['encryptedData'] == writer_key('ledger')
    assert {credentials['accessKeyId']['key'], credentials['secretAccessKey']['key']} == set(writer_key('ledger'))
    assert secret['spec']['template']['data'][credentials['region']['key']] == conventions.B2_ACCOUNT.region
    assert secret['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}


def test_base_backups_are_scheduled_and_kept_by_the_retention_class(declared: Recorder) -> None:
    """The class's cron line with the definition's seconds field ahead of it, and barman's window as deep as the class."""
    backup = one(declared, SCHEDULED_BACKUP, 'ledger')
    cluster = one(declared, CLUSTER, 'ledger')
    store = one(declared, OBJECT_STORE, 'ledger')
    seconds, *fields = backup['spec']['schedule'].split()

    assert seconds == '0'
    assert ' '.join(fields) == WEEKDAY.schedule
    assert backup['metadata']['labels'][conventions.RETENTION_LABEL] == WEEKDAY.name
    assert backup['spec']['cluster']['name'] == cluster['metadata']['name']
    assert backup['spec']['method'] == 'plugin'
    assert backup['spec']['immediate'] is True
    assert store['spec']['retentionPolicy'] == recovery_window(WEEKDAY)


@pytest.mark.parametrize(
    ('retention', 'window'),
    [
        (RetentionClass(name='a', schedule='0 3 * * *', period=dt.timedelta(days=1), daily=30, monthly=12), '12m'),
        (RetentionClass(name='b', schedule='0 4 * * 0', period=dt.timedelta(weeks=1), weekly=4), '4w'),
        (RetentionClass(name='c', schedule='0 3 * * *', period=dt.timedelta(days=1), daily=30), '30d'),
    ],
)
def test_the_window_reaches_as_far_back_as_the_deepest_tier(retention: RetentionClass, window: str) -> None:
    """barman thins nothing inside its window, so the window is the class's reach, in its deepest tier's unit."""
    assert recovery_window(retention) == window


def test_a_class_that_keeps_nothing_has_no_window() -> None:
    with pytest.raises(ValueError, match='keeps nothing'):
        _ = recovery_window(RetentionClass(name='none', schedule='0 3 * * *', period=dt.timedelta(days=1)))


# -- The bootstrap ----------------------------------------------------------------------


def test_a_migrated_database_recovers_from_the_store_it_is_handed_and_archives_to_the_bucket(
    declared: Recorder,
) -> None:
    """The legacy store is read once, through its ported credential, and nothing is written back to it."""
    cluster = one(declared, CLUSTER, 'photos')
    recovery = cluster['spec']['bootstrap']['recovery']
    (source,) = (it for it in cluster['spec']['externalClusters'] if it['name'] == recovery['source'])
    legacy = named(declared, OBJECT_STORE, 'photos', source['plugin']['parameters']['barmanObjectName'])
    credential = legacy['spec']['configuration']['googleCredentials']['applicationCredentials']
    sealed = named(declared, SEALED_SECRET, 'photos', credential['name'])
    written = archive_path(declared, 'photos', archiver(cluster))

    assert 'initdb' not in cluster['spec']['bootstrap']
    assert legacy['spec']['configuration']['destinationPath'] == LEGACY.destination_path
    assert source['plugin']['parameters']['serverName'] == LEGACY.server_name
    assert sealed['spec']['encryptedData'] == {LEGACY.key: LEGACY.ciphertext}
    assert credential['key'] == LEGACY.key
    assert recovery['database'] == recovery['owner'] == 'app'
    assert written.startswith(f's3://{conventions.BUCKET_BACKUP}/{conventions.barman_repo_path("photos", "")}')
    assert not written.startswith(LEGACY.destination_path)


def test_a_database_that_migrates_nothing_is_initialized_with_its_sql(declared: Recorder) -> None:
    cluster = one(declared, CLUSTER, 'ledger')
    initdb = cluster['spec']['bootstrap']['initdb']

    assert 'externalClusters' not in cluster['spec']
    assert initdb['database'] == initdb['owner'] == 'app'
    assert initdb['postInitApplicationSQL'] == ['CREATE EXTENSION pg_cron;']
    assert initdb['postInitTemplateSQL'] == ['CREATE EXTENSION cube;']
    assert of(declared, OBJECT_STORE, 'ledger') == [one(declared, OBJECT_STORE, 'ledger')]


def test_a_database_runs_one_instance_on_its_site_with_its_resources(declared: Recorder) -> None:
    spec = one(declared, CLUSTER, 'ledger')['spec']

    assert declared.options_of('ledger', CLUSTER).protect is True
    assert spec['instances'] == 1
    assert spec['affinity']['nodeAffinity'] == AFFINITY
    assert spec['resources'] == {'requests': RESOURCES.requests, 'limits': RESOURCES.limits}
    assert spec['imageName'] == str(OPERAND)
    assert spec['storage'] == {'storageClass': conventions.SC_LOCAL_PATH, 'size': '3Gi'}
    assert spec['postgresql'] == {'shared_preload_libraries': ['pg_cron'], 'parameters': {'cron.database_name': 'app'}}


def test_a_database_on_a_node_volume_is_claimed_under_its_class_and_drilled_on_local_path(declared: Recorder) -> None:
    """The drill's claim is removed with it, which `local-path` reclaims and a node volume's class keeps."""
    assert one(declared, CLUSTER, 'volume')['spec']['storage']['storageClass'] == NODE_VOLUME_CLASS
    assert drill_manifest(declared, 'volume')['spec']['storage']['storageClass'] == conventions.SC_LOCAL_PATH


@pytest.mark.parametrize('namespace', ['ledger', 'photos'])
def test_a_database_and_its_drill_hold_one_instance_with_no_disruption_budget(
    declared: Recorder, namespace: str
) -> None:
    """A budget over one instance refuses every eviction of it, so a drain of its node would hang (operations.md §3)."""
    for spec in (one(declared, CLUSTER, namespace)['spec'], drill_manifest(declared, namespace)['spec']):
        assert spec['instances'] == 1
        assert spec['enablePDB'] is False


@pytest.mark.parametrize('namespace', ['ledger', 'photos'])
def test_every_store_holds_the_plugins_sidecar_to_a_cpu_limit(declared: Recorder, namespace: str) -> None:
    """The sidecar runs in every instance and every recovery, on the cloud pool beside etcd too."""
    stores = of(declared, OBJECT_STORE, namespace)

    assert stores
    for store in stores:
        resources = store['spec']['instanceSidecarConfiguration']['resources']
        assert resources['limits']['cpu'], store['metadata']['name']
        assert resources == {kind: dict(values) for kind, values in SIDECAR_RESOURCES.items()}


# -- The pods and the API ---------------------------------------------------------------


def _api_denied(monitor: Recorder, namespace: str, labels: Mapping[str, str]) -> bool:
    """Whether the namespace's egress block denies a pod carrying `labels` the API's port on a node."""
    port = str(conventions.MANAGEMENT_PORTS.kubernetes)
    block = named(monitor, POLICY, namespace, 'egress-block')
    for spec in block['specs']:
        for deny in spec.get('egressDeny', []):
            ports = [it['port'] for rule in deny.get('toPorts', []) for it in rule['ports']]
            if deny.get('toEntities') and ports == [port] and selects(spec['endpointSelector'], labels):
                return True
    return False


@pytest.mark.parametrize('namespace', ['ledger', 'photos'])
def test_the_instances_and_the_drills_pods_reach_the_api_in_a_namespace_declaring_its_clients(
    declared: Recorder, namespace: str
) -> None:
    """Each pod that calls the API carries the label the namespace's declaration lets through, and only that opens it."""
    instances = one(declared, CLUSTER, namespace)['spec']['inheritedMetadata']['labels']
    recovered = drill_manifest(declared, namespace)['spec']['inheritedMetadata']['labels']
    drill = drill_pod(one(declared, CRONJOB, namespace))['metadata']['labels']

    assert _api_denied(declared, namespace, {})
    for labels in (instances, recovered, drill):
        assert not _api_denied(declared, namespace, labels), labels


@pytest.mark.parametrize('namespace', ['ledger', 'photos'])
def test_the_operator_reaches_every_instance_on_the_status_port_alone(declared: Recorder, namespace: str) -> None:
    """The namespace's default-deny admits no other namespace, and the operator reads each instance's status."""
    instances = one(declared, CLUSTER, namespace)['spec']['inheritedMetadata']['labels']
    recovered = drill_manifest(declared, namespace)['spec']['inheritedMetadata']['labels']
    drill = drill_pod(one(declared, CRONJOB, namespace))['metadata']['labels']
    (reach,) = (
        it
        for it in of(declared, POLICY, namespace)
        if 'ingress' in it.get('spec', {}) and it['metadata']['name'] != 'default-deny'
    )
    spec = reach['spec']
    (rule,) = spec['ingress']
    (peer,) = rule['fromEndpoints']

    assert selects(spec['endpointSelector'], instances)
    assert selects(spec['endpointSelector'], recovered)
    assert not selects(spec['endpointSelector'], drill)
    assert peer == {'matchLabels': {'io.kubernetes.pod.namespace': conventions.CNPG_NAMESPACE}}
    assert rule['toPorts'] == [{'ports': [{'port': '8000', 'protocol': 'TCP'}]}]


# -- The drill ---------------------------------------------------------------------------


@pytest.mark.parametrize('namespace', ['ledger', 'photos'])
def test_the_drill_recovers_the_databases_instance_from_its_archive_and_archives_nowhere(
    declared: Recorder, namespace: str
) -> None:
    """A recovered cluster archiving under the database's server name would write over the database's archive."""
    cluster = one(declared, CLUSTER, namespace)['spec']
    manifest = drill_manifest(declared, namespace)
    recovered = manifest['spec']
    (source,) = (
        it for it in recovered['externalClusters'] if it['name'] == recovered['bootstrap']['recovery']['source']
    )

    assert manifest['metadata']['namespace'] == namespace
    assert recovered['bootstrap']['recovery']['database'] == recovered['bootstrap']['recovery']['owner'] == 'app'
    assert manifest['metadata']['name'] != one(declared, CLUSTER, namespace)['metadata']['name']
    assert 'plugins' not in recovered and 'backup' not in recovered
    assert source['plugin'] == {
        'name': archiver_plugin(cluster)['name'],
        'parameters': archiver(one(declared, CLUSTER, namespace)),
    }
    for field in ('imageName', 'affinity', 'resources', 'postgresql'):
        assert recovered.get(field) == cluster.get(field), field
    assert recovered['storage'] == {'storageClass': conventions.SC_LOCAL_PATH, 'size': cluster['storage']['size']}


def archiver_plugin(spec: dict[str, Any]) -> dict[str, Any]:
    (plugin,) = (it for it in spec['plugins'] if it.get('isWALArchiver'))
    return plugin


#: The verbs that change an object or run in it, which the drill's role holds on its own objects alone.
MUTATING = frozenset({'patch', 'update', 'delete', 'deletecollection'})


def test_the_drills_role_reaches_its_own_cluster_and_pod_in_its_namespace_alone(declared: Recorder) -> None:
    """A Role in the database's namespace, bound to the drill's account, whose every change or exec names the drill's.

    A poisoned drill image runs with this token, so a verb that changes or
    enters an object reaches the throwaway cluster and its one pod, which
    the operator names `<cluster>-1`, and nothing else in the namespace;
    `create` names nothing, since the object does not exist yet.
    """
    cronjob = one(declared, CRONJOB, 'ledger')
    account = drill_pod(cronjob)['spec']['serviceAccountName']
    role = one(declared, ROLE, 'ledger')
    binding = one(declared, ROLE_BINDING, 'ledger')
    drill = drill_manifest(declared, 'ledger')['metadata']['name']
    own = {('postgresql.cnpg.io', 'clusters'): [drill], ('', 'pods/exec'): [f'{drill}-1']}

    assert declared.of_type(CLUSTER_ROLE) == []
    assert declared.of_type(CLUSTER_ROLE_BINDING) == []
    assert binding['roleRef'] == {
        'apiGroup': 'rbac.authorization.k8s.io',
        'kind': 'Role',
        'name': role['metadata']['name'],
    }
    assert binding['subjects'] == [{'kind': 'ServiceAccount', 'name': account, 'namespace': 'ledger'}]
    for rule in role['rules']:
        (group,), (kind,) = rule['apiGroups'], rule['resources']
        verbs = set(rule['verbs'])
        assert kind in {'clusters', 'pods', 'pods/exec'}, kind
        if verbs & MUTATING or kind == 'pods/exec':
            assert rule.get('resourceNames') == own[group, kind], rule
    assert any(rule['resources'] == ['pods/exec'] for rule in role['rules'])
    assert any('delete' in rule['verbs'] for rule in role['rules'])


def _threshold(expr: str) -> dt.timedelta:
    """The age a stale alert fires past, in the duration syntax its expression ends in."""
    found = re.search(r'> (\d+)([wdhms])$', expr)
    assert found is not None, expr
    unit = {'w': 'weeks', 'd': 'days', 'h': 'hours', 'm': 'minutes', 's': 'seconds'}[found[2]]
    return dt.timedelta(**{unit: int(found[1])})


@pytest.mark.parametrize('namespace', ['ledger', 'photos'])
def test_the_drills_rules_watch_the_cronjob_declared_beside_them(declared: Recorder, namespace: str) -> None:
    """A drill that stops running or fails pages, held to the CronJob declared beside it.

    Each rule's tier, summary and playbook are held by the monitoring suite's
    gate over every rule the programs declare, this one among them
    (`tests/test_monitoring.py`, a database wired beside `apps`).
    """
    cronjob = one(declared, CRONJOB, namespace)
    spec = cronjob['spec']['jobTemplate']['spec']
    (group,) = one(declared, VMRULE, namespace)['spec']['groups']
    selector = f'{{namespace="{namespace}", cronjob="{cronjob["metadata"]["name"]}"}}'
    rules = {rule['alert']: rule for rule in group['rules']}
    (stale,) = (rule for name, rule in rules.items() if name.endswith('Stale'))
    (failed,) = (rule for name, rule in rules.items() if name.endswith('Failed'))

    assert len(rules) == 2
    assert failed['for'] == duration(dt.timedelta(seconds=spec['activeDeadlineSeconds']))
    # A run on one day of every month succeeds inside the threshold whatever
    # the month's length: 31 days is the longest gap between two runs of a
    # cron line that names a day every month has.
    assert _threshold(stale['expr']) > dt.timedelta(days=31) + dt.timedelta(seconds=spec['activeDeadlineSeconds'])
    for rule in (stale, failed):
        assert selector in rule['expr'], rule['expr']


@pytest.mark.parametrize('namespace', ['ledger', 'photos'])
def test_the_drill_runs_as_its_job_of_the_scheduled_job_family(declared: Recorder, namespace: str) -> None:
    """The job the family's rules read is the CronJob declared: its name, its deadline, its starting deadline."""
    row = drill_job(namespace, one(declared, CLUSTER, namespace)['metadata']['name'])
    cronjob = one(declared, CRONJOB, namespace)
    spec = cronjob['spec']
    minute, hour, day, month, weekday = spec['schedule'].split()
    ready = int(environment(drill_container(cronjob))['READY_TIMEOUT'])
    assert row.starting_deadline is not None

    assert cronjob['metadata']['name'] == row.cronjob
    assert spec['jobTemplate']['spec']['activeDeadlineSeconds'] == row.deadline.total_seconds()
    assert spec['startingDeadlineSeconds'] == row.starting_deadline.total_seconds()
    assert (month, weekday) == ('*', '*')
    assert all(field.isdigit() for field in (minute, hour, day))
    # A day every month has, so the drill runs every month: one past the 28th
    # skips the months without it, a gap past the stale threshold.
    assert 1 <= int(day) <= 28, day
    assert spec['concurrencyPolicy'] == 'Forbid'
    assert spec['jobTemplate']['spec']['backoffLimit'] == 0
    assert 0 < ready < row.deadline.total_seconds()


def test_the_drills_pod_runs_restricted_with_its_resources_bounded(declared: Recorder) -> None:
    pod = drill_pod(one(declared, CRONJOB, 'ledger'))['spec']
    container = drill_container(one(declared, CRONJOB, 'ledger'))

    assert pod['securityContext']['runAsNonRoot'] is True
    assert pod['securityContext']['runAsUser'] != 0
    assert pod['securityContext']['seccompProfile'] == {'type': 'RuntimeDefault'}
    assert container['securityContext']['allowPrivilegeEscalation'] is False
    assert container['securityContext']['capabilities'] == {'drop': ['ALL']}
    assert set(container['resources']['limits']) == {'cpu', 'memory'}


# -- Refusals --------------------------------------------------------------------------


def _refused(size: int = 1, name: str = 'refused', **overrides: Any) -> None:
    arguments: dict[str, Any] = {
        'namespace': 'refused',
        'image': OPERAND,
        'dbname': 'app',
        'resources': RESOURCES,
        'node_affinity': AFFINITY,
        'writer_key': writer_key('refused'),
        'drill': Drill(table='asset'),
        'drill_image': DRILL_IMAGE,
        **overrides,
    }
    _ = database('refused', name, size, WEEKDAY, **arguments)


@pytest.mark.parametrize(
    ('overrides', 'refusal'),
    [
        ({'size': 0}, Refusal.SIZE),
        ({'generation': -1}, Refusal.GENERATION),
        ({'writer_key': {'AWS_ACCESS_KEY_ID': 'only-half'}}, Refusal.WRITER_KEY),
        ({'drill': Drill(table='asset; drop table asset')}, Refusal.IDENTIFIER),
        ({'drill': Drill(table='asset', extensions=("vector'",))}, Refusal.IDENTIFIER),
        (
            {'recovery': Recovery(destination_path='s3://elsewhere', server_name='a', key='k', ciphertext='v')},
            Refusal.STORE,
        ),
        # The database label carries the database's name onto every pod, the
        # drill's included.
        ({'name': 'two\nlines'}, Refusal.HELD_VALUE),
    ],
    ids=[
        'no size',
        'a negative generation',
        'half a writer key',
        'a table that is no name',
        'an extension that is no name',
        'an s3 store',
        'a held label value with a newline',
    ],
)
def test_what_a_database_cannot_be_is_refused_before_anything_is_declared(
    overrides: dict[str, Any], refusal: Refusal
) -> None:
    with pytest.raises(DatabaseRefused) as refused:
        _refused(**overrides)

    assert refused.value.refusal is refusal


# -- The drill, run ---------------------------------------------------------------------

#: The fake `kubectl`, run by the image's own `python3`: it logs each command
#: line, as JSON after `CALL`, on the container's stderr, and answers what the drill asks as `$FAKE_*`
#: says the recovered cluster is. `FAKE_EXEC_FAIL` names the queries that fail
#: as `psql` does: `all` of them, as a database that does not exist, or the
#: `table`'s, as a relation that does not. `FAKE_WAIT_HANG` holds the wait for
#: the cluster until the process is killed.
FAKE_KUBECTL = r"""#!/usr/bin/python3
import json, os, sys, time
args = sys.argv[1:]
with open('/proc/1/fd/2', 'a') as log:
    log.write('kubectl-call: ' + json.dumps(args) + '\n')
verb = args[0]
if verb == 'wait':
    if os.environ.get('FAKE_WAIT_HANG') == '1':
        time.sleep(3600)
    sys.exit(0 if os.environ['FAKE_READY'] == '1' else 1)
if verb == 'get' and args[1] == 'pods':
    print(os.environ['FAKE_POD'])
elif verb == 'exec':
    query = args[-1]
    failing = os.environ.get('FAKE_EXEC_FAIL', '')
    if failing == 'all':
        sys.exit('psql: error: FATAL:  database "app" does not exist')
    if query == 'select 1':
        print(1)
    elif query == 'select pg_is_in_recovery()':
        print(os.environ['FAKE_IN_RECOVERY'])
    elif query == 'select extname from pg_extension':
        print('\n'.join(os.environ['FAKE_EXTENSIONS'].split()))
    elif query.startswith('select count(*) from '):
        if failing == 'table':
            sys.exit('ERROR:  relation "Expense" does not exist')
        print(os.environ['FAKE_ROWS'])
    else:
        sys.exit(f'the fake answers no query {query!r}')
"""

#: What the recovered cluster is unless a case says otherwise: ready, with a
#: primary, out of recovery, with rows and every extension the drill checks.
RECOVERED = {
    'FAKE_READY': '1',
    'FAKE_POD': 'pod/ledger-drill-1',
    'FAKE_IN_RECOVERY': 'f',
    'FAKE_ROWS': '42',
    'FAKE_EXTENSIONS': 'plpgsql pg_cron cube',
}

#: The label a container started here carries, the census of what a run left.
LABEL = f'kluster-test={Path(__file__).stem}'
#: Stop-losses, which nothing asserts on: one run of the drill against the
#: fake, a container's own bound, and the wait for the drill's first wait.
RUN_TIMEOUT = 60
CONTAINER_TIMEOUT = 300
COMMAND_TIMEOUT = 30


@pytest.fixture(scope='module', name='drill_image')
def drill_image_fixture() -> str:
    """The drill's image, pulled by its digest, by a first container of it."""
    _ = image_busybox.in_image(DRILL_IMAGE, 'true')
    return image_busybox.source(DRILL_IMAGE)


@dataclass(frozen=True)
class Drilled:
    """A run of the declared drill, as the image's `python3` runs it, against the fake."""

    status: int
    stdout: str
    stderr: str
    calls: list[list[str]]


#: What the fake writes ahead of each command line it logs, on the container's
#: own stderr, which is the one place both the drill's lines and its own reach
#: the case: the drill captures every `kubectl`'s output.
CALL = 'kubectl-call: '


class DrillContainer:
    """The declared CronJob's container as a container of the drill's image: its command, environment and files.

    The ConfigMap's files and the fake are written by a shell ahead of the
    drill into memory-backed mounts at the declared paths, from the
    container's environment, and the shell then hands its process to the
    drill's command, which runs as PID 1 as it does in the pod. Nothing is
    mounted from the host, whose temporary directory a rootless podman need
    not see.
    """

    def __init__(self, declared: Recorder, image: str, **recovered: str) -> None:
        cronjob = one(declared, CRONJOB, 'ledger')
        container = drill_container(cronjob)
        user = drill_pod(cronjob)['spec']['securityContext']
        files = named(declared, CONFIGMAP, 'ledger', cronjob['metadata']['name'])['data']
        mounts = {it['name']: it['mountPath'] for it in container['volumeMounts']}
        written = {f'{mounts["drill"]}/{name}': text for name, text in files.items()} | {'/fake/kubectl': FAKE_KUBECTL}
        self.image = image
        self.command = list(container['command'])
        self.name = f'kluster-test-drill-{uuid.uuid4().hex[:12]}'
        environment_ = {
            **environment(container),
            'PATH': '/fake:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            **{f'WRITTEN_{at}': text for at, text in enumerate(written.values())},
            **RECOVERED,
            **recovered,
        }
        setup = ' && '.join(f'printf %s "$WRITTEN_{at}" > {path}' for at, path in enumerate(written))
        self.argv = [
            'podman', 'run', '--name', self.name, '--label', LABEL, '--timeout', str(CONTAINER_TIMEOUT),
            '--pull', 'never', '--network', 'none', '--read-only',
            '--user', f'{int(user["runAsUser"])}:{int(user["runAsGroup"])}',
            *(arg for path in {mounts['scratch'], mounts['drill'], '/fake'} for arg in ('--tmpfs', f'{path}:rw,exec,mode=1777')),
            *(arg for key, value in environment_.items() for arg in ('--env', f'{key}={value}')),
            '--entrypoint', '/bin/sh',
        ]  # fmt: skip
        self.arguments = [self.image, '-c', f'{setup} && chmod 755 /fake/kubectl && exec "$@"', 'sh', *self.command]

    @staticmethod
    def drilled(status: int, stdout: str, stderr: str) -> Drilled:
        lines = stderr.splitlines()
        calls = [json.loads(line.removeprefix(CALL)) for line in lines if line.startswith(CALL)]
        return Drilled(status, stdout, '\n'.join(line for line in lines if not line.startswith(CALL)), calls)

    def run(self) -> Drilled:
        with process_sessions.removing(self.name, timeout=COMMAND_TIMEOUT):
            done = process_sessions.run([*self.argv, *self.arguments], text=True, timeout=RUN_TIMEOUT)
        return self.drilled(done.returncode, done.stdout, done.stderr)

    def _logs(self) -> tuple[str, str]:
        logs = process_sessions.run(['podman', 'logs', self.name], text=True, timeout=COMMAND_TIMEOUT, check=True)
        return logs.stdout, logs.stderr

    def run_stopped(self) -> Drilled:
        """A run whose wait hangs, sent TERM, as the kubelet does at the Job's deadline, once the wait has started."""
        with process_sessions.removing(self.name, timeout=COMMAND_TIMEOUT):
            started = process_sessions.run(
                [*self.argv[:2], '--detach', *self.argv[2:], *self.arguments], text=True, timeout=RUN_TIMEOUT
            )
            assert started.returncode == 0, started.stderr
            bound = time.monotonic() + RUN_TIMEOUT
            while not any(call[0] == 'wait' for call in self.drilled(0, *self._logs()).calls):
                assert time.monotonic() < bound, 'the drill never started waiting for its cluster'
                time.sleep(0.2)
            _ = process_sessions.run(
                ['podman', 'kill', '--signal', 'TERM', self.name], timeout=COMMAND_TIMEOUT, check=True
            )
            waited = process_sessions.run(['podman', 'wait', self.name], text=True, timeout=RUN_TIMEOUT, check=True)
            return self.drilled(int(waited.stdout.strip()), *self._logs())


def _removed_last(drilled: Drilled) -> None:
    """The run ended by removing the recovered cluster, its claims before it, whatever happened before."""
    *_, removal = drilled.calls
    assert removal[:3] == ['delete', 'cluster', 'ledger-drill']
    assert '--cascade=foreground' in removal


def _queried(drilled: Drilled) -> list[str]:
    return [call[-1] for call in drilled.calls if call[0] == 'exec']


def test_a_drill_that_recovers_a_database_with_its_rows_and_extensions_passes(
    declared: Recorder, drill_image: str
) -> None:
    drilled = DrillContainer(declared, drill_image).run()
    verbs = [call[0] for call in drilled.calls]

    assert drilled.status == 0, drilled.stderr
    assert verbs[0] == 'delete'
    assert verbs.index('apply') < verbs.index('wait') < verbs.index('exec')
    assert drilled.stdout.splitlines()[-1] == 'passed: app recovered, Expense holds 42 rows'
    _removed_last(drilled)


@pytest.mark.parametrize(
    ('recovered', 'found'),
    [
        ({'FAKE_READY': '0'}, 'ledger-drill did not become ready'),
        ({'FAKE_POD': ''}, 'ledger-drill has no primary'),
        ({'FAKE_IN_RECOVERY': 't'}, 'ledger-drill is still in recovery'),
        ({'FAKE_ROWS': '0'}, 'Expense holds no rows'),
        ({'FAKE_EXTENSIONS': 'plpgsql pg_cron'}, 'the extension cube is missing'),
        ({'FAKE_EXEC_FAIL': 'table'}, 'Expense could not be read in app'),
        ({'FAKE_EXEC_FAIL': 'all'}, 'app does not answer in ledger-drill-1'),
    ],
    ids=[
        'never ready',
        'no primary',
        'still in recovery',
        'no rows',
        'an extension missing',
        'a table that cannot be read',
        'a database that does not answer',
    ],
)
def test_a_drill_that_finds_the_recovery_wanting_fails_naming_why_and_removes_what_it_made(
    declared: Recorder, drill_image: str, recovered: dict[str, str], found: str
) -> None:
    drilled = DrillContainer(declared, drill_image, **recovered).run()
    failures = [line for line in drilled.stderr.splitlines() if line.startswith('FAIL: ')]

    assert drilled.status == 1, drilled.stdout + drilled.stderr
    assert any(found in line for line in failures), failures
    _removed_last(drilled)


def test_a_table_that_cannot_be_read_leaves_the_other_checks_to_run(declared: Recorder, drill_image: str) -> None:
    """Each check fails on a line of its own, so one that fails does not hide the next."""
    drilled = DrillContainer(declared, drill_image, FAKE_EXEC_FAIL='table', FAKE_EXTENSIONS='plpgsql').run()
    failures = [line for line in drilled.stderr.splitlines() if line.startswith('FAIL: ')]

    assert 'select extname from pg_extension' in _queried(drilled)
    assert len(failures) == 3, failures


def test_a_database_that_does_not_answer_is_reported_as_that_and_nothing_else(
    declared: Recorder, drill_image: str
) -> None:
    """A wrong database name is not a recovery that failed: the reachability check stops the run before the checks."""
    drilled = DrillContainer(declared, drill_image, FAKE_EXEC_FAIL='all').run()
    failures = [line for line in drilled.stderr.splitlines() if line.startswith('FAIL: ')]

    assert _queried(drilled) == ['select 1']
    assert len(failures) == 1, failures
    assert 'does not exist' in failures[0]


def test_a_drill_stopped_at_its_deadline_still_removes_its_cluster(declared: Recorder, drill_image: str) -> None:
    """The kubelet's TERM to the pod's PID 1, mid-wait, ends the run by removing the throwaway cluster."""
    drilled = DrillContainer(declared, drill_image, FAKE_WAIT_HANG='1').run_stopped()

    assert drilled.status == 1, drilled.stdout + drilled.stderr
    assert any('stopped by SIGTERM' in line for line in drilled.stderr.splitlines()), drilled.stderr
    _removed_last(drilled)


# -- The drill, admitted ------------------------------------------------------------------
#
# The drills' policy is judged by the API server the cluster runs: the
# `kube-apiserver` and `etcd` images the pinned Talos release names by default
# (`talosctl images default`), which is what the cluster's control plane runs,
# with `kubectl` from the drill's own image as the client. The two are pinned
# here by the tag the release names and that tag's digest, and pulled by the
# digest (`tests/pinned_image.py`); a case holds the tags to the release's, so
# a Talos bump onto another Kubernetes is red until the pins move with it.
# The three share one
# network namespace of their own and publish nothing. The server admits every
# request its authorizer is asked about, so what a case reads is admission
# alone; a request is sent as an account by impersonating it, and a create is
# sent with `--dry-run=server`, which admission judges as it judges a create.

#: The policy's types, and the one CRD the policy is about.
ADMISSION_POLICY = 'kubernetes:admissionregistration.k8s.io/v1:ValidatingAdmissionPolicy'
ADMISSION_BINDING = 'kubernetes:admissionregistration.k8s.io/v1:ValidatingAdmissionPolicyBinding'
CLUSTER_DEFINITION = 'clusters.postgresql.cnpg.io'
#: The definitions the program's custom resources are generated from.
BUNDLE = Path(__file__).resolve().parents[1] / 'packages' / 'crds' / 'crds.yaml'

#: The control plane's images, each the tag the pinned Talos release names
#: and the digest the registry serves for it, by name.
CONTROL_PLANE_PINS = {
    'kube-apiserver': ImagePin(
        'registry.k8s.io/kube-apiserver',
        'v1.36.3',
        'sha256:b4bc06c81fd76f81174e6c19ddacf477acdf1583e7a5846ebbd513493aef6e43',
    ),
    'etcd': ImagePin(
        'registry.k8s.io/etcd', 'v3.6.12', 'sha256:3c2ced08f23b1183e8bd4613064c3fb6b8db5057a4d1f13c3518c76e357a07a8'
    ),
}
#: The server's port, inside the network namespace the three containers share.
SERVER_PORT = 6443
#: The user the server's one token authenticates, who sets the server up.
ADMIN = 'admin'
#: The account of the database operator, which updates every `Cluster`.
OPERATOR = f'system:serviceaccount:{conventions.CNPG_NAMESPACE}:cloudnative-pg'
#: A namespace that holds no database, and so no drill's parameters.
BARE = 'bare'
#: A stop-loss, which nothing asserts on: the waits for the server to answer
#: and for the policy to judge.
READY_TIMEOUT = 60
#: The bound on a case of this section, past the suite's own: the first case
#: a worker runs starts the server, behind the pulls of its two images.
ADMISSION_CASE_TIMEOUT = 2 * pinned_image.PULL_TIMEOUT + 2 * READY_TIMEOUT


def podman(*args: str) -> str:
    return process_sessions.run(['podman', *args], text=True, timeout=COMMAND_TIMEOUT, check=True).stdout


def test_the_control_plane_pins_are_the_images_the_pinned_talos_release_runs() -> None:
    """The policy is judged by the API server the cluster runs, which is the one the fleet's Talos defaults to."""
    release = default_images()

    for name, pin in CONTROL_PLANE_PINS.items():
        assert (pin.repository, pin.tag) == (release[name].repository, release[name].tag), (
            f'{name}: move the pin in CONTROL_PLANE_PINS to {release[name].repository}:{release[name].tag} '
            'and the digest the registry serves for it'
        )


def definition(name: str) -> dict[str, Any]:
    """The bundled definition `name`, as the cluster installs it."""
    for document in yaml.safe_load_all(BUNDLE.read_text()):
        if isinstance(document, dict) and cast('dict[str, Any]', document).get('metadata', {}).get('name') == name:
            return cast('dict[str, Any]', document)
    raise AssertionError(f'{BUNDLE} defines no {name}')


@dataclass(frozen=True)
class ApiServer:
    """The control plane's API server, reached through `kubectl` in the container beside it."""

    client: str
    token: str

    def kubectl(
        self, *args: str, user: str | None = None, document: Mapping[str, Any] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """`kubectl` with `args`, as `user` where one is named and as `ADMIN` otherwise, reading `document`."""
        return process_sessions.run(
            [
                'podman', 'exec', '--interactive', self.client, 'kubectl',
                f'--server=https://127.0.0.1:{SERVER_PORT}', '--insecure-skip-tls-verify', f'--token={self.token}',
                *([f'--as={user}'] if user is not None else []),
                *args,
            ],
            text=True,
            timeout=COMMAND_TIMEOUT,
            input=json.dumps(document) if document is not None else None,
        )  # fmt: skip

    def must(self, *args: str, document: Mapping[str, Any] | None = None) -> str:
        """`kubectl` with `args` as `ADMIN`, which must succeed."""
        done = self.kubectl(*args, document=document)
        assert done.returncode == 0, done.stderr
        return done.stdout

    def created(self, document: Mapping[str, Any], user: str) -> subprocess.CompletedProcess[str]:
        """`document` created by `user`, as admission judges it, and nothing stored."""
        return self.kubectl('create', '--dry-run=server', '-o', 'name', '-f', '-', user=user, document=document)


def refused(done: subprocess.CompletedProcess[str]) -> bool:
    """Whether the drills' policy refused the request, as the server words a policy's refusal."""
    return done.returncode != 0 and f"ValidatingAdmissionPolicy '{DRILL_POLICY}'" in done.stderr


def until(probe: Callable[[], bool], what: str) -> None:
    """Return once `probe` holds, within `READY_TIMEOUT`, a stop-loss."""
    bound = time.monotonic() + READY_TIMEOUT
    while not probe():
        assert time.monotonic() < bound, f'{what} within {READY_TIMEOUT}s'
        time.sleep(0.2)


def drill_account(monitor: Recorder, namespace: str, database: str | None = None) -> str:
    """The user name the drill's pod calls the API as: its service account's (Kubernetes' authentication docs)."""
    account = drill_pod(drill_cronjob(monitor, namespace, database))['spec']['serviceAccountName']
    return f'system:serviceaccount:{namespace}:{account}'


def typed(api_version: str, kind: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
    return {'apiVersion': api_version, 'kind': kind, **inputs}


@pytest.fixture(scope='module')
def apiserver(declared: Recorder, tmp_path_factory: pytest.TempPathFactory) -> Iterator[ApiServer]:
    """The control plane's API server, holding the drills' policy and the parameters of every declared drill."""
    pinned_image.require_podman("the drills' policy is judged by the API server image the cluster runs")
    images = {name: pinned_image.pulled(pin) for name, pin in CONTROL_PLANE_PINS.items()}
    client_image = pinned_image.pulled(DRILL_IMAGE)
    files = tmp_path_factory.mktemp('apiserver')
    key = ec.generate_private_key(ec.SECP256R1())
    _ = (files / 'sa.key').write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    _ = (files / 'sa.pub').write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    token = uuid.uuid4().hex
    _ = (files / 'tokens.csv').write_text(f'{token},{ADMIN},{ADMIN},"system:masters"\n')
    suffix = uuid.uuid4().hex[:12]
    etcd, server, client = (f'kluster-test-{role}-{suffix}' for role in ('etcd', 'apiserver', 'client'))
    common = ['--rm', '--label', LABEL, '--timeout', str(pinned_image.CONTAINER_TIMEOUT)]
    beside = ['--network', f'container:{etcd}']
    # The two beside etcd go first: podman refuses to remove a container whose network another shares.
    with process_sessions.removing(client, server, etcd, timeout=COMMAND_TIMEOUT):
        _ = podman(
            'run', '--detach', '--name', etcd, *common, '--network', 'none', '--tmpfs', '/data',
            images['etcd'], 'etcd', '--data-dir=/data',
            '--listen-client-urls=http://127.0.0.1:2379', '--advertise-client-urls=http://127.0.0.1:2379',
            '--listen-peer-urls=http://127.0.0.1:2380',
        )  # fmt: skip
        _ = podman(
            'create', '--name', server, *common, *beside, '--tmpfs', '/certs',
            images['kube-apiserver'], 'kube-apiserver',
            '--etcd-servers=http://127.0.0.1:2379', f'--secure-port={SERVER_PORT}',
            '--bind-address=127.0.0.1', '--advertise-address=127.0.0.1', '--cert-dir=/certs',
            '--service-cluster-ip-range=10.0.0.0/24', '--service-account-issuer=https://kubernetes.default.svc',
            '--service-account-key-file=/sa.pub', '--service-account-signing-key-file=/sa.key',
            '--token-auth-file=/tokens.csv', '--authorization-mode=AlwaysAllow',
        )  # fmt: skip
        for name in ('sa.key', 'sa.pub', 'tokens.csv'):
            _ = podman('cp', str(files / name), f'{server}:/{name}')
        _ = podman('start', server)
        _ = podman('run', '--detach', '--name', client, *common, *beside, '--entrypoint', 'sleep', client_image,
                   str(pinned_image.CONTAINER_TIMEOUT))  # fmt: skip
        running = ApiServer(client, token)
        until(lambda: running.kubectl('get', '--raw', '/readyz').returncode == 0, 'the API server did not answer')

        _ = running.must('create', '-f', '-', document=definition(CLUSTER_DEFINITION))
        _ = running.must('wait', '--for=condition=Established', f'crd/{CLUSTER_DEFINITION}', '--timeout=30s')
        namespaces = {'ledger', 'pair', BARE}
        for namespace in sorted(namespaces):
            _ = running.must('create', 'namespace', namespace)
        for parameters in of_type_labelled(declared, CONFIGMAP, DRILL_ADMISSION_LABEL):
            if parameters['metadata']['namespace'] in namespaces:
                _ = running.must('create', '-f', '-', document=typed('v1', 'ConfigMap', parameters))
        group = 'admissionregistration.k8s.io/v1'
        _ = running.must(
            'create', '-f', '-',
            document=typed(group, 'ValidatingAdmissionPolicy', declared.inputs_of('drill-admission', ADMISSION_POLICY)),
        )  # fmt: skip
        _ = running.must(
            'create', '-f', '-',
            document=typed(
                group, 'ValidatingAdmissionPolicyBinding', declared.inputs_of('drill-admission', ADMISSION_BINDING)
            ),
        )  # fmt: skip
        # The server takes in a policy behind its informers, so the first
        # requests can come before it judges; this is the first it refuses.
        stray = renamed(drill_manifest(declared, 'ledger'), 'ledger-stray')
        until(lambda: refused(running.created(stray, drill_account(declared, 'ledger'))), 'the policy did not judge')
        yield running


def of_type_labelled(monitor: Recorder, typ: str, label: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if label in it.inputs['metadata'].get('labels', {})]


def changed(
    manifest: Mapping[str, Any], path: tuple[str, ...], entries: Mapping[str, str], *, merged: bool = True
) -> dict[str, Any]:
    """`manifest` with the map at `path` merged with `entries`, or replaced by them where not `merged`."""
    copied = cast('dict[str, Any]', json.loads(json.dumps(manifest)))
    at = copied
    for key in path[:-1]:
        at = at.setdefault(key, {})
    at[path[-1]] = {**at.get(path[-1], {}), **entries} if merged else dict(entries)
    return copied


def swapped(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """`manifest` with an inherited label of its own, other than the API client label, traded for the mail sender label."""
    own = dict(manifest['spec']['inheritedMetadata']['labels'])
    del own[next(key for key in sorted(own) if key not in conventions.API_CLIENT_LABEL)]
    return changed(manifest, INHERITED_LABELS, {**own, **conventions.MAIL_SENDER_LABEL}, merged=False)


def renamed(manifest: Mapping[str, Any], name: str) -> dict[str, Any]:
    copied = cast('dict[str, Any]', json.loads(json.dumps(manifest)))
    copied['metadata']['name'] = name
    return copied


INHERITED_LABELS = ('spec', 'inheritedMetadata', 'labels')
INHERITED_ANNOTATIONS = ('spec', 'inheritedMetadata', 'annotations')
OWN_LABELS = ('metadata', 'labels')
OWN_ANNOTATIONS = ('metadata', 'annotations')
#: The annotation CloudNativePG applies to each instance pod as a JSON patch
#: (`buildInstance` in its `pkg/specs/pods.go`, at v1.30.0), and the
#: annotation client-side `kubectl apply` writes what it applied under.
POD_PATCH = 'cnpg.io/podPatch'
LAST_APPLIED = 'kubectl.kubernetes.io/last-applied-configuration'


def pod_patch(labels: Mapping[str, str]) -> dict[str, str]:
    """A pod patch that adds `labels` to the instance pod: one JSON patch `add` per label, its key escaped as RFC 6901 has it."""
    escaped = {key.replace('~', '~0').replace('/', '~1'): value for key, value in labels.items()}
    return {
        POD_PATCH: json.dumps(
            [{'op': 'add', 'path': f'/metadata/labels/{key}', 'value': value} for key, value in escaped.items()]
        )
    }


def applied_by_kubectl(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """`manifest` as client-side `kubectl apply` sends it: carrying itself as the configuration it applied."""
    return changed(manifest, OWN_ANNOTATIONS, {LAST_APPLIED: json.dumps(manifest)})


#: What differs from the drill's manifest on a field the policy holds, each a
#: change of the manifest: a label a grant is keyed on, on the cluster,
#: carried to its pods or patched onto them, beside its own labels or in
#: place of one of them; its labels taken away or naming another database;
#: and another name, its database's own among them, which in this suite is
#: its namespace's.
NOT_THE_MANIFESTS: Mapping[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    'the mail sender label inherited': lambda it: changed(it, INHERITED_LABELS, conventions.MAIL_SENDER_LABEL),
    'the mail sender label on the cluster': lambda it: changed(it, OWN_LABELS, conventions.MAIL_SENDER_LABEL),
    'the API client label on the cluster': lambda it: changed(it, OWN_LABELS, conventions.API_CLIENT_LABEL),
    'the mail sender label as an inherited annotation': lambda it: changed(
        it, INHERITED_ANNOTATIONS, conventions.MAIL_SENDER_LABEL
    ),
    'a pod patch that adds the mail sender label': lambda it: changed(
        it, OWN_ANNOTATIONS, pod_patch(conventions.MAIL_SENDER_LABEL)
    ),
    'the same pod patch beside the annotation kubectl apply writes': lambda it: changed(
        applied_by_kubectl(it), OWN_ANNOTATIONS, pod_patch(conventions.MAIL_SENDER_LABEL)
    ),
    'one of its inherited labels traded for the mail sender label': swapped,
    'its inherited labels taken away': lambda it: changed(it, INHERITED_LABELS, {}, merged=False),
    'its database label naming another database': lambda it: changed(it, INHERITED_LABELS, {DATABASE_LABEL: 'other'}),
    'another name': lambda it: renamed(it, f'{it["metadata"]["name"]}-2'),
    "the database's name": lambda it: renamed(it, it['metadata']['namespace']),
}


@pytest.mark.timeout(ADMISSION_CASE_TIMEOUT)
@pytest.mark.parametrize(('namespace', 'database'), [('ledger', None), ('pair', 'alpha'), ('pair', 'beta')])
def test_a_drill_is_admitted_to_create_the_cluster_its_manifest_names(
    declared: Recorder, apiserver: ApiServer, namespace: str, database: str | None
) -> None:
    """The manifest the drill applies, by the account it runs as: alone in its namespace, and beside another database."""
    done = apiserver.created(
        drill_manifest(declared, namespace, database), drill_account(declared, namespace, database)
    )

    assert done.returncode == 0, done.stderr


@pytest.mark.timeout(ADMISSION_CASE_TIMEOUT)
def test_a_drill_is_admitted_with_the_annotation_its_kubectl_apply_writes(
    declared: Recorder, apiserver: ApiServer
) -> None:
    """The drill applies its manifest with client-side `kubectl apply`, which records it on the cluster as an annotation."""
    done = apiserver.created(applied_by_kubectl(drill_manifest(declared, 'ledger')), drill_account(declared, 'ledger'))

    assert done.returncode == 0, done.stderr


@pytest.mark.timeout(ADMISSION_CASE_TIMEOUT)
@pytest.mark.parametrize('change', list(NOT_THE_MANIFESTS))
def test_a_drill_is_refused_a_cluster_that_differs_from_its_manifest_where_the_policy_holds_it(
    declared: Recorder, apiserver: ApiServer, change: str
) -> None:
    """A poisoned drill image holds the drill's token, and a cluster's metadata reaches the pods the operator starts."""
    document = NOT_THE_MANIFESTS[change](drill_manifest(declared, 'ledger'))

    assert refused(done := apiserver.created(document, drill_account(declared, 'ledger'))), done.stderr


@pytest.mark.timeout(ADMISSION_CASE_TIMEOUT)
def test_a_drill_is_refused_the_cluster_of_the_other_database_in_its_namespace(
    declared: Recorder, apiserver: ApiServer
) -> None:
    """Each database's parameters hold its own drill, and the binding holds a request to all of them at once."""
    done = apiserver.created(drill_manifest(declared, 'pair', 'beta'), drill_account(declared, 'pair', 'alpha'))

    assert refused(done), done.stderr


@pytest.mark.timeout(ADMISSION_CASE_TIMEOUT)
@pytest.mark.parametrize('user', [ADMIN, OPERATOR])
def test_a_namespace_holding_no_parameters_is_not_judged(declared: Recorder, apiserver: ApiServer, user: str) -> None:
    """The program may create a database's cluster before the parameters beside it, which nothing orders it behind."""
    document = {**drill_manifest(declared, 'ledger'), 'metadata': {'name': 'first', 'namespace': BARE}}

    assert (done := apiserver.created(document, user)).returncode == 0, done.stderr


@pytest.mark.timeout(ADMISSION_CASE_TIMEOUT)
@pytest.mark.parametrize('user', [ADMIN, OPERATOR, 'system:serviceaccount:ledger:default'])
def test_every_other_account_is_not_judged(declared: Recorder, apiserver: ApiServer, user: str) -> None:
    """The program, the operator and a namespace's other accounts write clusters the policy is no judge of."""
    manifest = drill_manifest(declared, 'ledger')
    for change, made in NOT_THE_MANIFESTS.items():
        done = apiserver.created(made(manifest), user)
        assert done.returncode == 0, (change, done.stderr)


@pytest.mark.timeout(ADMISSION_CASE_TIMEOUT)
def test_a_drill_is_refused_a_change_of_what_the_policy_holds(declared: Recorder, apiserver: ApiServer) -> None:
    """The role patches the drill's cluster, so an update is judged as a create is."""
    manifest = drill_manifest(declared, 'ledger')
    name, namespace = manifest['metadata']['name'], manifest['metadata']['namespace']
    account = drill_account(declared, 'ledger')
    _ = apiserver.must('create', '-f', '-', document=manifest)
    try:
        patch = ['patch', 'cluster', name, '-n', namespace, '--dry-run=server', '--type=merge', '-p']
        widened = json.dumps({'spec': {'inheritedMetadata': {'labels': dict(conventions.MAIL_SENDER_LABEL)}}})
        kept = json.dumps({'metadata': {'annotations': {LAST_APPLIED: json.dumps(manifest)}}})

        assert refused(done := apiserver.kubectl(*patch, widened, user=account)), done.stderr
        assert (done := apiserver.kubectl(*patch, kept, user=account)).returncode == 0, done.stderr
    finally:
        _ = apiserver.must('delete', 'cluster', name, '-n', namespace, '--wait=true')
