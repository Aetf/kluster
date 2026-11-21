"""How `perf` reaches the cluster: Talos for a node's files, the API server for etcd and the history store, `physical` for the nodes.

Each through the pinned client the operator uses by hand
(physical/gateway-cutover.md §8.1): `talosctl` under the operator's
talosconfig, whose one endpoint is the balancer and which names a control
plane by its VCN address;
`kubectl` under the cluster-admin kubeconfig, through the API server's node
proxy to etcd's metrics listener and its service proxy to the history store;
and the `pulumi` CLI for `physical`'s `node_private_ips`, under `physical`'s
own passphrase (credentials.md §4.4), in the environment `ci-stack` gives a
run of that stack (`manual_run.Environments`) and from a working copy that
holds the forge's `main` (`manual_run.require_main`). Every call is bounded,
so a node that does not answer costs a round its bound and nothing more.
"""

from __future__ import annotations

import json
import os
import subprocess as sp
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kluster import conventions
from kluster.lib import pulumi_cli, stack_addresses, stack_environment
from kluster.lib.monitoring.vmsingle import VMSINGLE_PORT, vmsingle_workload
from kluster.scripts import manual_run
from kluster.scripts.credentials import oci_recovery

__all__ = (
    'CALL_TIMEOUT',
    'KUBECONFIG',
    'TALOSCONFIG',
    'Api',
    'Kubectl',
    'Node',
    'Run',
    'Talos',
    'Talosctl',
    'Unanswered',
    'control_planes',
    'etcd_metrics',
    'history',
    'run_command',
)

#: Where the operator keeps the cluster's client configurations
#: (physical/gateway-cutover.md §8.1).
KUBECONFIG = Path('~/.kube/kluster')
TALOSCONFIG = Path('~/.talos/kluster')

#: The bound of one call to the cluster, past which the call is stopped and
#: counts as unanswered. A stop-loss: a read through Talos or the API server
#: answers in well under a second.
CALL_TIMEOUT = 10.0


class Unanswered(RuntimeError):
    """A call to the cluster failed, or did not finish within its bound."""


class Run(Protocol):
    """How a client is run: its arguments and standard input, its standard output back. Substituted in tests."""

    def __call__(self, argv: Sequence[str], *, stdin: bytes | None, timeout: float) -> bytes: ...


def run_command(argv: Sequence[str], *, stdin: bytes | None, timeout: float) -> bytes:
    """Run `argv`, returning its standard output; `Unanswered` with its last line of error where it fails or overruns.

    In a session of its own, so the Ctrl-C that stops a recording reaches the
    recorder alone, and the reads under way answer before it stops.
    """
    try:
        done = sp.run(argv, input=stdin, capture_output=True, timeout=timeout, check=False, start_new_session=True)
    except sp.TimeoutExpired as exc:
        raise Unanswered(f'`{argv[0]}` did not answer within {timeout:g}s and was stopped') from exc
    if done.returncode != 0:
        said = done.stderr.decode(errors='replace').strip().splitlines()
        raise Unanswered(f'`{argv[0]}` exited {done.returncode}: {said[-1] if said else "with nothing on stderr"}')
    return done.stdout


class Talos(Protocol):
    """A node's files, read through Talos."""

    def read(self, address: str, path: str) -> str: ...


class Api(Protocol):
    """The API server, by the path of a request."""

    def get(self, path: str) -> bytes: ...

    def post(self, path: str, body: bytes) -> None: ...


@dataclass(frozen=True)
class Talosctl:
    """`talosctl read` under a talosconfig."""

    talosconfig: Path
    run: Run = run_command

    def read(self, address: str, path: str) -> str:
        argv = ['talosctl', '--talosconfig', str(self.talosconfig), '-n', address, 'read', path]
        return self.run(argv, stdin=None, timeout=CALL_TIMEOUT).decode()


@dataclass(frozen=True)
class Kubectl:
    """`kubectl`'s raw requests under a kubeconfig: `get --raw`, and `create --raw`, which posts its file."""

    kubeconfig: Path
    run: Run = run_command
    timeout: float = CALL_TIMEOUT

    def get(self, path: str) -> bytes:
        return self.run(
            ['kubectl', '--kubeconfig', str(self.kubeconfig), 'get', '--raw', path], stdin=None, timeout=self.timeout
        )

    def post(self, path: str, body: bytes) -> None:
        argv = ['kubectl', '--kubeconfig', str(self.kubeconfig), 'create', '--raw', path, '-f', '-']
        _ = self.run(argv, stdin=body, timeout=self.timeout)


def etcd_metrics(node: str) -> str:
    """The path of `node`'s etcd metrics, through the API server's node proxy."""
    return f'/api/v1/nodes/http:{node}:{conventions.ETCD_METRICS_PORT}/proxy/metrics'


def history(path: str) -> str:
    """The path of `path` on the history store, through the API server's service proxy."""
    service = f'{vmsingle_workload(conventions.HISTORY_STORE)}:{VMSINGLE_PORT}'
    return f'/api/v1/namespaces/{conventions.MONITORING_NAMESPACE}/services/{service}/proxy{path}'


@dataclass(frozen=True)
class Node:
    """A control plane: its name, the `node` its series carry, and the address Talos reaches it at."""

    name: str
    address: str


def control_planes(
    run: pulumi_cli.Runner = pulumi_cli.run_pulumi,
    *,
    physical: Callable[[], str] = oci_recovery.slot_passphrase,
    require_main: Callable[[Path], None] = manual_run.require_main,
) -> list[Node]:
    """The control planes, from `physical`'s `node_private_ips`, read under `physical`'s passphrase.

    The run's environment is the one every manual run of a stack CI deploys
    is given (`manual_run.Environments`), as `ci-stack physical` gives it:
    `physical`'s passphrase from `physical`, its workstation slot, in place of
    the ambient stack passphrase, the estate's backend, and CI's Helm
    environment. It is the run's whole environment
    (`pulumi_cli.WholeEnvironment`): one laid over this process's would bring
    a passphrase file of the caller's back. It starts from a working copy that
    holds the forge's `main` (`require_main`), refused as
    `manual_run.CheckRefused` otherwise, since the rule for a manual run of a
    stack CI deploys covers reads (framework/pulumi.md §3.4).
    """
    output = conventions.PHYSICAL_OUTPUTS.node_private_ips
    checkout = pulumi_cli.project_dir()
    with manual_run.helm_homes(checkout, prefix='perf-helm-') as helm:
        environments = manual_run.Environments(base=os.environ, helm=helm, physical=physical)
        env = pulumi_cli.WholeEnvironment(environments.pulumi(stack_environment.PHYSICAL_STACK))
        require_main(checkout)
        printed = run(
            ['stack', 'output', output, '--json', '--stack', stack_environment.PHYSICAL_STACK],
            cwd=checkout,
            env=env,
            stdin=None,
        )
    addresses = stack_addresses.usable_addresses(output, 4, json.loads(printed or 'null'))
    return [Node(name, address) for name, address in sorted(addresses.items())]
