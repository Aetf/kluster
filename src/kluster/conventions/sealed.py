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
namespace's writer keys, which every Secret there that backs up carries under
its own name (`writer_key`). A ciphertext ported from the legacy cluster as it is
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

from kluster.conventions.backup import BACKED_NAMESPACES, ETCD_SCOPE, BackupFamily, writer_scope
from kluster.conventions.cluster import (
    BGP_SECRETS_NAMESPACE,
    CERT_MANAGER_NAMESPACE,
    ETCD_BACKUP_NAMESPACE,
    MONITORING_NAMESPACE,
    WRITER_KEY_FIELDS,
)
from kluster.conventions.identity import STACK_NAMES

__all__ = (
    'ALERT_WEBHOOK',
    'AUTHELIA_STORAGE_KEY',
    'BGP_PASSWORD',
    'CONFIG_KEY',
    'DKIM_EXIM',
    'DNS01_TOKEN',
    'ETCD_WRITER_KEY',
    'NAMESPACE_WRITER_KEYS',
    'OCI_METRICS',
    'VALUES',
    'WRITER_KEYS_BY_SCOPE',
    'WRITER_KEY_STACKS',
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
    ciphertext serves every Secret of its namespace that backs up, under that
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

#: The id of the Home Assistant webhook alertmanager posts to, the one secret
#: part of its URL (rfc-007 §7.3). The rest of the URL is Home Assistant's
#: address, port and path, which the monitoring component composes around the
#: id from `conventions` (`components.monitoring.webhook_url`), so the address
#: is held once, in `HOME_ASSISTANT`, and a moved address needs no new seal.
ALERT_WEBHOOK = SealedValue(
    name='alert-webhook',
    namespace=MONITORING_NAMESPACE,
    keys=('id',),
    stack=STACK_NAMES.k8s_base,
)

#: The OCI API key the block-volume metrics pull signs with: its own user's,
#: whose one statement reads the metric namespace
#: `providers.OCI_BLOCKSTORE_METRICS` of `physical`'s compartment
#: (credentials.md §3). Its data keys are what the OCI SDK's
#: configuration calls the three parts a key carries -- the user it signs as,
#: the key's fingerprint and the PEM -- so a reader hands them over by name;
#: the tenancy and the region are constants of `conventions`, and travel with
#: no credential.
OCI_METRICS = SealedValue(
    name='oci-metrics',
    namespace=MONITORING_NAMESPACE,
    keys=('fingerprint', 'key_content', 'user'),
    stack=STACK_NAMES.k8s_base,
)

#: The backup bucket's `etcd` writer key, which the hourly etcd snapshot
#: uploads with (`components.backup.etcd`): `physical` mints it with the bucket,
#: as the scope `ETCD_SCOPE` (`components.backup.etcd_scope`), and `credentials
#: derived b2-writer seal` seals it out of that stack's state. Strict, unlike a
#: backed namespace's writer keys: one Secret reads it, under one name, and the
#: namespace-wide scope is for a key that every Secret of its namespace carries
#: under its own. Its data keys are the fields the snapshot reads the key's two
#: halves from, which are a mover's (`WRITER_KEY_FIELDS`).
ETCD_WRITER_KEY = SealedValue(
    name=f'writer-{ETCD_SCOPE}',
    namespace=ETCD_BACKUP_NAMESPACE,
    keys=tuple(sorted(WRITER_KEY_FIELDS)),
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

#: Authelia's storage encryption key, which encrypts the second factors and
#: the OIDC data in its database. A value of its own rather than a data key of
#: the ported `authelia` Secret, because it is the one value of that Secret
#: whose loss loses data: it is escrowed, and sealed here from the escrow by
#: `credentials derived authelia-storage-key` (credentials.md §2.2). The data
#: key is the one the ported Secret carried it under, so the server reads it as
#: before; the namespace is the legacy server's, which the ported server keeps.
AUTHELIA_STORAGE_KEY = SealedValue(
    name='authelia-storage-key',
    namespace='serving-system',
    keys=('STORAGE_ENCRYPTION_KEY',),
    stack=STACK_NAMES.apps,
)


#: The stack that declares what reads a backed namespace's writer keys, for
#: each namespace where that is not `apps`: a namespace the platform holds,
#: whose consumers `k8s-base` declares beside the rest of that namespace -- the
#: monitoring stack's backups, beside alertmanager's webhook
#: (`ALERT_WEBHOOK`). Every other backed namespace holds an application, which
#: `apps` declares. A key sealed for a stack other than its consumer's would be
#: ciphertext in a configuration nothing reads it from.
WRITER_KEY_STACKS: Mapping[str, str] = MappingProxyType({MONITORING_NAMESPACE: STACK_NAMES.k8s_base})


def writer_key(namespace: str, family: BackupFamily) -> SealedValue:
    """One namespace's writer key of one family on the backup bucket, sealed for the stack its consumers are in.

    `physical` mints the key (`conventions.backup.BACKED_NAMESPACES`) and
    `credentials derived b2-writer seal` seals it out of that stack's state,
    into the configuration of the stack that declares what reads it
    (`WRITER_KEY_STACKS`, and `apps` for every namespace it does not name).
    Sealed namespace-wide, so the one ciphertext opens under whatever name a
    consumer in its namespace gives it: each repository Secret a backed claim
    declares carries it under the claim's own name, beside that repository's
    password (declarative/workloads.md §3). Its data keys are the fields a
    mover reads the key's two halves from (`WRITER_KEY_FIELDS`), which
    `vmbackup` and `vmrestore` read as restic does. The name is the key's
    scope on the bucket after `writer-`, which is what keeps it apart from
    every value ported from the legacy cluster under that application's own
    name.
    """
    return SealedValue(
        name=f'writer-{writer_scope(namespace, family)}',
        namespace=namespace,
        keys=tuple(sorted(WRITER_KEY_FIELDS)),
        stack=WRITER_KEY_STACKS.get(namespace, STACK_NAMES.apps),
        scope=SealingScope.NAMESPACE_WIDE,
    )


#: Every writer key the backed namespaces hold, one per namespace and family,
#: each sealed namespace-wide (`writer_key`).
NAMESPACE_WRITER_KEYS: tuple[SealedValue, ...] = tuple(
    writer_key(namespace, family) for namespace, families in BACKED_NAMESPACES.items() for family in sorted(families)
)

#: Every writer key `credentials derived b2-writer seal` seals out of
#: `physical`'s state, by the scope `physical` exports it under: the etcd
#: snapshots' (`ETCD_WRITER_KEY`), sealed strict, then the backed namespaces'
#: (`NAMESPACE_WRITER_KEYS`), in the census's order.
WRITER_KEYS_BY_SCOPE: Mapping[str, SealedValue] = MappingProxyType(
    {
        ETCD_SCOPE: ETCD_WRITER_KEY,
        **{
            writer_scope(namespace, family): writer_key(namespace, family)
            for namespace, families in BACKED_NAMESPACES.items()
            for family in sorted(families)
        },
    }
)

#: Every sealed value, by name.
VALUES: Mapping[str, SealedValue] = MappingProxyType(
    {
        value.name: value
        for value in (
            DNS01_TOKEN,
            BGP_PASSWORD,
            ALERT_WEBHOOK,
            OCI_METRICS,
            ETCD_WRITER_KEY,
            DKIM_EXIM,
            AUTHELIA_STORAGE_KEY,
            *NAMESPACE_WRITER_KEYS,
        )
    }
)
