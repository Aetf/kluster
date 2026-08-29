"""Kubernetes helpers shared by the `k8s-base` and `apps` stacks.

Only what both stacks need: installing a pinned upstream chart, reaching into
what one rendered, declaring a SealedSecret in the shape
[declarative/cluster-infra.md](../../docs/declarative/cluster-infra.md) §1.1
fixes, and labelling a Service into a Cilium load-balancer pool. Anything
specific to one component belongs with that component, not here.

These are functions returning provider resources rather than subclasses of
them. A subclass buys nothing over a call — the resource is the resource — and
it costs the caller's ability to see, in one place, exactly which arguments
were passed to the provider.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar, cast

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.lib.versions import versions

__all__ = (
    'SealingScope',
    'SecretTemplate',
    'find_rendered',
    'helm_chart',
    'lb_pool_labels',
    'pick_resource',
    'sealed_secret',
)

_Resource = TypeVar('_Resource', bound=pulumi.Resource)

#: An OCI-registry chart carries its registry in the reference itself, so it
#: takes no repository options.
_OCI_SCHEME = 'oci://'


def helm_chart(
    name: str,
    *,
    chart: str,
    namespace: pulumi.Input[str],
    pin: str | None = None,
    values: Mapping[str, Any] | None = None,
    skip_crds: bool = False,
    opts: pulumi.ResourceOptions | None = None,
) -> k8s.helm.v4.Chart:
    """An upstream chart, pinned by stack configuration rather than by code.

    The repository and the version come from the `chart:<key>` config entry of
    the stack doing the installing (`repo:version`), because that is where
    renovate can see and bump them: `renovate.json5` groups the pins by the
    stack file they live in, so a chart bump is reviewable as one in-cluster
    change. A version written in Python would be a pin nobody bumps.

    :param chart: The chart reference — a name within the pinned repository,
        or a full ``oci://`` reference.
    :param pin: The `chart:` config key holding the pin, when it differs from
        the chart reference (an OCI reference is not a usable config key).
    :param skip_crds: Leave the chart's bundled CRDs uninstalled. Helm never
        upgrades a CRD it installed that way, so a component whose CRDs are
        declared separately sets this and keeps them upgradable.
    """
    pinned = versions.chart[pin if pin is not None else chart]
    return k8s.helm.v4.Chart(
        name,
        chart=chart,
        version=pinned.version,
        namespace=namespace,
        repository_opts=None if chart.startswith(_OCI_SCHEME) else k8s.helm.v4.RepositoryOptsArgs(repo=pinned.repo),
        values=dict(values) if values is not None else None,
        skip_crds=skip_crds,
        opts=opts,
    )


def find_rendered(
    rendered: pulumi.Input[Sequence[Any]],
    kind: type[_Resource],
    name_pattern: str = '*',
) -> pulumi.Output[_Resource]:
    """The one resource of `kind` a chart rendered whose name matches.

    Takes the chart's `resources` rather than the chart, so the search can be
    handed any set of resources — including, in a test, one that no chart
    produced. `pick_resource` says what the search will and will not do.
    """

    def search(rendered: Sequence[Any]) -> pulumi.Output[_Resource]:
        candidates = [resource for resource in rendered if isinstance(resource, pulumi.Resource)]
        urns = pulumi.Output.all(*[candidate.urn for candidate in candidates])
        return urns.apply(
            lambda urns: pick_resource(list(zip(candidates, cast('Sequence[str]', urns))), kind, name_pattern)
        )

    return pulumi.Output.from_input(rendered).apply(search)


def pick_resource(
    named: Sequence[tuple[pulumi.Resource, str]],
    kind: type[_Resource],
    name_pattern: str = '*',
) -> _Resource:
    """The single resource of `kind` whose name matches, out of `(resource, urn)` pairs.

    Deliberately strict: no match and more than one match are both errors,
    because either one means the caller's picture of the chart is wrong, and a
    silently chosen resource would then be wired somewhere by its address.

    The pattern is matched with shell globbing against the last segment of the
    URN — the Pulumi name, which Helm renders as ``<namespace>/<object name>``.
    """

    def resource_name(urn: str) -> str:
        return urn.rsplit('::', 1)[-1]

    matched = [
        resource
        for resource, urn in named
        if isinstance(resource, kind) and fnmatch.fnmatch(resource_name(urn), name_pattern)
    ]
    if len(matched) == 1:
        return matched[0]
    rendered = ', '.join(sorted(resource_name(urn) for _, urn in named)) or 'nothing'
    raise LookupError(f'{len(matched)} resources match {kind.__name__} {name_pattern!r}; the chart rendered {rendered}')


class SealingScope(StrEnum):
    """How much of a SealedSecret's identity its ciphertext is bound to.

    The scope is sealed *into* the ciphertext by `kubeseal`, so it describes
    how the value was produced rather than being a switch that can be flipped
    afterwards: a manifest whose annotation disagrees with the sealing it was
    given simply fails to decrypt.
    """

    STRICT = 'strict'
    """Name and namespace both fixed. `kubeseal`'s own default."""

    NAMESPACE_WIDE = 'namespace-wide'
    """Namespace fixed, any name. What the legacy cluster's secrets carry, so
    it stays the default here: the migration restores the legacy sealing key
    and ports the existing manifests unchanged (cluster/migration.md §0.5),
    and a different default would re-seal all of them for no gain."""

    CLUSTER_WIDE = 'cluster-wide'
    """Neither fixed — a secret any namespace can decrypt. Never a default."""


