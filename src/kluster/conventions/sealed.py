"""The sealed values: what `credentials` seals, and where the stack that declares each one reads it.

A sealed value is `kubeseal` ciphertext of a Secret's data, which opens with
the cluster's sealing key alone. It needs no stack encryption, so it is a
**plain** value in the configuration of the stack that declares it, committed
in the clear beside that stack's config secrets (credentials.md §1 rule 6,
rfc-007 §6.2). Two programs agree on each one: the `credentials` command seals
it and writes it there, and the stack program reads it there and hands it to
`sealed_secret`. Two readers is what places the census here.

Each row names the Secret it becomes, the namespace that Secret lives in, the
keys of its data, the scope it is sealed at and the stack that declares it.
Where its ciphertext lives follows from the row (`SealedValue.path`): one
structured key, `CONFIG_KEY`, keyed by the value's name and then by its data
key, in the project's own namespace like every other key the programs read.
The program reads it back by the same row's path
(`kluster.lib.k8s.sealed_ciphertext`), so the writer and the reader cannot
name two places.

A row sealed namespace-wide opens under any name in its namespace, so its name
says where its ciphertext is kept rather than which Secret it becomes: a
namespace's writer keys, which every repository Secret there carries under its
own name (`writer_key`). A ciphertext ported from the legacy cluster as it is
has no row: it is not sealed here, the component that declares it is its one
reader, and that component names it and the namespace it was sealed to
(declarative/workloads.md §5).

Read qualified -- `conventions.sealed.DNS01_TOKEN` -- because the module path
says what a prefix on every name would otherwise have to.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from kluster.conventions.backup import BACKED_NAMESPACES, BackupFamily, writer_scope
from kluster.conventions.cluster import (
    BGP_SECRETS_NAMESPACE,
    CERT_MANAGER_NAMESPACE,
    MONITORING_NAMESPACE,
    WRITER_KEY_FIELDS,
)
from kluster.conventions.identity import STACK_NAMES

__all__ = (
    'ALERT_WEBHOOK',
    'BGP_PASSWORD',
    'CONFIG_KEY',
    'DKIM_EXIM',
    'DNS01_TOKEN',
    'VALUES',
    'WRITER_KEYS',
    'SealedValue',
    'SealingScope',
    'writer_key',
)


class SealingScope(StrEnum):
    """How much of a SealedSecret's identity its ciphertext is bound to.

    The scope is sealed *into* the ciphertext by `kubeseal`, so it describes
    how the value was produced rather than being a switch that can be flipped
    afterwards: a manifest whose annotation disagrees with the sealing it was
    given simply fails to decrypt. The values are `kubeseal --scope`'s own.
    """

    STRICT = 'strict'
    """Name and namespace both fixed. `kubeseal`'s own default, and the scope
    of every value `credentials` seals but the writer keys: nothing about one
    will be renamed."""

    NAMESPACE_WIDE = 'namespace-wide'
    """Namespace fixed, any name. What the legacy cluster's secrets carry, so
    it stays the default of `sealed_secret`: the migration restores the legacy
    sealing key and ports the existing manifests unchanged
    (cluster/migration.md §0.5), and a different default would re-seal all of
    them for no gain. The writer keys are sealed at it too, because one
    ciphertext serves every repository Secret of its namespace under that
    Secret's own name (`writer_key`)."""

    CLUSTER_WIDE = 'cluster-wide'
    """Neither fixed — a secret any namespace can decrypt. Never a default."""


#: The structured configuration key every sealed value lives under, keyed by
#: the value's name and then by its data key. Bare, and therefore in the
#: project's own namespace, which is the one `pulumi.Config()` resolves
#: against inside the program.
CONFIG_KEY = 'sealedSecrets'


