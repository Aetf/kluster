"""The control plane's API server, in containers, for the suites that have it judge or mutate a write: `running`.

An admission policy is evaluated by the API server the cluster runs: the
`kube-apiserver` and `etcd` images the pinned Talos release names by default
(`talosctl images default`), which is what the cluster's control plane runs.
Each image of the control plane run here, those two and the
`kube-controller-manager` below, is pinned by the tag the release names and
that tag's digest (`CONTROL_PLANE_PINS`), and pulled by the digest
(`tests/pinned_image.py`); `tests/test_apps_database.py` holds the tags to
the release's, so a Talos bump onto another Kubernetes is red until the pins
move with it.

The server and `etcd` share one network namespace of their own and publish
nothing, so the client runs inside it too, the way a suite hands it
(`Client`): a container of its own from an image that carries `kubectl`
(`BesideServer`), or a static `kubectl` copied into the server's container
(`InServer`). The server admits every request its authorizer is asked about,
so what a case reads is admission alone; a request is sent as an account by
impersonating it, and a write is sent with `--dry-run=server`, which
admission judges and mutates as it does the write itself.

**A suite that has a controller act on what it stores** runs the
controller beside the server, in that network namespace, as the server's
`ADMIN` through a kubeconfig (`ApiServer.alongside`): the suite's own image
and arguments, for an operator a suite pins itself. The workload
controllers are the release's `kube-controller-manager`, pulled and run
only for a suite that names the controllers it runs (`running`'s
`controllers`), today the tripwire of `tests/test_operator_updates.py`. No
node registers and no scheduler runs, so a workload of zero replicas is the
one a controller reports ready.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pinned_image
import process_sessions
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from kluster.lib.versions import ImagePin

#: The control plane's images, each the tag the pinned Talos release names
#: and the digest the registry serves for it, by the name the release lists
#: it under.
CONTROL_PLANE_PINS = {
    'kube-apiserver': ImagePin(
        'registry.k8s.io/kube-apiserver',
        'v1.36.3',
        'sha256:b4bc06c81fd76f81174e6c19ddacf477acdf1583e7a5846ebbd513493aef6e43',
    ),
    'etcd': ImagePin(
        'registry.k8s.io/etcd', 'v3.6.12', 'sha256:3c2ced08f23b1183e8bd4613064c3fb6b8db5057a4d1f13c3518c76e357a07a8'
    ),
    'kube-controller-manager': ImagePin(
        'registry.k8s.io/kube-controller-manager',
        'v1.36.3',
        'sha256:ed56454bf514916079a227f5765b64524fde52106dfcc52978b28634765b78b8',
    ),
}
#: The images every server runs; the controller manager runs only where a
#: suite names controllers.
_SERVER_IMAGES = ('kube-apiserver', 'etcd')
#: The server's port, inside the network namespace the containers share.
SERVER_PORT = 6443
#: The user the server's one token authenticates, who sets the server up.
ADMIN = 'admin'
#: The bound on one command a case sends the server or podman.
COMMAND_TIMEOUT = 30
#: A stop-loss, which nothing asserts on: the waits for the server to answer
#: and for a policy to take effect.
READY_TIMEOUT = 60
#: The bound on a case that reads the server, past the suite's own: the first
#: case a worker runs starts the server, behind the pulls of its two images.
CASE_TIMEOUT = 2 * pinned_image.PULL_TIMEOUT + 2 * READY_TIMEOUT

#: Where `InServer` copies `kubectl` to in the server's container, and the
#: directory it caches discovery in, on the container's writable mount.
_IN_SERVER_KUBECTL = '/kubectl'
_IN_SERVER_CACHE = '/certs/kubectl-cache'
#: Where a container beside the server reads its kubeconfig from, named by
#: `KUBECONFIG` in its environment and by the controller manager's flag.
_KUBECONFIG = '/kubeconfig'


@dataclass(frozen=True)
class BesideServer:
    """The client as a container of its own, from `image`, whose `kubectl` is on its `PATH`, beside the server."""

    image: str
    """The reference the image is run by, pulled already."""


@dataclass(frozen=True)
class InServer:
    """The client as a static `kubectl` at `binary`, copied into the server's container and run there."""

    binary: str


