"""The hourly etcd snapshot `k8s-base` declares: its Talos grant, its writer key, its recipients and its CronJob.

The run is the whole `k8s-base` program, once with what the stack file holds,
every sealed value the stack declares sealed in, and once more with the
restore-verify's recipient file in a directory of the module's, holding the recipient of an
identity the module draws.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import image_busybox
import process_sessions
import pulumi
import pytest
import pytest_asyncio
from k8s_base_installation import Run, applied
from k8s_quantity import MIB, quantity
from node_selection import CLOUD, schedulable

from kluster import conventions
from kluster.components import talos
from kluster.components.backup import etcd_scope
from kluster.components.backup.etcd import (
    PROCESS_MEMORY_MIB,
    SERVICE_ACCOUNT,
    TALOS_SECRET_DIR,
    TALOS_SERVICE_ACCOUNT,
    TALOS_SERVICE_ACCOUNT_API_VERSION,
    EtcdSnapshots,
)
from kluster.conventions.schedule import ETCD_SNAPSHOT
from kluster.lib import etcd_backup
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PodSecurity
from kluster.lib.state_backend import committed
from kluster.lib.versions import ImagePin
from kluster.scripts.credentials import age, derived

CRONJOB = 'kubernetes:batch/v1:CronJob'
NAMESPACE = 'kubernetes:core/v1:Namespace'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
TALOS_SERVICE_ACCOUNT_TYPE = f'kubernetes:{TALOS_SERVICE_ACCOUNT_API_VERSION}:{TALOS_SERVICE_ACCOUNT}'
#: The components that declare in the snapshots' namespace: the snapshot's
#: own, and its restore-verify's.
COMPONENTS = ('kluster:backup:EtcdSnapshots', 'kluster:backup:EtcdRestoreVerify')

#: The pin of the image whose `age` the restore-verify decrypts with, and where
#: the image keeps it (`docker/etcd-age.Containerfile`).
AGE_PIN = 'etcd-verify'
AGE_BINARY = '/usr/local/bin/age'
#: The bound on one `age` run, which encrypts or decrypts a line.
AGE_TIMEOUT = 15

KEY = conventions.sealed.ETCD_WRITER_KEY


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The stack as its file holds it: every sealed value it declares, sealed in."""
    return await applied()


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def drilled(tmp_path_factory: pytest.TempPathFactory) -> tuple[Run, age.Identity]:
    """Every sealed value sealed in, and the restore-verify's recipient file holding the recipient of an identity drawn here."""
    identity = age.generate()
    recipient_file = tmp_path_factory.mktemp('etcd-drill') / 'recipient.txt'
    _ = recipient_file.write_text(f'# the restore-verify, public half\n{identity.public}\n')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(etcd_backup, 'ETCD_DRILL_RECIPIENT', recipient_file)
        run = await applied()
    return run, identity


def _named(run: Run, typ: str, name: str) -> list[dict[str, Any]]:
    return [it.inputs for it in run.monitor.of_type(typ) if it.inputs.get('metadata', {}).get('name') == name]


def _pod(run: Run) -> dict[str, Any]:
    (cronjob,) = _named(run, CRONJOB, ETCD_SNAPSHOT.cronjob)
    return cronjob['spec']['jobTemplate']['spec']['template']['spec']


def _environment(run: Run) -> dict[str, Any]:
    (container,) = _pod(run)['containers']
    return {it['name']: it.get('value', it.get('valueFrom')) for it in container['env']}


def test_the_service_account_asks_for_the_grant_the_control_planes_configuration_allows(applied: Run) -> None:
    """Talos refuses a role, or a namespace, the machine configuration does not list, and writes the pod no talosconfig.

    The two sides are two programs: `physical` renders the control planes'
    configuration, and `k8s-base` declares the `ServiceAccount`.
    """
    allowed = talos.control_plane_patch(cert_sans=['203.0.113.10'])['machine']['features']['kubernetesTalosAPIAccess']
    (account,) = _named(applied, TALOS_SERVICE_ACCOUNT_TYPE, SERVICE_ACCOUNT)

    assert allowed['enabled'] is True
    assert account['metadata']['namespace'] in allowed['allowedKubernetesNamespaces']
    assert set(account['spec']['roles']) <= set(allowed['allowedRoles'])


def test_the_namespace_is_restricted_and_holds_the_etcd_backups_objects_alone(applied: Run) -> None:
    """The grant reaches every pod of its namespace, so nothing but the etcd backup's own component declares there."""
    (namespace,) = _named(applied, NAMESPACE, ETCD_SNAPSHOT.namespace)
    in_namespace = {
        (it.typ, it.name)
        for it in applied.monitor.declared
        if it.inputs.get('metadata', {}).get('namespace') == ETCD_SNAPSHOT.namespace
    }
    children = {(typ, name) for component in COMPONENTS for typ, name, _ in applied.children(component)}

    assert namespace['metadata']['labels'][POD_SECURITY_ENFORCE_LABEL] == PodSecurity.RESTRICTED.value
    assert in_namespace, 'nothing declared in the namespace'
    assert in_namespace <= children


