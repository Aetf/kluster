"""Stack outputs, by name: what crosses a stack boundary as a machine fact.

A stack output carries a machine fact — an address the cloud handed out, a
credential a program generated — and the fact is not a convention
(declarative/README.md §2).
Its *name* is: the exporting program writes under it, and every reader asks by
it — a StackReference in another stack program, a state read in the
`credentials` command — with nothing but the string between them. A name
spelled in each program separately drifts silently: a rename in the exporter
leaves a StackReference reading an absent output as `None` and a state read
finding nothing, and neither is caught at declaration. So the names are one
structure here, the one package a stack program and a script can both import
(style/pulumi.md), and each side reads its half from the same field.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class PhysicalOutputs:
    """The `physical` stack's exports, under the names every reader asks for them by.

    One field per export. A reader that needs a fact the stack does not publish
    adds a field here and an export there, in that order: the structure is the
    contract and the export is its implementation.
    """

    cluster_endpoint: str
    """The load balancer's public IPv4: the cluster endpoint, and the `dns` anchor's A."""
    cluster_endpoint_v6: str
    """The load balancer's public IPv6, which only the `dns` anchor's AAAA names."""
    vip1: str
    """The reserved public IPv4 behind the dedicated VIP (the `dns` `vip1` anchor)."""
    vip1_private: str
    """The secondary private address the reserved one is 1:1-NATed onto."""
    node_private_ips: str
    """Cloud node name → its VCN address."""
    node_public_ips: str
    """Cloud node name → its public address, which day 1 dials it at."""
    kubeconfig: str
    """The cluster-admin kubeconfig, secret; `k8s-base` and `apps` are built on it."""
    talosconfig: str
    """The Talos client configuration, secret."""
    backup_bucket: str
    """The backup bucket's name, on the account that is not the cloud's."""
    backup_endpoint: str
    """The S3 endpoint a consumer of that bucket is configured with."""
    backup_keys: str
    """Scope → the application key that scope's mover authenticates with."""
    ci_identity: Mapping[str, str]
    """Overlay roster member → the export carrying that member's join credential.

    Keyed by the roster name of the continuous-integration member
    (`overlay.CI_MEMBERS`), one per joining stack: the `credentials derived
    sync` command pushes each into the `ZEROTIER_IDENTITY` secret of the
    Environments whose jobs join with it (credentials.md §3, gateway.md §2.6).
    The mapping is written out rather than derived from the member's name so
    that a roster rename this table does not follow fails the run instead of
    quietly renaming the contract.
    """

    def names(self) -> tuple[str, ...]:
        """Every export name the structure carries, one entry per export.

        The shape of the structure spelled once: the string fields, then the
        identity mapping's values. What the exporting program exports is held
        equal to this as a set, and a name carried twice shows here as a
        repeat.
        """
        return (
            *(value for value in vars(self).values() if isinstance(value, str)),
            *self.ci_identity.values(),
        )


PHYSICAL_OUTPUTS = PhysicalOutputs(
    cluster_endpoint='cluster_endpoint',
    cluster_endpoint_v6='cluster_endpoint_v6',
    vip1='vip1',
    vip1_private='vip1_private',
    node_private_ips='node_private_ips',
    node_public_ips='node_public_ips',
    kubeconfig='kubeconfig',
    talosconfig='talosconfig',
    backup_bucket='backup_bucket',
    backup_endpoint='backup_endpoint',
    backup_keys='backup_keys',
    ci_identity={
        'ci-physical': 'ci_zerotier_identity_physical',
        'ci-dns': 'ci_zerotier_identity_dns',
    },
)