@dataclass(frozen=True)
class SealedValue:
    """One Secret the cluster receives as a SealedSecret, and where its ciphertext is committed."""

    name: str
    """The value's name under `CONFIG_KEY`, and the name `kubeseal` seals it
    for. Strict, it is the Secret's name too, which the SealedSecret carries."""
    namespace: str
    keys: tuple[str, ...]
    """The keys of the Secret's data, each sealed on its own."""
    stack: str
    """The stack that declares the SealedSecret, and whose configuration holds
    the ciphertext."""
    scope: SealingScope = SealingScope.STRICT

    def path(self, key: str) -> str:
        """Where the ciphertext of `key` is, as `pulumi config set --path` names it.

        Each segment quoted, because `--path` reads every dot as a level: a
        data key such as `tls.key` would otherwise land one level below the
        value's name, where the program, which reads the name and then the
        data key, finds nothing. Refused for a key the value does not carry,
        which is the one way a writer and a reader of the same row could
        otherwise name two places.
        """
        if key not in self.keys:
            raise KeyError(f'{self.name} carries {", ".join(self.keys)}, not {key}')
        return f'{CONFIG_KEY}["{self.name}"]["{key}"]'


#: The Cloudflare token cert-manager's cluster issuer answers DNS-01 challenges
#: with (rfc-007 §5.2), in the namespace a cluster issuer's credentials are
#: read from under the chart's defaults.
DNS01_TOKEN = SealedValue(
    name='cloudflare-dns01',
    namespace=CERT_MANAGER_NAMESPACE,
    keys=('api-token',),
    stack=STACK_NAMES.k8s_base,
)

#: The worker's end of the BGP session's MD5 password: Cilium's BGPv2
#: `authSecretRef` reads the key `password` from a Secret in its secrets
#: namespace (rfc-007 §4.6).
BGP_PASSWORD = SealedValue(
    name='bgp-password',
    namespace=BGP_SECRETS_NAMESPACE,
    keys=('password',),
    stack=STACK_NAMES.k8s_base,
)

#: The address of the Home Assistant webhook alertmanager posts to, which the
#: receiver reads through the VictoriaMetrics operator's `url_secret`, so it
#: appears in no rendered configuration (rfc-007 §7.3).
ALERT_WEBHOOK = SealedValue(
    name='alert-webhook',
    namespace=MONITORING_NAMESPACE,
    keys=('url',),
    stack=STACK_NAMES.k8s_base,
)

#: The mail relay's DKIM private key, which exim signs every mail zone's `k8s`
#: selector with and reads as `tls.key`, the key the legacy cluster's
#: cert-manager Secret holds it under. It is carried from the legacy cluster
#: rather than minted, so the public half `dns.DKIM_K8S` publishes stays its
#: own (declarative/workloads.md §5). The namespace is the legacy relay's,
#: which the ported relay keeps: its other SealedSecrets are sealed
#: namespace-wide to it.
DKIM_EXIM = SealedValue(
    name='dkim-exim',
    namespace='mail-system',
    keys=('tls.key',),
    stack=STACK_NAMES.apps,
)


def writer_key(namespace: str, family: BackupFamily) -> SealedValue:
    """One namespace's writer key of one family on the backup bucket, sealed for the `apps` stack.

    `physical` mints the key (`conventions.backup.BACKED_NAMESPACES`) and
    `credentials derived b2-writer seal` seals it out of that stack's state.
    Sealed namespace-wide, so the one ciphertext opens under whatever name a
    consumer in its namespace gives it: each repository Secret a backed claim
    declares carries it under the claim's own name, beside that repository's
    password (declarative/workloads.md §3). Its data keys are the fields a
    mover reads the key's two halves from (`WRITER_KEY_FIELDS`). The name is
    the key's scope on the bucket after `writer-`, which is what keeps it
    apart from every value ported from the legacy cluster under that
    application's own name.
    """
    return SealedValue(
        name=f'writer-{writer_scope(namespace, family)}',
        namespace=namespace,
        keys=tuple(sorted(WRITER_KEY_FIELDS)),
        stack=STACK_NAMES.apps,
        scope=SealingScope.NAMESPACE_WIDE,
    )


#: Every writer key the backed namespaces hold, one per namespace and family.
WRITER_KEYS: tuple[SealedValue, ...] = tuple(
    writer_key(namespace, family) for namespace, families in BACKED_NAMESPACES.items() for family in sorted(families)
)

#: Every sealed value, by name.
VALUES: Mapping[str, SealedValue] = MappingProxyType(
    {value.name: value for value in (DNS01_TOKEN, BGP_PASSWORD, ALERT_WEBHOOK, DKIM_EXIM, *WRITER_KEYS)}
)