def test_the_pod_reads_the_talosconfig_talos_writes_for_its_service_account(applied: Run) -> None:
    pod = _pod(applied)
    (container,) = pod['containers']
    (mount,) = [it for it in container['volumeMounts'] if it['mountPath'] == TALOS_SECRET_DIR]
    (volume,) = [it for it in pod['volumes'] if it['name'] == mount['name']]

    # Talos writes the talosconfig into a Secret named after the
    # `ServiceAccount`, beside it.
    assert volume['secret']['secretName'] == SERVICE_ACCOUNT
    assert _named(applied, TALOS_SERVICE_ACCOUNT_TYPE, volume['secret']['secretName'])


def test_the_pod_reads_the_writer_key_from_the_secret_its_sealed_secret_becomes(applied: Run) -> None:
    (key,) = _named(applied, SEALED_SECRET, KEY.name)
    environment = _environment(applied)
    secret = key['spec']['template']['metadata']['name']

    assert key['metadata']['namespace'] == ETCD_SNAPSHOT.namespace
    assert set(key['spec']['encryptedData']) == conventions.WRITER_KEY_FIELDS
    for field in conventions.WRITER_KEY_FIELDS:
        assert environment[field] == {'secretKeyRef': {'name': secret, 'key': field}}, field


def test_each_snapshot_is_written_directly_under_the_etcd_scopes_prefix(applied: Run) -> None:
    """The object's key is `<S3_PREFIX>/<file>` in `BUCKET`: under the prefix the scope's key writes, with nothing between.

    A key inside the prefix but a level below it, `etcd//<file>`, would still
    upload, and would be a layout neither storage.md nor `fetch` reads.
    """
    environment = _environment(applied)
    scope = etcd_scope()

    assert environment['BUCKET'] == conventions.BUCKET_BACKUP
    # `talos-backup`'s name for an object: `<S3_PREFIX>/<CLUSTER_NAME>-<RFC 3339 UTC>.snap.age`
    # (`pkg/s3/s3.go`, `pkg/talos/talos.go` at v0.1.0).
    name = f'{environment["S3_PREFIX"]}/{environment["CLUSTER_NAME"]}-2026-11-17T06:05:00Z.snap.age'
    # Not `posixpath.dirname`, which reads `etcd//<file>` as `etcd` and
    # misses the empty level.
    assert name.startswith(scope.prefix)
    assert '/' not in name.removeprefix(scope.prefix)


def test_the_run_writes_into_a_writable_directory_of_its_own_pod(applied: Run) -> None:
    """`talos-backup` writes the snapshot and its ciphertext into its working directory; the image's is `/`, which the run's user cannot write.

    The root filesystem is read-only besides, so the working directory is
    the one place the run can write them.
    """
    pod = _pod(applied)
    (container,) = pod['containers']
    (mount,) = [it for it in container['volumeMounts'] if it['mountPath'] == container.get('workingDir')]
    (volume,) = [it for it in pod['volumes'] if it['name'] == mount['name']]

    assert not mount.get('readOnly') and 'emptyDir' in volume
    assert container['securityContext']['readOnlyRootFilesystem'] is True


def test_every_directory_the_run_holds_in_memory_is_bounded(applied: Run) -> None:
    """A memory-backed directory is memory of the node it runs on, until the pod is gone."""
    in_memory = [it for it in _pod(applied)['volumes'] if it.get('emptyDir', {}).get('medium') == 'Memory']

    assert in_memory
    for volume in in_memory:
        assert volume['emptyDir'].get('sizeLimit'), volume['name']


def test_every_container_requests_and_is_limited_in_cpu_and_memory(applied: Run) -> None:
    """The pod may be scheduled to a cloud node, where etcd shares the cores (workloads.md §1)."""
    pod = _pod(applied)
    containers = [*pod.get('initContainers', []), *pod['containers']]

    assert containers
    for container in containers:
        resources = container['resources']
        for resource in ('cpu', 'memory'):
            request, limit = resources['requests'][resource], resources['limits'][resource]
            assert quantity(request) <= quantity(limit), (container['name'], resource)


def test_the_memory_limit_holds_every_in_memory_directory_and_the_processes_share_above_it(applied: Run) -> None:
    """A memory-backed directory is charged to the pod, so a database that outgrows it fails naming the directory.

    A limit under the directories' bounds and the processes' share would end
    such a run as an out-of-memory kill, which names nothing, rather than as
    `no space left on device`.
    """
    pod = _pod(applied)
    (container,) = pod['containers']
    in_memory = sum(
        quantity(it['emptyDir']['sizeLimit'])
        for it in pod['volumes']
        if it.get('emptyDir', {}).get('medium') == 'Memory'
    )

    assert quantity(container['resources']['limits']['memory']) >= in_memory + PROCESS_MEMORY_MIB * MIB


