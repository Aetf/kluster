"""The SealedSecret channel (docs/credentials.md §1 rule 6): a value sealed to the cluster, committed in the clear.

A value the cluster itself consumes reaches it as a SealedSecret, and what
this side writes is the ciphertext alone: `kubeseal` output, which opens with
the cluster's sealing key and nothing else, written as a **plain** value into
the configuration of the stack that declares it, at the path its row in
`conventions.sealed` derives. The stack program reads it there and declares
the SealedSecret. No stack encryption is involved, because none is needed.

**The tool, not a reimplementation.** Sealing is a hybrid encryption format of
the sealed-secrets controller's own, so it goes through the pinned `kubeseal`
(mise.toml), which is the tool built for it; doing it here would be
hand-rolled cryptography that could disagree with the controller.

**The certificate comes from the cluster**, fetched by `kubeseal` itself over
the API server's service proxy from the controller `conventions` names, with
the kubeconfig in `physical`'s state, which is where the cluster's credential
is generated and what the copies in other stacks follow. It is fetched once
per run, before anything is minted, so a cluster that cannot
be reached refuses the run while the refusal costs nothing.

**A seal is fresh every time.** `kubeseal` draws a session key per value, so
sealing the same plaintext twice gives two ciphertexts, and the write after a
re-run is a change to the committed file whatever was in it -- a repeated
`seal` of a value the cluster already holds included. That is accepted rather
than checked for: the ciphertext cannot be compared with the plaintext without
the cluster's private key, and a fresh ciphertext of the same value opens to
what the old one did, so the cost of a re-run is a diff and nothing else.
"""

from __future__ import annotations

import logging
import subprocess as sp
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pulumi.runtime import rpc

from ... import conventions
from . import pulumi_config
from .pulumi_config import SlotRefused

log = logging.getLogger(__name__)

#: The pinned tool (mise.toml).
KUBESEAL = 'kubeseal'

#: Long enough for a fetch across the internet to the cluster's endpoint, short
#: enough that a cluster that does not answer fails the run rather than
#: holding it. Sealing itself is local and takes nothing like this.
TIMEOUT = 60


class Kubeseal(Protocol):
    """How `kubeseal` is run: its arguments, and what it reads on standard input."""

    def __call__(self, args: Sequence[str], *, stdin: str | None) -> str: ...


