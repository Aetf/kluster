"""The hourly etcd snapshot `k8s-base` declares: its Talos grant, its writer key, its recipients and its CronJob.

The run is the whole `k8s-base` program, once with every sealed value the
stack declares sealed in and once with none, which is the stack before the
bring-up has sealed anything (physical/bring-up.md §1).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pulumi
import pytest
import pytest_asyncio
from k8s_base_installation import SEALED, Run, applied

from kluster import conventions
from kluster.components import talos
from kluster.components.backup import etcd_scope
from kluster.components.backup.etcd import (
    SERVICE_ACCOUNT,
    TALOS_SECRET_DIR,
    TALOS_SERVICE_ACCOUNT,
    TALOS_SERVICE_ACCOUNT_API_VERSION,
    EtcdSnapshots,
)
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.conventions.schedule import ETCD_SNAPSHOT
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PodSecurity
from kluster.lib.state_backend import committed
from kluster.lib.versions import ImagePin

CRONJOB = 'kubernetes:batch/v1:CronJob'
NAMESPACE = 'kubernetes:core/v1:Namespace'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
TALOS_SERVICE_ACCOUNT_TYPE = f'kubernetes:{TALOS_SERVICE_ACCOUNT_API_VERSION}:{TALOS_SERVICE_ACCOUNT}'
COMPONENT = 'kluster:backup:EtcdSnapshots'

KEY = conventions.sealed.ETCD_WRITER_KEY


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def sealed() -> Run:
    """Every sealed value the stack declares, sealed in."""
    return await applied(sealed=SEALED)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def absent() -> tuple[Run, list[str]]:
    """No sealed value at all, and the warnings the run logged."""
    warned: list[str] = []

    def warn(message: str, *_: object, **__: object) -> None:
        warned.append(message)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.log, 'warn', warn)
        run = await applied()
    return run, warned


def _named(run: Run, typ: str, name: str) -> list[dict[str, Any]]:
    return [it.inputs for it in run.monitor.of_type(typ) if it.inputs.get('metadata', {}).get('name') == name]


def _pod(run: Run) -> dict[str, Any]:
    (cronjob,) = _named(run, CRONJOB, ETCD_SNAPSHOT.cronjob)
    return cronjob['spec']['jobTemplate']['spec']['template']['spec']


def _environment(run: Run) -> dict[str, Any]:
    (container,) = _pod(run)['containers']
    return {it['name']: it.get('value', it.get('valueFrom')) for it in container['env']}


def test_the_service_account_asks_for_the_grant_the_control_planes_configuration_allows(sealed: Run) -> None:
    """Talos refuses a role, or a namespace, the machine configuration does not list, and writes the pod no talosconfig.

    The two sides are two programs: `physical` renders the control planes'
    configuration, and `k8s-base` declares the `ServiceAccount`.
    """
    allowed = talos.control_plane_patch(cert_sans=['203.0.113.10'])['machine']['features']['kubernetesTalosAPIAccess']
    (account,) = _named(sealed, TALOS_SERVICE_ACCOUNT_TYPE, SERVICE_ACCOUNT)

    assert allowed['enabled'] is True
    assert account['metadata']['namespace'] in allowed['allowedKubernetesNamespaces']
    assert set(account['spec']['roles']) <= set(allowed['allowedRoles'])


def test_the_namespace_is_restricted_and_holds_the_etcd_backups_objects_alone(sealed: Run) -> None:
    """The grant reaches every pod of its namespace, so nothing but the etcd backup's own component declares there."""
    (namespace,) = _named(sealed, NAMESPACE, ETCD_SNAPSHOT.namespace)
    in_namespace = {
        (it.typ, it.name)
        for it in sealed.monitor.declared
        if it.inputs.get('metadata', {}).get('namespace') == ETCD_SNAPSHOT.namespace
    }
    children = {(typ, name) for typ, name, _ in sealed.children(COMPONENT)}

    assert namespace['metadata']['labels'][POD_SECURITY_ENFORCE_LABEL] == PodSecurity.RESTRICTED.value
    assert in_namespace, 'nothing declared in the namespace'
    assert in_namespace <= children


def test_without_the_writer_key_nothing_but_the_namespace_and_the_grant_is_declared_and_the_key_is_named(
    absent: tuple[Run, list[str]],
) -> None:
    """No CronJob, so no series for the family's rules to read, until the key is sealed in."""
    run, warnings = absent

    assert _named(run, NAMESPACE, ETCD_SNAPSHOT.namespace)
    assert _named(run, TALOS_SERVICE_ACCOUNT_TYPE, SERVICE_ACCOUNT)
    assert not _named(run, CRONJOB, ETCD_SNAPSHOT.cronjob)
    assert not _named(run, SEALED_SECRET, KEY.name)
    assert [it for it in warnings if repr(KEY.name) in it and KEY.namespace in it]