def test_each_snapshot_is_encrypted_to_every_escrowed_generation_on_file_and_then_to_the_restore_verify(
    applied: Run,
) -> None:
    """The generations are the committed file the state backend's dumps are encrypted to, so one rotation moves both.

    The restore-verify's recipient comes after them: it stands outside the
    generations, and a reader of the list finds the generations where the
    dumps' own list has them.
    """
    recipients = _environment(applied)['AGE_X25519_PUBLIC_KEY'].split(',')
    generations = list(committed.backup_recipients().values())

    assert sorted(recipients[: len(generations)]) == sorted(generations)
    assert recipients[len(generations) :] == [etcd_backup.drill_recipient()]


def test_a_snapshot_encrypted_as_declared_opens_with_the_identity_whose_recipient_is_on_file(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    tmp_path: Path,
    drilled: tuple[Run, age.Identity],
) -> None:
    """What `talos-backup` encrypts to opens with the restore-verify's identity, by the `age` the verify decrypts with.

    The recipient file is the module's, holding an identity drawn here, so
    the run reads it from where the program was pointed, not from the file
    committed in the checkout, whose identity nobody here holds.
    """
    run, identity = drilled
    cache = image_busybox.cache_directory(request.config, tmp_path_factory)
    tool = str(image_busybox.copied(image_busybox.pinned(AGE_PIN), AGE_BINARY, cache))
    recipients = _environment(run)['AGE_X25519_PUBLIC_KEY'].split(',')
    plain, sealed_file, key = tmp_path / 'snapshot.db', tmp_path / 'snapshot.db.age', tmp_path / 'identity'
    _ = plain.write_bytes(b'a snapshot of the keyspace')
    _ = key.write_text(f'{identity.secret}\n')

    _ = process_sessions.run(
        [tool, *(arg for it in recipients for arg in ('-r', it)), '-o', str(sealed_file), str(plain)],
        timeout=AGE_TIMEOUT,
        check=True,
    )
    opened = process_sessions.run([tool, '-d', '-i', str(key), str(sealed_file)], timeout=AGE_TIMEOUT, check=True)

    assert opened.stdout == plain.read_bytes()


def test_an_absent_recipient_file_is_refused_naming_the_command_that_writes_it(tmp_path: Path) -> None:
    with pytest.raises(
        committed.Refused, match=f'`credentials derived {derived.ETCD_DRILL_AGE_IDENTITY_ROW} generate`'
    ):
        _ = etcd_backup.drill_recipient(tmp_path / 'absent.txt')


@pytest.mark.parametrize('form', ['two recipients', 'the identity', 'a plugin recipient'])
def test_a_recipient_file_its_writer_does_not_write_is_refused_without_quoting_it(tmp_path: Path, form: str) -> None:
    """One native recipient is what the generator writes; anything else is refused, and the line never reaches the message."""
    first, second = age.generate(), age.generate()
    line = {
        'two recipients': f'{first.public}\n{second.public}',
        'the identity': first.secret,
        'a plugin recipient': 'age1yubikey1qwertyuiopasdfghjklzxcvbnm',
    }[form]
    path = tmp_path / 'recipient.txt'
    _ = path.write_text(f'# the restore-verify, public half\n{line}\n')

    with pytest.raises(committed.Refused) as refused:
        _ = etcd_backup.drill_recipient(path)

    for written in line.splitlines():
        assert written not in str(refused.value)


def test_the_reader_names_the_row_the_command_tree_spells() -> None:
    """The refusal names a command; the command tree is where it is spelled, and the reader cannot import a script."""
    assert etcd_backup.DRILL_RECIPIENT_ROW == derived.ETCD_DRILL_AGE_IDENTITY_ROW


def test_the_snapshot_runs_through_an_outage_of_the_home_site(applied: Run) -> None:
    """Every cloud node may take a run, so the snapshots go on while the home site is down."""
    assert schedulable(_pod(applied)) >= CLOUD


def test_an_empty_recipient_list_is_refused() -> None:
    """`talos-backup` fails every run on an empty list; the program refuses it where it is written."""
    with pytest.raises(ValueError, match='recipient'):
        _ = EtcdSnapshots(
            'etcd-snapshots',
            image=ImagePin('registry.example.invalid/talos-backup', 'v1', f'sha256:{"0" * 64}'),
            writer_key=dict.fromkeys(conventions.sealed.ETCD_WRITER_KEY.keys, 'AgB-sealed'),
            recipients=(),
            after=[],
            opts=pulumi.ResourceOptions(),
        )