@dataclass(frozen=True, kw_only=True)
class SecretTemplate:
    """The produced Secret, minus the fields that had to be encrypted.

    This is the `template.data` pattern (cluster-infra.md §1.1) as a type:
    `data` holds the whole configuration file or connection string in
    plaintext, with a Go template expression where a decrypted field belongs
    (`{{ index . "password" }}`). What stays in the repository is therefore
    reviewable configuration with credential-shaped holes in it.
    """

    data: Mapping[str, pulumi.Input[str]] | None = None
    type: str | None = None
    immutable: bool | None = None
    labels: Mapping[str, str] | None = None
    annotations: Mapping[str, str] | None = None


def sealed_secret(
    name: str,
    *,
    namespace: pulumi.Input[str],
    encrypted_data: Mapping[str, pulumi.Input[str]],
    template: SecretTemplate | None = None,
    scope: SealingScope = SealingScope.NAMESPACE_WIDE,
    opts: pulumi.ResourceOptions | None = None,
) -> crds.bitnami.v1alpha1.SealedSecret:
    """A Secret whose sensitive fields are the only encrypted part of it.

    The first choice for anything the cluster itself consumes
    (cluster-infra.md §1.1). Sensitive values arrive as `encrypted_data` —
    `kubeseal` ciphertext, safe in a public repository — while `template`
    carries the rest of the Secret in the clear.

    The name is fixed rather than autonamed, because the workload that mounts
    the resulting Secret names it, and because every scope but `cluster-wide`
    seals the ciphertext against it. A replacement therefore deletes first:
    two objects of one name cannot coexist.

    :param namespace: The namespace the ciphertext was sealed for, which is
        more than where the object lands.
    :param encrypted_data: Field name to `kubeseal` ciphertext.
    """
    template = template if template is not None else SecretTemplate()
    scope_annotations = {f'sealedsecrets.bitnami.com/{scope.value}': 'true'}

    # The controller reads the scope off the template's metadata and only falls
    # back to the resource's own, so both carry it — the shape `kubeseal`
    # writes, which keeps a manifest round-trippable through it.
    template_metadata: dict[str, Any] = {
        'name': name,
        'annotations': {**(template.annotations or {}), **scope_annotations},
    }
    if template.labels is not None:
        template_metadata['labels'] = dict(template.labels)

    return crds.bitnami.v1alpha1.SealedSecret(
        name,
        metadata=k8s.meta.v1.ObjectMetaArgs(
            name=name,
            namespace=namespace,
            annotations=scope_annotations,
            labels=dict(template.labels) if template.labels is not None else None,
        ),
        spec=crds.bitnami.v1alpha1.SealedSecretSpecArgs(
            encrypted_data=dict(encrypted_data),
            template=crds.bitnami.v1alpha1.SealedSecretSpecTemplateArgs(
                metadata=template_metadata,
                data=dict(template.data) if template.data is not None else None,
                type=template.type,
                immutable=template.immutable,
            ),
        ),
        opts=pulumi.ResourceOptions.merge(pulumi.ResourceOptions(delete_before_replace=True), opts),
    )


def lb_pool_labels(pool: str) -> dict[str, str]:
    """The labels that put a Service in a Cilium load-balancer pool.

    Pool membership is a property of the *Service* here, matched by the pool's
    `serviceSelector` (cluster-infra.md §2). The legacy cluster decided it on
    the *node* instead, with k3s `svccontroller` labels — a shape with no
    successor, because Cilium allocates an address from a pool rather than
    lending out whichever node happens to be announcing.
    """
    if pool not in (conventions.POOL_INTERNET, conventions.POOL_LAN):
        raise ValueError(f'no such load-balancer pool: {pool}')
    return {conventions.LB_POOL_LABEL: pool}