def test_the_pod_reads_the_talosconfig_talos_writes_for_its_service_account(sealed: Run) -> None:
    pod = _pod(sealed)
    (container,) = pod['containers']
    (mount,) = [it for it in container['volumeMounts'] if it['mountPath'] == TALOS_SECRET_DIR]
    (volume,) = [it for it in pod['volumes'] if it['name'] == mount['name']]

    # Talos writes the talosconfig into a Secret named after the
    # `ServiceAccount`, beside it.
    assert volume['secret']['secretName'] == SERVICE_ACCOUNT
    assert _named(sealed, TALOS_SERVICE_ACCOUNT_TYPE, volume['secret']['secretName'])


def test_the_pod_reads_the_writer_key_from_the_secret_its_sealed_secret_becomes(sealed: Run) -> None:
    (key,) = _named(sealed, SEALED_SECRET, KEY.name)
    environment = _environment(sealed)
    secret = key['spec']['template']['metadata']['name']

    assert key['metadata']['namespace'] == ETCD_SNAPSHOT.namespace
    assert set(key['spec']['encryptedData']) == conventions.WRITER_KEY_FIELDS
    for field in conventions.WRITER_KEY_FIELDS:
        assert environment[field] == {'secretKeyRef': {'name': secret, 'key': field}}, field


def test_each_snapshot_is_written_directly_under_the_etcd_scopes_prefix(sealed: Run) -> None:
    """The object's key is `<S3_PREFIX>/<file>` in `BUCKET`: under the prefix the scope's key writes, with nothing between.

    A key inside the prefix but a level below it, `etcd//<file>`, would still
    upload, and would be a layout neither storage.md nor `fetch` reads.
    """
    environment = _environment(sealed)
    scope = etcd_scope()

    assert environment['BUCKET'] == conventions.BUCKET_BACKUP
    # `talos-backup`'s name for an object: `<S3_PREFIX>/<CLUSTER_NAME>-<RFC 3339 UTC>.snap.age`
    # (`pkg/s3/s3.go`, `pkg/talos/talos.go` at v0.1.0).
    name = f'{environment["S3_PREFIX"]}/{environment["CLUSTER_NAME"]}-2026-11-17T06:05:00Z.snap.age'
    # Not `posixpath.dirname`, which reads `etcd//<file>` as `etcd` and
    # misses the empty level.
    assert name.startswith(scope.prefix)
    assert '/' not in name.removeprefix(scope.prefix)


def test_the_run_writes_into_a_writable_directory_of_its_own_pod(sealed: Run) -> None:
    """`talos-backup` writes the snapshot and its ciphertext into its working directory; the image's is `/`, which the run's user cannot write.

    The root filesystem is read-only besides, so the working directory is
    the one place the run can write them.
    """
    pod = _pod(sealed)
    (container,) = pod['containers']
    (mount,) = [it for it in container['volumeMounts'] if it['mountPath'] == container.get('workingDir')]
    (volume,) = [it for it in pod['volumes'] if it['name'] == mount['name']]

    assert not mount.get('readOnly') and 'emptyDir' in volume
    assert container['securityContext']['readOnlyRootFilesystem'] is True


def test_every_directory_the_run_holds_in_memory_is_bounded(sealed: Run) -> None:
    """A memory-backed directory is memory of the node it runs on, a control plane, until the pod is gone."""
    in_memory = [it for it in _pod(sealed)['volumes'] if it.get('emptyDir', {}).get('medium') == 'Memory']

    assert in_memory
    for volume in in_memory:
        assert volume['emptyDir'].get('sizeLimit'), volume['name']


def test_each_snapshot_is_encrypted_to_every_escrowed_generation_on_file(sealed: Run) -> None:
    """The recipients are the committed file the state backend's dumps are encrypted to, so one rotation moves both."""
    recipients = _environment(sealed)['AGE_X25519_PUBLIC_KEY'].split(',')

    assert sorted(recipients) == sorted(committed.backup_recipients().values())


def test_the_snapshot_runs_on_the_control_planes_and_not_on_the_worker(sealed: Run) -> None:
    """The snapshots go on through an outage of the home site, and the stream stays off the mesh to the worker."""
    required = _pod(sealed)['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
    # As Talos labels the nodes: every cloud node is a control plane, the
    # worker is not.
    nodes: dict[str, dict[str, str]] = {
        **{node: {CONTROL_PLANE_LABEL: ''} for node in conventions.CLOUD_NODES},
        conventions.HOMELAB_NODE: {},
    }

    def holds(expression: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
        return {'Exists': True, 'DoesNotExist': False}[expression['operator']] == (expression['key'] in labels)

    def admits(labels: Mapping[str, str]) -> bool:
        return any(all(holds(it, labels) for it in term['matchExpressions']) for term in required['nodeSelectorTerms'])

    assert {node for node, labels in nodes.items() if admits(labels)} == set(conventions.CLOUD_NODES)


def test_an_empty_recipient_list_is_refused() -> None:
    """`talos-backup` fails every run on an empty list; the program refuses it where it is written."""
    with pytest.raises(ValueError, match='recipient'):
        _ = EtcdSnapshots(
            'etcd-snapshots',
            image=ImagePin('registry.example.invalid/talos-backup', 'v1', f'sha256:{"0" * 64}'),
            writer_key=None,
            recipients=(),
            after=[],
            opts=pulumi.ResourceOptions(),
        )
