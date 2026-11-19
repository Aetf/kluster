"""The daily etcd restore-verify `k8s-base` declares, and its script run on real snapshots under its image's own bytes.

The declaration is read off the whole `k8s-base` program, with what the stack
file holds, every sealed value the stack declares sealed in. The script runs
as the declared CronJob's container says: its command, its environment and its
mounts, each path mapped to a directory of the case's. Its `/bin/sh` and every applet it calls
are the busybox of the image the pin names (`image_busybox`), and `etcd`,
`etcdctl`, `etcdutl` and `age` are the image's own binaries, read out of it.
B2 is the fake bucket behind a fake `curl` (`fake_b2_shell`). The ports are
the one thing remapped beyond the paths: a pod has a loopback of its own, and
the cases share the host's, so each run takes ports no other holds.

Each snapshot is real: the image's `etcd`, started on the host's loopback,
written `KEY` (or, for the keyless one, a key of its own), and saved with
`etcdctl snapshot save`, as a member's snapshot is streamed with etcd's hash
at its end. It is encrypted by the image's `age`, and named in the bucket as
the snapshot job names it, from the prefix and cluster the declared
`etcd-snapshot` CronJob hands `talos-backup`.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fake_b2_shell
import image_busybox
import process_sessions
import pytest
import pytest_asyncio
from fake_b2_shell import AUTHORIZATION_V4, BUCKET, FAKE_KEY
from k8s_base_installation import Run, applied

from kluster import conventions
from kluster.components.backup.etcd import (
    VERIFY_IDENTITY_DIR,
    VERIFY_KEY,
    VERIFY_SHELL,
    VERIFY_SHELL_CONFIGMAP,
    VERIFY_SHELL_DIR,
    VERIFY_WORK_DIR,
)
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.conventions.schedule import ETCD_RESTORE_VERIFY, ETCD_SNAPSHOT
from kluster.scripts.credentials import age

CRONJOB = 'kubernetes:batch/v1:CronJob'
CONFIGMAP = 'kubernetes:core/v1:ConfigMap'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
COMPONENT = 'kluster:backup:EtcdRestoreVerify'

READER = conventions.sealed.ETCD_READER_KEY
IDENTITY = conventions.sealed.ETCD_DRILL_IDENTITY

#: The pin the CronJob's image is configured from, the `etcd-age` image.
PIN = 'etcd-verify'
#: Where the image keeps the binaries it adds to curl's (`docker/etcd-age.Containerfile`).
BINARY_DIR = '/usr/local/bin'
BINARIES = ('etcd', 'etcdctl', 'etcdutl', 'age')
#: The shell the container's command names, the image's busybox.
SHELL = '/bin/sh'

#: The bound on one run of the script, which restores a small snapshot and
#: starts one etcd: a stop-loss under the case bound, which nothing asserts on.
RUN_TIMEOUT = 45
#: The bound on each other command a case runs, every one local.
COMMAND_TIMEOUT = 15
#: How long the fixtures' etcd may take to answer healthy: a stop-loss.
START_TIMEOUT = 20.0

#: How far inside the window a snapshot the cases call current is stamped,
#: and how far outside it a stale one is: far beyond the case bound, so the
#: seconds between the stamp and the run never cross the window's edge.
MARGIN = dt.timedelta(minutes=10)


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The stack as its file holds it: every sealed value it declares, sealed in."""
    return await applied()


def _named(run: Run, typ: str, name: str) -> list[dict[str, Any]]:
    return [it.inputs for it in run.monitor.of_type(typ) if it.inputs.get('metadata', {}).get('name') == name]


def _pod(run: Run, cronjob: str = ETCD_RESTORE_VERIFY.cronjob) -> dict[str, Any]:
    (declared,) = _named(run, CRONJOB, cronjob)
    return declared['spec']['jobTemplate']['spec']['template']['spec']


def _container(run: Run) -> dict[str, Any]:
    (container,) = _pod(run)['containers']
    return container


