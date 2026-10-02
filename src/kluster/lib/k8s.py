"""Kubernetes helpers shared by the `k8s-base` and `apps` stacks.

Only what both stacks need: checking the kubeconfig their Kubernetes provider
is opened with, installing a pinned upstream chart, reaching into what one
rendered, creating a namespace at its Pod Security level, declaring a
SealedSecret in the shape
[declarative/cluster-infra.md](../../docs/declarative/cluster-infra.md) §1.1
fixes, and labeling a Service into a Cilium load-balancer pool. Anything
specific to one component belongs with that component, not here. A pinned
release manifest is fetched by `kluster.lib.release_assets`, which imports no
bindings, so `update_crds` can run it while it regenerates them.

These are functions returning provider resources rather than subclasses of
them. A subclass buys nothing over a call — the resource is the resource — and
it costs the caller's ability to see, in one place, exactly which arguments
were passed to the provider.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s
from pulumi.runtime.rpc import UNKNOWN as UNKNOWN_SENTINEL

from kluster import conventions
from kluster.conventions.sealed import SealingScope
from kluster.lib.versions import ChartPin

__all__ = (
    'KUBECONFIG_KEY',
    'POD_SECURITY_ENFORCE_LABEL',
    'PodSecurity',
    'SealingScope',
    'SecretTemplate',
    'UnusableKubeconfig',
    'find_rendered',
    'helm_chart',
    'kubeconfig_from',
    'lb_pool_labels',
    'namespace',
    'pick_resource',
    'sealed_secret',
)


#: Where the `k8s-base` and `apps` programs read the cluster-admin kubeconfig:
#: a config secret of each stack's own, in this project's namespace, as every
#: credential a program opens a provider with is (rfc-002 §8.1). It is
#: generated in the `physical` stack's state, and a StackReference cannot carry
#: it here: `physical` is encrypted under a passphrase of its own (rfc-005
#: §5.1), and a StackReference elides every secret output the reading stack
#: cannot decrypt. So `credentials derived sync --only kubeconfig` copies it out
#: of that state into both stacks' configuration (credentials.md §3), under this
#: key, which the slot map names as its target.
KUBECONFIG_KEY = 'kubeconfig'

#: What fills `KUBECONFIG_KEY`, named in every refusal below.
_FILLED_BY = f'`credentials derived sync --only {KUBECONFIG_KEY}`'


class UnusableKubeconfig(ValueError):
    """The stack's configuration holds no kubeconfig, or holds one no provider can open."""


def kubeconfig_from(configured: pulumi.Output[str] | None) -> pulumi.Output[str]:
    """The kubeconfig a stack's configuration holds, refused unless it is one a provider can open.

    `configured` is what the program read under `KUBECONFIG_KEY` with
    `get_secret`, at the line that builds its Kubernetes provider; this reads
    no configuration itself. Absent, it is refused here and now, naming the
    command that fills it -- `require_secret` would refuse too, but by telling
    the operator to `pulumi config set` a value by hand, which is the copy the
    command exists to make. Present, it can still be no kubeconfig:

    -   an empty or blank string;
    -   Pulumi's unknown sentinel, which a targeted apply of `physical` exports
        as an ordinary string (framework/pulumi.md §1.4), should a copy ever
        carry it here -- the command refuses to.

    The provider reads a kubeconfig it cannot load as an unreachable cluster,
    so a preview of plain resources would go green over nothing; one that is
    handed no kubeconfig at all falls back to `$KUBECONFIG` and then
    `~/.kube/config`, so an `up` from a shell holding another cluster's would
    act on that cluster. So the value is checked where it is read, and the run
    stops naming what it found and never the value.
    """
    if configured is None:
        raise UnusableKubeconfig(
            f"this stack's configuration holds no {KUBECONFIG_KEY!r}, so no Kubernetes provider is opened; "
            f"{_FILLED_BY} copies it in from the physical stack's state once physical has been applied"
        )
    return configured.apply(_usable_kubeconfig)


def _usable_kubeconfig(value: object) -> str:
    """`value` if it is a kubeconfig, else a refusal saying what it is -- never the value itself."""
    if isinstance(value, str) and value.strip() and value != UNKNOWN_SENTINEL:
        return value
    if value == UNKNOWN_SENTINEL:
        found = (
            "Pulumi's unknown sentinel, which a targeted apply of `physical` exports for a value it did not "
            'produce (framework/pulumi.md §1.4)'
        )
        remedy = f'apply physical in full, then {_FILLED_BY} copies it in again'
    else:
        found = 'a blank string' if isinstance(value, str) else f'a {type(value).__name__}'
        remedy = f"{_FILLED_BY} replaces it with physical's"
    raise UnusableKubeconfig(
        f"this stack's configured {KUBECONFIG_KEY!r} is {found}; no Kubernetes provider is opened with it. "
        f'{remedy[0].upper()}{remedy[1:]}'
    )