def run_kubeseal(args: Sequence[str], *, stdin: str | None) -> str:
    """Run the pinned `kubeseal`; a failure is a slot that would not take the value."""
    try:
        proc = sp.run([KUBESEAL, *args], input=stdin, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except FileNotFoundError as exc:
        raise SlotRefused(f'{KUBESEAL} is not on PATH; mise.toml pins it, so run under `mise x -- ...`') from exc
    except sp.TimeoutExpired as exc:
        raise SlotRefused(f'{KUBESEAL} did not finish within {TIMEOUT} seconds') from exc
    if proc.returncode != 0:
        raise SlotRefused(f'{KUBESEAL} refused: {proc.stderr.strip() or f"exit {proc.returncode}"}')
    return proc.stdout


class NoCluster(SlotRefused):
    """`physical` has published no kubeconfig, so there is no cluster to seal to yet."""


def cluster_kubeconfig(physical: pulumi_config.Stack) -> str:
    """The kubeconfig `physical`'s state exports; `NoCluster` where it exports none.

    Read out of state rather than out of a copy in another stack's
    configuration, because state is where the cluster's credential is
    generated and the copies follow it (credentials.md §3, the kubeconfig
    row). An export that is not a string, or is Pulumi's unknown sentinel --
    what an apply restricted by `--target` leaves for an export it skipped --
    is refused by name rather than handed to `kubeseal` as a file.
    """
    output = conventions.PHYSICAL_OUTPUTS.kubeconfig
    if not physical.exists():
        raise NoCluster(f'the `{physical.name}` stack has no state, so there is no cluster to seal to yet')
    value = physical.outputs().get(output)
    if value is None:
        raise NoCluster(
            f'the `{physical.name}` stack exports no `{output}`, so there is no cluster to seal to yet; '
            f'its first `up` publishes one'
        )
    if not isinstance(value, str) or not value.strip():
        raise SlotRefused(f'the `{physical.name}` stack output `{output}` is not a kubeconfig')
    if value == rpc.UNKNOWN:
        raise SlotRefused(
            f"the `{physical.name}` stack exports `{output}` as Pulumi's unknown sentinel, which an apply "
            f'restricted by `--target` leaves behind; apply `{physical.name}` again with no targets'
        )
    return value


def fetch_certificate(kubeconfig: str, *, run: Kubeseal = run_kubeseal) -> str:
    """The certificate the cluster's controller seals to, fetched by `kubeseal` itself.

    The kubeconfig is a credential, so it is written to a file of its own in
    a directory only this user can enter, for the length of the one call, and
    removed with it. The certificate is public.
    """
    log.info(
        'fetching the sealing certificate from %s/%s through the API server',
        conventions.SEALING_NAMESPACE,
        conventions.SEALING_CONTROLLER,
    )
    with tempfile.TemporaryDirectory(prefix='kluster-seal-') as directory:
        path = Path(directory) / 'kubeconfig'
        _ = path.write_text(kubeconfig)
        path.chmod(0o600)
        certificate = run(
            [
                '--fetch-cert',
                '--kubeconfig',
                str(path),
                '--controller-name',
                conventions.SEALING_CONTROLLER,
                '--controller-namespace',
                conventions.SEALING_NAMESPACE,
            ],
            stdin=None,
        )
    if 'BEGIN CERTIFICATE' not in certificate:
        raise SlotRefused(f'{KUBESEAL} --fetch-cert printed no certificate')
    return certificate


@dataclass(frozen=True)
class Sealer:
    """Seals values to one cluster's certificate and writes them where their stack reads them."""

    #: The controller's certificate, PEM. Public.
    certificate: str
    #: Opens a stack by name, as the run already opens every other one: the
    #: stack picks its own passphrase out of the run's environment.
    open_stack: Callable[[str], pulumi_config.Stack]
    run: Kubeseal = run_kubeseal

    def stack(self, value: conventions.sealed.SealedValue) -> pulumi_config.Stack:
        """The stack that declares `value`, refused unless the state backend holds it.

        A seal fills a stack that is already there: creating one is that
        stack's own bring-up. Asked before anything is minted, so a refusal
        leaves nothing live at a provider.
        """
        stack = self.open_stack(value.stack)
        if not stack.exists():
            raise SlotRefused(
                f'the `{value.stack}` stack does not exist in the state backend, so it has no configuration to '
                f'write the sealed {value.name} into; `pulumi stack init {value.stack} --no-select` creates it'
            )
        return stack

    def seal(self, value: conventions.sealed.SealedValue, data: Mapping[str, str]) -> dict[str, str]:
        """Each of `data`'s values sealed for `value`'s name, namespace and scope, by data key.

        `data` has to carry exactly the keys the row names: a key the program
        does not read is a ciphertext nothing opens, and a missing one is a
        Secret with a hole in it.
        """
        if set(data) != set(value.keys):
            raise SlotRefused(f'{value.name} carries {", ".join(value.keys)}; this seal was given {", ".join(data)}')
        sealed: dict[str, str] = {}
        with tempfile.TemporaryDirectory(prefix='kluster-seal-') as directory:
            cert = Path(directory) / 'cert.pem'
            _ = cert.write_text(self.certificate)
            for key in value.keys:
                log.info('sealing %s/%s %s, %s', value.namespace, value.name, key, value.scope.value)
                ciphertext = self.run(
                    [
                        '--raw',
                        '--scope',
                        value.scope.value,
                        '--name',
                        value.name,
                        '--namespace',
                        value.namespace,
                        '--cert',
                        str(cert),
                        '--from-file=/dev/stdin',
                    ],
                    stdin=data[key],
                ).strip()
                if not ciphertext:
                    raise SlotRefused(f'{KUBESEAL} printed nothing for {value.name} {key}')
                sealed[key] = ciphertext
        return sealed

    def write(self, value: conventions.sealed.SealedValue, sealed: Mapping[str, str]) -> None:
        """Write ciphertext already sealed for `value` into its stack, each at the path its row derives."""
        stack = self.stack(value)
        for key in value.keys:
            stack.set_plain_at(value.path(key), sealed[key])
        log.info(
            'the %s stack holds the sealed %s; commit Pulumi.%s.yaml to publish it', stack.name, value.name, stack.name
        )

    def deliver(self, value: conventions.sealed.SealedValue, data: Mapping[str, str]) -> None:
        """Seal `data` for `value` and write it into its stack."""
        self.write(value, self.seal(value, data))


def cluster_sealer(
    physical: pulumi_config.Stack, *, open_stack: Callable[[str], pulumi_config.Stack], run: Kubeseal = run_kubeseal
) -> Sealer:
    """A `Sealer` for the cluster `physical` brought up; `NoCluster` where it has brought none up yet."""
    return Sealer(certificate=fetch_certificate(cluster_kubeconfig(physical), run=run), open_stack=open_stack, run=run)


__all__ = (
    'KUBESEAL',
    'Kubeseal',
    'NoCluster',
    'Sealer',
    'cluster_kubeconfig',
    'cluster_sealer',
    'fetch_certificate',
    'run_kubeseal',
)