def _environment(container: Mapping[str, Any]) -> dict[str, Any]:
    return {it['name']: it.get('value', it.get('valueFrom')) for it in container['env']}


def _volume(pod: Mapping[str, Any], container: Mapping[str, Any], path: str) -> dict[str, Any]:
    """The volume `container` mounts at `path`."""
    (mount,) = [it for it in container['volumeMounts'] if it['mountPath'] == path]
    (volume,) = [it for it in pod['volumes'] if it['name'] == mount['name']]
    return volume


def _mib(quantity: str) -> int:
    assert quantity.endswith('Mi'), quantity
    return int(quantity.removesuffix('Mi'))


# -- The declaration -----------------------------------------------------------------------


def test_the_pod_reads_the_reader_key_from_the_secret_its_sealed_secret_becomes(applied: Run) -> None:
    (key,) = _named(applied, SEALED_SECRET, READER.name)
    environment = _environment(_container(applied))
    secret = key['spec']['template']['metadata']['name']

    assert key['metadata']['namespace'] == ETCD_RESTORE_VERIFY.namespace
    assert set(key['spec']['encryptedData']) == conventions.WRITER_KEY_FIELDS
    for field in conventions.WRITER_KEY_FIELDS:
        assert environment[field] == {'secretKeyRef': {'name': secret, 'key': field}}, field


def test_the_identity_the_script_reads_is_the_key_of_the_secret_its_sealed_secret_becomes(applied: Run) -> None:
    (sealed_identity,) = _named(applied, SEALED_SECRET, IDENTITY.name)
    pod = _pod(applied)
    container = _container(applied)
    path = _environment(container)['IDENTITY']
    directory, _, key = path.rpartition('/')
    volume = _volume(pod, container, directory)

    assert sealed_identity['metadata']['namespace'] == ETCD_RESTORE_VERIFY.namespace
    assert volume['secret']['secretName'] == sealed_identity['spec']['template']['metadata']['name']
    assert key in sealed_identity['spec']['encryptedData']


def test_the_script_and_its_bucket_client_are_the_config_map_the_command_runs_from(applied: Run) -> None:
    pod = _pod(applied)
    container = _container(applied)
    (shell,) = _named(applied, CONFIGMAP, VERIFY_SHELL_CONFIGMAP)
    script = container['command'][-1]
    directory, _, name = script.rpartition('/')
    volume = _volume(pod, container, directory)

    assert volume['configMap']['name'] == shell['metadata']['name']
    assert shell['metadata']['namespace'] == ETCD_RESTORE_VERIFY.namespace
    assert name in shell['data']
    # The script sources its bucket client from its own directory.
    assert '. "${0%/*}/b2.sh"' in shell['data'][name]
    assert 'b2.sh' in shell['data']


def test_the_pod_holds_no_kubernetes_or_talos_credential(applied: Run) -> None:
    """It speaks B2 and its own loopback: no service account token, and nothing of Talos' Secret."""
    pod = _pod(applied)
    secrets = {it['secret']['secretName'] for it in pod['volumes'] if 'secret' in it}

    assert pod['automountServiceAccountToken'] is False
    assert secrets == {IDENTITY.name}