def helm_chart(
    name: str,
    *,
    pin: ChartPin,
    namespace: pulumi.Input[str],
    values: Mapping[str, Any] | None = None,
    skip_crds: bool = False,
    opts: pulumi.ResourceOptions | None = None,
) -> k8s.helm.v4.Chart:
    """An upstream chart, installed from the pin its caller resolved.

    The pin is configuration, and this helper reads none: the stack program
    installing the chart reads it with `versions.chart[<name>]`
    (`lib/versions.py`) and passes the result down, as it does every other pin
    (docs/style/pulumi.md, "Layering"). Where the pin lives is the
    `versions:chart-<name>` key in `Pulumi.yaml`'s project-level `config:`
    block, which renovate moves (docs/framework/pulumi.md §3.2).

    A chart from an OCI registry is located by its digest-pinned reference,
    `oci://<registry path>/<name>@sha256:<digest>`, with the version beside it.
    Helm pulls the manifest the digest names and refuses the pull when the
    version's tag resolves to any other (`pkg/registry/client.go` at the
    v3.20.2 the pinned `pulumi-kubernetes` embeds); an OCI reference carries
    its registry itself, so no repository options go with it. A chart from an
    HTTP repository is located by its name in that repository.

    :param pin: The parsed pin: where the chart is served, its version, and
        for an OCI chart its digest.
    :param values: The values the chart is installed with. The pin's
        `render_values` are `update_crds`'s, not these.
    :param skip_crds: Leave the chart's bundled CRDs uninstalled. Helm never
        upgrades a CRD it installed that way, so a component whose CRDs are
        declared separately sets this and keeps them upgradable.
    """
    return k8s.helm.v4.Chart(
        name,
        chart=pin.reference,
        version=pin.version,
        namespace=namespace,
        repository_opts=None if pin.oci else k8s.helm.v4.RepositoryOptsArgs(repo=pin.repository),
        values=dict(values) if values is not None else None,
        skip_crds=skip_crds,
        opts=opts,
    )


def find_rendered[R: pulumi.Resource](
    rendered: pulumi.Input[Sequence[Any]],
    kind: type[R],
    name_pattern: str = '*',
) -> pulumi.Output[R]:
    """The one resource of `kind` a chart rendered whose name matches.

    Takes the chart's `resources` rather than the chart, so the search can be
    handed any set of resources — including, in a test, one that no chart
    produced. `pick_resource` says what the search will and will not do.
    """

    def search(rendered: Sequence[Any]) -> pulumi.Output[R]:
        candidates = [resource for resource in rendered if isinstance(resource, pulumi.Resource)]
        urns = pulumi.Output.all(*[candidate.urn for candidate in candidates])
        return urns.apply(
            lambda urns: pick_resource(
                list(zip(candidates, cast('Sequence[str]', urns), strict=True)), kind, name_pattern
            )
        )

    return pulumi.Output.from_input(rendered).apply(search)


def pick_resource[R: pulumi.Resource](
    named: Sequence[tuple[pulumi.Resource, str]],
    kind: type[R],
    name_pattern: str = '*',
) -> R:
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


class PodSecurity(StrEnum):
    """A Pod Security Standards level, as the admission controller spells it.

    The values are Kubernetes' own: the admission controller reads them off a
    namespace's labels, and an unknown one is refused when the label is set.
    """

    PRIVILEGED = 'privileged'
    """No restriction: what a namespace needs when its pods use the host --
    its network, its PID namespace, its paths or its devices."""

    BASELINE = 'baseline'
    """Refuses the host's namespaces and paths, privileged containers and
    added capabilities beyond a default set."""

    RESTRICTED = 'restricted'
    """`baseline`, plus: volumes only of the configMap, csi, downwardAPI,
    emptyDir, ephemeral, persistentVolumeClaim, projected and secret types;
    no privilege escalation; a non-root user; a `RuntimeDefault` or
    `Localhost` seccomp profile; and every capability dropped,
    `NET_BIND_SERVICE` alone allowed back."""


#: The label the Pod Security admission controller enforces a namespace's
#: level from. A pod that violates the level is refused at admission.
POD_SECURITY_ENFORCE_LABEL = 'pod-security.kubernetes.io/enforce'


def namespace(
    resource_name: str,
    *,
    name: str,
    pod_security: PodSecurity,
    opts: pulumi.ResourceOptions | None = None,
) -> k8s.core.v1.Namespace:
    """A namespace, with the Pod Security level its pods are admitted under.

    The level is required rather than defaulted, because a namespace that
    states none is held to the cluster-wide default, whatever that is, and the
    pods in it fail admission only once they are created
    (declarative/cluster-infra.md §0). A namespace whose level is not
    `restricted` says why where it is declared.

    :param resource_name: The logical name, which carries the declaring
        component's name.
    :param name: The namespace's own name. It is fixed rather than autonamed:
        what installs into the namespace names it.
    """
    return k8s.core.v1.Namespace(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(
            name=name,
            labels={POD_SECURITY_ENFORCE_LABEL: pod_security.value},
        ),
        opts=opts,
    )


@dataclass(frozen=True, kw_only=True)
class SecretTemplate:
    """The produced Secret, minus the fields that had to be encrypted.

    This is the `template.data` pattern (cluster-infra.md §1.1) as a type:
    `data` holds the whole configuration file or connection string in
    plaintext, with a Go template expression where a decrypted field belongs
    (`{{ index . "password" }}`). What stays in the repository is therefore
    reviewable configuration with credential-shaped holes in it.

    `data` does not print. It is the produced Secret's own data, and a value in
    it arrives as an `Output` — whose repr discloses nothing — only where the
    caller made it one: the type promises nothing, and a test hands over the
    literal. The field is hidden for what it holds rather than for how one of
    its types prints.
    """

    data: Mapping[str, pulumi.Input[str]] | None = field(default=None, repr=False, compare=False)
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
    if pool not in (conventions.POOL_INTERNET, conventions.LAN_POOL.name):
        raise ValueError(f'no such load-balancer pool: {pool}')
    return {conventions.LB_POOL_LABEL: pool}