Client = BesideServer | InServer


def podman(*args: str) -> str:
    return process_sessions.run(['podman', *args], text=True, timeout=COMMAND_TIMEOUT, check=True).stdout


def until(probe: Callable[[], bool], what: str) -> None:
    """Return once `probe` holds, within `READY_TIMEOUT`, a stop-loss."""
    bound = time.monotonic() + READY_TIMEOUT
    while not probe():
        assert time.monotonic() < bound, f'{what} within {READY_TIMEOUT}s'
        time.sleep(0.2)


def typed(api_version: str, kind: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """A declaration's inputs as the object the server takes: with its `apiVersion` and `kind`."""
    return {'apiVersion': api_version, 'kind': kind, **inputs}


@dataclass(frozen=True)
class ApiServer:
    """The control plane's API server, reached through `kubectl` as `kubectl` runs it."""

    kubectl_argv: tuple[str, ...]
    """The command that runs `kubectl` inside the server's network namespace, before its own arguments."""
    token: str
    label: str
    """The label every container beside the server carries."""
    network: str
    """The `--network` a container beside the server joins: the server's namespace."""
    kubeconfig: Path
    """A kubeconfig that reaches the server as `ADMIN` from inside its network namespace."""

    @contextmanager
    def alongside(self, role: str, image: str, *args: str, env: Mapping[str, str] | None = None) -> Generator[str]:
        """A container from `image` running `args`, beside the server as `ADMIN`, for as long as the block lasts.

        Its name, which is what the block yields, carries `role`. The
        kubeconfig is at `_KUBECONFIG`, which `KUBECONFIG` names, in an
        environment that also holds `env`. The container carries the
        server's label and the bound every container here does, and is
        removed after the block, before the server's own.
        """
        name = f'kluster-test-{role}-{uuid.uuid4().hex[:12]}'
        environment = {'KUBECONFIG': _KUBECONFIG, **(env or {})}
        with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
            _ = podman(
                'create', '--name', name, *_common(self.label), '--network', self.network,
                *(f'--env={key}={value}' for key, value in environment.items()),
                image, *args,
            )  # fmt: skip
            _ = podman('cp', str(self.kubeconfig), f'{name}:{_KUBECONFIG}')
            _ = podman('start', name)
            yield name

    def kubectl(
        self, *args: str, user: str | None = None, document: Mapping[str, Any] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """`kubectl` with `args`, as `user` where one is named and as `ADMIN` otherwise, reading `document`."""
        return process_sessions.run(
            [
                *self.kubectl_argv,
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

    def written(
        self, verb: str, document: Mapping[str, Any], user: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """`document` sent by `user` through `verb`, a `create` or a `replace`, as admission leaves it, and nothing stored.

        Its output is the object admission let through, as JSON.
        """
        return self.kubectl(verb, '--dry-run=server', '-o', 'json', '-f', '-', user=user, document=document)

    def created(self, document: Mapping[str, Any], user: str) -> subprocess.CompletedProcess[str]:
        """`document` created by `user`, as admission judges it, and nothing stored."""
        return self.written('create', document, user)


def _common(label: str) -> list[str]:
    """What every container here is started with: removed once it stops, labeled `label`, bounded, logging to a file."""
    return ['--rm', '--label', label, '--timeout', str(pinned_image.CONTAINER_TIMEOUT), process_sessions.LOG_TO_FILE]


def _kubeconfig(token: str) -> dict[str, object]:
    """A kubeconfig that reaches the server as the user `token` authenticates, from inside its network namespace."""
    return {
        'apiVersion': 'v1',
        'kind': 'Config',
        'clusters': [
            {
                'name': 'server',
                'cluster': {'server': f'https://127.0.0.1:{SERVER_PORT}', 'insecure-skip-tls-verify': True},
            }
        ],
        'users': [{'name': ADMIN, 'user': {'token': token}}],
        'contexts': [{'name': ADMIN, 'context': {'cluster': 'server', 'user': ADMIN}}],
        'current-context': ADMIN,
    }


@contextmanager
def running(label: str, files: Path, client: Client, *, controllers: tuple[str, ...] = ()) -> Generator[ApiServer]:
    """The server, answering, with `client` beside it, for as long as the block lasts; every container is removed after.

    Each container carries `label`, the census of what a run left, and is
    bounded by `pinned_image.CONTAINER_TIMEOUT`. `files` is a directory of the
    caller's, which takes the server's service-account key, token file and
    kubeconfig. Where `controllers` names any, the controller manager runs
    those controllers beside the server, as `ADMIN`, with no leader election
    and nothing served.
    """
    names = (*_SERVER_IMAGES, *(('kube-controller-manager',) if controllers else ()))
    images = {name: pinned_image.pulled(CONTROL_PLANE_PINS[name]) for name in names}
    key = ec.generate_private_key(ec.SECP256R1())
    _ = (files / 'sa.key').write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    _ = (files / 'sa.pub').write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    token = uuid.uuid4().hex
    _ = (files / 'tokens.csv').write_text(f'{token},{ADMIN},{ADMIN},"system:masters"\n')
    kubeconfig = files / 'kubeconfig'
    _ = kubeconfig.write_text(json.dumps(_kubeconfig(token)))
    suffix = uuid.uuid4().hex[:12]
    etcd, server, beside = (f'kluster-test-{role}-{suffix}' for role in ('etcd', 'apiserver', 'client'))
    common = _common(label)
    shared = ['--network', f'container:{etcd}']
    # The two beside etcd go first: podman refuses to remove a container whose network another shares.
    with process_sessions.removing(beside, server, etcd, timeout=COMMAND_TIMEOUT):
        _ = podman(
            'run', '--detach', '--name', etcd, *common, '--network', 'none', '--tmpfs', '/data',
            images['etcd'], 'etcd', '--data-dir=/data',
            '--listen-client-urls=http://127.0.0.1:2379', '--advertise-client-urls=http://127.0.0.1:2379',
            '--listen-peer-urls=http://127.0.0.1:2380',
        )  # fmt: skip
        _ = podman(
            'create', '--name', server, *common, *shared, '--tmpfs', '/certs',
            images['kube-apiserver'], 'kube-apiserver',
            '--etcd-servers=http://127.0.0.1:2379', f'--secure-port={SERVER_PORT}',
            '--bind-address=127.0.0.1', '--advertise-address=127.0.0.1', '--cert-dir=/certs',
            '--service-cluster-ip-range=10.0.0.0/24', '--service-account-issuer=https://kubernetes.default.svc',
            '--service-account-key-file=/sa.pub', '--service-account-signing-key-file=/sa.key',
            '--token-auth-file=/tokens.csv', '--authorization-mode=AlwaysAllow',
        )  # fmt: skip
        for name in ('sa.key', 'sa.pub', 'tokens.csv'):
            _ = podman('cp', str(files / name), f'{server}:/{name}')
        match client:
            case InServer(binary):
                _ = podman('cp', binary, f'{server}:{_IN_SERVER_KUBECTL}')
                argv = (
                    'podman',
                    'exec',
                    '--interactive',
                    server,
                    _IN_SERVER_KUBECTL,
                    f'--cache-dir={_IN_SERVER_CACHE}',
                )
                _ = podman('start', server)
            case BesideServer(image):
                _ = podman('start', server)
                _ = podman('run', '--detach', '--name', beside, *common, *shared, '--entrypoint', 'sleep', image,
                           str(pinned_image.CONTAINER_TIMEOUT))  # fmt: skip
                argv = ('podman', 'exec', '--interactive', beside, 'kubectl')
        apiserver = ApiServer(argv, token, label, f'container:{etcd}', kubeconfig)
        until(lambda: apiserver.kubectl('get', '--raw', '/readyz').returncode == 0, 'the API server did not answer')
        if not controllers:
            yield apiserver
            return
        with apiserver.alongside(
            'controller-manager', images['kube-controller-manager'], 'kube-controller-manager',
            f'--kubeconfig={_KUBECONFIG}', f'--controllers={",".join(controllers)}', '--leader-elect=false',
            '--secure-port=0',
        ):  # fmt: skip
            yield apiserver