def test_the_verify_runs_off_the_control_planes(applied: Run) -> None:
    """On the worker today, never on a node etcd's own memory budget is kept on."""
    required = _pod(applied)['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
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

    assert {node for node, labels in nodes.items() if admits(labels)} == {conventions.HOMELAB_NODE}


def test_the_working_directory_is_in_memory_and_fills_before_the_pods_memory_does(applied: Run) -> None:
    """The plaintext keyspace never reaches a disk, and a database that outgrows the directory fails naming the directory.

    A memory-backed directory is charged to the pod, so a limit at or under
    its bound would end an outgrown run as an out-of-memory kill, which names
    nothing, rather than as `no space left on device`.
    """
    pod = _pod(applied)
    container = _container(applied)
    work = _volume(pod, container, _environment(container)['WORK'])['emptyDir']
    resources = container['resources']

    assert work['medium'] == 'Memory'
    assert _mib(work['sizeLimit']) < _mib(resources['limits']['memory'])
    assert _mib(resources['requests']['memory']) <= _mib(resources['limits']['memory'])


# -- The script, run -----------------------------------------------------------------------


@dataclass(frozen=True)
class Image:
    """The image's busybox applets, and a directory of its own binaries."""

    applets: Path
    binaries: Path

    def binary(self, name: str) -> str:
        return str(self.binaries / name)


@pytest.fixture(scope='module', name='image')
def image_fixture(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Image:
    cache = image_busybox.cache_directory(request.config, tmp_path_factory)
    pin = image_busybox.pinned(PIN)
    applets = image_busybox.applets(image_busybox.extracted(pin, cache), tmp_path_factory.mktemp('busybox'))
    binaries = tmp_path_factory.mktemp('binaries')
    for name in BINARIES:
        (binaries / name).symlink_to(image_busybox.copied(pin, f'{BINARY_DIR}/{name}', cache))
    return Image(applets, binaries)


def _free_ports(count: int) -> list[int]:
    """Ports nothing on the host's loopback holds, each asked of the kernel."""
    held: list[socket.socket] = []
    try:
        for _ in range(count):
            bound = socket.socket()
            bound.bind(('127.0.0.1', 0))
            held.append(bound)
        return [it.getsockname()[1] for it in held]
    finally:
        for it in held:
            it.close()


def _wait_healthy(image: Image, endpoint: str) -> None:
    """Until the etcd at `endpoint` answers healthy, with a stop-loss bound."""
    bound = time.monotonic() + START_TIMEOUT
    while (
        process_sessions.run(
            [image.binary('etcdctl'), '--endpoints', endpoint, 'endpoint', 'health'], timeout=COMMAND_TIMEOUT
        ).returncode
        != 0
    ):
        assert time.monotonic() < bound, f'the fixture etcd at {endpoint} did not answer healthy'
        time.sleep(0.1)


def _snapshot(image: Image, into: Path, key: str) -> Path:
    """A snapshot of a fresh etcd holding `key`, saved as `etcdctl` streams a member's, its hash at the end."""
    client, peer = (f'http://127.0.0.1:{port}' for port in _free_ports(2))
    data, saved = into / 'data', into / 'snapshot.db'
    etcdctl = [image.binary('etcdctl'), '--endpoints', client]
    with process_sessions.started(
        [
            image.binary('etcd'),
            '--name=fixture',
            f'--data-dir={data}',
            f'--listen-client-urls={client}',
            f'--advertise-client-urls={client}',
            f'--listen-peer-urls={peer}',
            f'--initial-advertise-peer-urls={peer}',
            f'--initial-cluster=fixture={peer}',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ):
        _wait_healthy(image, client)
        _ = process_sessions.run([*etcdctl, 'put', key, 'a value'], timeout=COMMAND_TIMEOUT, check=True)
        _ = process_sessions.run([*etcdctl, 'snapshot', 'save', str(saved)], timeout=COMMAND_TIMEOUT, check=True)
    shutil.rmtree(data)
    return saved


@pytest.fixture(scope='module', name='snapshots')
def snapshots_fixture(image: Image, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """`whole`, a snapshot holding `VERIFY_KEY`; `keyless`, one holding another key alone; `corrupt`, `whole` with a byte flipped."""
    whole = _snapshot(image, tmp_path_factory.mktemp('whole'), VERIFY_KEY)
    keyless = _snapshot(image, tmp_path_factory.mktemp('keyless'), '/registry/namespaces/default')
    corrupt = tmp_path_factory.mktemp('corrupt') / 'snapshot.db'
    data = bytearray(whole.read_bytes())
    data[len(data) // 2] ^= 0xFF
    _ = corrupt.write_bytes(bytes(data))
    return {'whole': whole, 'keyless': keyless, 'corrupt': corrupt}


@pytest.fixture(scope='module', name='identities')
def identities_fixture() -> tuple[age.Identity, age.Identity]:
    """The verify's identity, and another one a snapshot can be encrypted to instead."""
    return age.generate(), age.generate()


class Verify:
    """The declared container's run, mapped onto the case's directories, against the fake bucket."""

    def __init__(
        self, run: Run, image: Image, tmp_path: Path, identity: age.Identity, *, buckets: Sequence[Mapping[str, Any]]
    ) -> None:
        container = _container(run)
        self.container = container
        self.image = image
        self.state, self.fakes = tmp_path / 'state', tmp_path / 'bin'
        self.bucket = self.state / 'bucket'
        shell, identities, self.work = tmp_path / 'shell', tmp_path / 'identity', tmp_path / 'work'
        for directory in (shell, identities, self.work):
            directory.mkdir()
        for name, text in VERIFY_SHELL.items():
            _ = (shell / name).write_text(text)
        fake_b2_shell.install(self.state, self.fakes, buckets)
        self.environment = _environment(container)
        _, _, key = self.environment['IDENTITY'].rpartition('/')
        _ = (identities / key).write_text(f'{identity.secret}\n')
        self.local = {
            VERIFY_SHELL_DIR: str(shell),
            VERIFY_IDENTITY_DIR: str(identities),
            VERIFY_WORK_DIR: str(self.work),
        }
        snapshot_job = _environment(_pod(run, ETCD_SNAPSHOT.cronjob)['containers'][0])
        #: How the snapshot job names an object: `<S3_PREFIX>/<CLUSTER_NAME>-<RFC 3339 UTC>.snap.age`
        #: (`pkg/s3/s3.go`, `pkg/talos/talos.go` at talos-backup v0.1.0).
        self.object_stem = f'{snapshot_job["S3_PREFIX"]}/{snapshot_job["CLUSTER_NAME"]}-'
        self.max_age = dt.timedelta(seconds=int(self.environment['MAX_AGE']))
        self.key = ':'.join(FAKE_KEY)

    def here(self, value: str) -> str:
        """`value` with each path of the pod written as the case's directory for it."""
        for remote, path in self.local.items():
            value = value.replace(remote, path)
        return value

    def upload(self, snapshot: Path, taken: dt.datetime, recipients: Sequence[str]) -> str:
        """`snapshot`, encrypted by the image's `age` to `recipients`, as the object of a snapshot taken at `taken`."""
        name = f'{self.object_stem}{taken.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}.snap.age'
        return self.put(name, snapshot, recipients)

    def put(self, name: str, snapshot: Path, recipients: Sequence[str]) -> str:
        target = self.bucket / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = process_sessions.run(
            [
                self.image.binary('age'),
                *(arg for recipient in recipients for arg in ('-r', recipient)),
                '-o',
                str(target),
                str(snapshot),
            ],
            timeout=COMMAND_TIMEOUT,
            check=True,
        )
        return name

    def run(self) -> subprocess.CompletedProcess[str]:
        client, peer = _free_ports(2)
        halves = dict(zip(sorted(conventions.WRITER_KEY_FIELDS), FAKE_KEY, strict=True))
        environment = {
            'PATH': ':'.join(str(it) for it in (self.fakes, self.image.binaries, self.image.applets)),
            **fake_b2_shell.fake_env(self.state, self.key),
            **{
                it['name']: self.here(it['value']) if 'value' in it else halves[it['name']]
                for it in self.container['env']
            },
            'CLIENT_PORT': str(client),
            'PEER_PORT': str(peer),
        }
        argv = [str(self.image.applets / 'sh') if arg == SHELL else self.here(arg) for arg in self.container['command']]
        return process_sessions.run(argv, env=environment, text=True, timeout=RUN_TIMEOUT)

    def calls(self) -> list[str]:
        """The address of each call `curl` was run with, in order."""
        found = self.state / 'curl.argv'
        return [json.loads(line)[-1] for line in found.read_text().splitlines()] if found.exists() else []


@pytest.fixture(name='verify')
def verify_fixture(
    applied: Run, image: Image, tmp_path: Path, identities: tuple[age.Identity, age.Identity]
) -> Iterator[Verify]:
    yield Verify(applied, image, tmp_path, identities[0], buckets=[BUCKET])


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _ok(run: subprocess.CompletedProcess[str], name: str) -> None:
    assert run.returncode == 0, run.stderr
    (last,) = run.stdout.splitlines()[-1:]
    assert last.startswith('ok: ') and name in last, run.stdout


def _failed(run: subprocess.CompletedProcess[str], step: str, *named: str) -> str:
    """The `FAILED <step>` line of a run that failed there, which names each of `named`."""
    assert run.returncode != 0, run.stdout
    lines = [it for it in run.stderr.splitlines() if it.startswith('FAILED ')]
    assert lines, run.stderr
    (line,) = lines
    assert line.startswith(f'FAILED {step}: '), line
    for it in named:
        assert it in line, line
    return line


def test_the_script_runs_under_the_busybox_and_binaries_of_its_containers_own_image(image: Image) -> None:
    """The files the suite runs are the image's own, byte for byte, by the sums the image computes itself."""
    pin = image_busybox.pinned(PIN)
    *loader, binary = image_busybox.launched(image.applets / 'sh')
    interpreter = image_busybox.interpreter(binary.read_bytes())
    named = {
        image_busybox.BINARY: binary,
        **({interpreter: loader[0]} if interpreter is not None and loader else {}),
        **{f'{BINARY_DIR}/{it}': (image.binaries / it).resolve() for it in BINARIES},
    }

    in_image = image_busybox.sums_in(pin, *named)

    assert {path: image_busybox.sha256(local) for path, local in named.items()} == in_image


def test_the_newest_snapshot_restores_and_the_line_names_it(
    verify: Verify, snapshots: Mapping[str, Path], identities: tuple[age.Identity, age.Identity]
) -> None:
    """Listed under the window, downloaded by its id from the download address, decrypted, restored and served."""
    name = verify.upload(snapshots['whole'], _now() - dt.timedelta(minutes=35), [identities[0].public])

    run = verify.run()

    _ok(run, name)
    storage = AUTHORIZATION_V4['apiInfo']['storageApi']
    assert verify.calls()[-1] == f'{storage["downloadUrl"]}/b2api/v4/b2_download_file_by_id'


@pytest.mark.parametrize('corrupt', ['newest', 'older'])
def test_the_newest_snapshot_is_the_one_read(
    verify: Verify, snapshots: Mapping[str, Path], identities: tuple[age.Identity, age.Identity], corrupt: str
) -> None:
    """A corrupt newest fails at the restore whatever is older, and a corrupt older one is never read.

    The corrupt snapshot has one byte flipped before it was encrypted, so it
    decrypts and only etcd's own hash refuses it: the first case is what
    proves that check bites.
    """
    recipient = [identities[0].public]
    older = verify.upload(
        snapshots['corrupt' if corrupt == 'older' else 'whole'], _now() - dt.timedelta(minutes=65), recipient
    )
    newest = verify.upload(
        snapshots['corrupt' if corrupt == 'newest' else 'whole'], _now() - dt.timedelta(minutes=5), recipient
    )

    run = verify.run()

    if corrupt == 'newest':
        _failed(run, 'restore', newest)
    else:
        _ok(run, newest)
        assert older not in run.stdout


def test_a_snapshot_not_encrypted_to_the_verifys_recipient_fails_naming_the_identity(
    verify: Verify, snapshots: Mapping[str, Path], identities: tuple[age.Identity, age.Identity]
) -> None:
    name = verify.upload(snapshots['whole'], _now() - dt.timedelta(minutes=5), [identities[1].public])

    run = verify.run()

    _failed(run, 'decrypt', name, verify.here(verify.environment['IDENTITY']))


@pytest.mark.parametrize('held', ['older than the window', 'nothing'])
def test_no_snapshot_inside_the_window_is_stale(
    verify: Verify, snapshots: Mapping[str, Path], identities: tuple[age.Identity, age.Identity], held: str
) -> None:
    """The newest snapshot older than the snapshot job's own `max_age`, or none at all: stale, either way."""
    if held != 'nothing':
        _ = verify.upload(snapshots['whole'], _now() - verify.max_age - MARGIN, [identities[0].public])

    run = verify.run()

    _failed(run, 'stale', f'{verify.object_stem.rpartition("/")[0]}/')


def test_a_snapshot_just_inside_the_window_passes(
    verify: Verify, snapshots: Mapping[str, Path], identities: tuple[age.Identity, age.Identity]
) -> None:
    name = verify.upload(snapshots['whole'], _now() - verify.max_age + MARGIN, [identities[0].public])

    _ok(verify.run(), name)


def test_a_snapshot_without_the_key_fails_naming_the_key(
    verify: Verify, snapshots: Mapping[str, Path], identities: tuple[age.Identity, age.Identity]
) -> None:
    """A snapshot of a member that holds no Kubernetes state restores and serves, and is still no recovery."""
    name = verify.upload(snapshots['keyless'], _now() - dt.timedelta(minutes=5), [identities[0].public])

    run = verify.run()

    _failed(run, 'key', name, VERIFY_KEY)


def test_a_name_of_another_form_inside_the_window_fails_naming_it(
    verify: Verify, snapshots: Mapping[str, Path], identities: tuple[age.Identity, age.Identity]
) -> None:
    """talos-backup naming its objects another way is a change the restore runbook reads the name by too."""
    _ = verify.upload(snapshots['whole'], _now() - dt.timedelta(minutes=30), [identities[0].public])
    stamp = (_now() - dt.timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
    odd = verify.put(f'{verify.object_stem}{stamp}.snap', snapshots['whole'], [identities[0].public])

    run = verify.run()

    _failed(run, 'name', odd)


def test_a_key_b2_refuses_fails_at_the_authorization(
    verify: Verify, snapshots: Mapping[str, Path], identities: tuple[age.Identity, age.Identity]
) -> None:
    _ = verify.upload(snapshots['whole'], _now() - dt.timedelta(minutes=5), [identities[0].public])
    verify.key = 'key-id:another-secret'

    run = verify.run()

    _failed(run, 'authorize', 'reader key')


def test_a_bucket_b2_names_null_still_downloads_by_its_id(
    applied: Run,
    image: Image,
    tmp_path: Path,
    snapshots: Mapping[str, Path],
    identities: tuple[age.Identity, age.Identity],
) -> None:
    """B2's own example answers a bucket's name as `null`, and the verify needs only its id."""
    verify = Verify(applied, image, tmp_path, identities[0], buckets=[{'id': BUCKET['id'], 'name': None}])
    name = verify.upload(snapshots['whole'], _now() - dt.timedelta(minutes=5), [identities[0].public])

    _ok(verify.run(), name)


def test_a_paged_listing_takes_the_newest_from_its_last_page(
    verify: Verify, snapshots: Mapping[str, Path], identities: tuple[age.Identity, age.Identity]
) -> None:
    """B2 answers a page at a time, and the names sort oldest first, so the first page holds the oldest."""
    recipient = [identities[0].public]
    for minutes in (50, 40, 30):
        _ = verify.upload(snapshots['corrupt'], _now() - dt.timedelta(minutes=minutes), recipient)
    newest = verify.upload(snapshots['whole'], _now() - dt.timedelta(minutes=20), recipient)
    _ = (verify.state / 'page-size').write_text('1')

    _ok(verify.run(), newest)
