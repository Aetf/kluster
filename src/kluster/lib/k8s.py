"""Kubernetes helpers for the `k8s-base` and `apps` stacks.

What both stacks need: checking the kubeconfig their Kubernetes provider is
opened with, installing a pinned upstream chart, reaching into what one
rendered, creating a namespace at its Pod Security level, declaring a
SealedSecret in the shape
[declarative/cluster-infra.md](../../docs/declarative/cluster-infra.md) §1.1
fixes -- for a row of the sealed census, from the row -- and labeling a
Service into a Cilium load-balancer pool. Anything specific to one component
belongs with that component, not here. A pinned release manifest is fetched
by `kluster.lib.release_assets`, which imports no bindings, so `update_crds`
can run it while it regenerates them.

One helper here is `apps`' alone: `application_namespace`, an application's
namespace with its route label and its policy. It is the base the `apps`
components compose on: the component base of rfc-007 §9.1, which the second
half of rfc-007 slice 12 writes beside `route`, calls it. It is built on
`namespace` and needs nothing of the `apps` area, so it sits beside the
helper it extends.

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
from typing import Annotated, Any, Literal, cast

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s
from pulumi.runtime.rpc import UNKNOWN as UNKNOWN_SENTINEL

from kluster import conventions
from kluster.conventions.sealed import SealedValue, SealingScope
from kluster.lib.config import Shape, ShapeRefused, text
from kluster.lib.secret import Secret
from kluster.lib.versions import ChartPin

__all__ = (
    'INGRESS_ENTITY',
    'KUBECONFIG_KEY',
    'POD_SECURITY_ENFORCE_LABEL',
    'ApplicationNamespace',
    'LooserLevel',
    'PodSecurity',
    'SealingScope',
    'SecretTemplate',
    'UnusableKubeconfig',
    'application_namespace',
    'find_rendered',
    'helm_chart',
    'kubeconfig_from',
    'lb_pool_labels',
    'namespace',
    'pick_resource',
    'sealed_ciphertext',
    'sealed_secret',
    'sealed_value',
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


@dataclass(frozen=True, kw_only=True)
class LooserLevel:
    """A namespace's exception to `restricted`: the level its pods are admitted under instead, and why.

    `restricted` is what `namespace` gives a namespace that names none of
    these, so a namespace held to less is one whose declaration says so, with
    its reason on the same lines, and a loosening is a line in the diff rather
    than one level traded for another. The level is typed to the two looser
    ones: an exception that named `restricted` would be no exception, and the
    type refuses it where it is written.
    """

    level: Literal[PodSecurity.BASELINE, PodSecurity.PRIVILEGED]
    #: What the namespace's pods need that `restricted` refuses: the host's
    #: namespaces or paths, a device, or a security context the chart does not
    #: expose.
    reason: str


def namespace(
    resource_name: str,
    *,
    name: str,
    looser: LooserLevel | None = None,
    labels: Mapping[str, str] | None = None,
    opts: pulumi.ResourceOptions | None = None,
) -> k8s.core.v1.Namespace:
    """A namespace, its pods admitted under `restricted` unless `looser` names a lower level and why.

    The level is stated on every namespace, never left off, because a
    namespace that states none is held to the cluster-wide default, whatever
    that is, and the pods in it fail admission only once they are created
    (declarative/cluster-infra.md §0). `restricted` is the level a caller gets
    by naming nothing, so what a declaration has to spell out is the
    exception, and the exception carries its reason (`LooserLevel`).

    :param resource_name: The logical name, which carries the declaring
        component's name.
    :param name: The namespace's own name. It is fixed rather than autonamed:
        what installs into the namespace names it.
    :param looser: The level the namespace is held to instead of `restricted`,
        and why.
    :param labels: Further labels the namespace carries. The level is not one
        of them: a mapping naming `POD_SECURITY_ENFORCE_LABEL` is refused,
        since `looser` is the one way to state a level other than
        `restricted`, and the one that carries a reason.
    """
    if labels is not None and POD_SECURITY_ENFORCE_LABEL in labels:
        raise ValueError(
            f'namespace {name!r} is handed {POD_SECURITY_ENFORCE_LABEL!r} as a label; '
            'its level is `restricted` unless `looser` names another, with the reason'
        )
    level = PodSecurity.RESTRICTED if looser is None else looser.level
    return k8s.core.v1.Namespace(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(
            name=name,
            labels={**(labels or {}), POD_SECURITY_ENFORCE_LABEL: level.value},
        ),
        opts=opts,
    )


#: The identity Cilium gives what its Gateways' Envoy forwards to a backend
#: ([`ingress-reference.rst` L28-56](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/network/servicemesh/ingress-reference.rst#L28-L56)),
#: so a pod's policy sees a routed request arrive from it rather than from
#: the client.
INGRESS_ENTITY = 'ingress'


@dataclass(frozen=True)
class ApplicationNamespace:
    """An application's namespace and the policy that holds every pod in it to default-deny."""

    namespace: k8s.core.v1.Namespace
    policy: crds.cilium.v2.CiliumNetworkPolicy


def application_namespace(
    resource_name: str,
    *,
    name: str,
    looser: LooserLevel | None = None,
    opts: pulumi.ResourceOptions | None = None,
) -> ApplicationNamespace:
    """The namespace an application's component installs into, admitting from outside it the Gateways alone.

    Three things, each of which an application would otherwise have to
    remember (declarative/workloads.md §1):

    -   **The level**, `restricted` unless `looser` names another and why, as
        `namespace` gives every namespace.
    -   **The route label** (`conventions.routes.ROUTE_LABEL`), which the
        Gateways' `allowedRoutes` select on, so an `HTTPRoute` declared in
        the namespace attaches to them. A namespace with no route still
        carries it: what it admits is a route the component declares, and
        the policy below is what decides who reaches the pods.
    -   **A default-deny policy on ingress**, selecting every pod in the
        namespace and admitting two peers: the namespace's own pods, and
        `INGRESS_ENTITY`, the identity a request a Gateway forwards arrives
        with. An endpoint selector that names no namespace selects in the
        policy's own
        ([`kubernetes.rst` L216-219](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/security/policy/kubernetes.rst#L216-L219)),
        which is how the first peer reads. Any other peer -- a pod in
        another namespace, a client at a raw port -- is refused until the
        component declares an allow for it, as a policy of its own beside
        this one, since Cilium admits what any policy selecting a pod
        allows. The one peer no policy decides is the pod's own node, which
        the agent admits whatever the policies say: `allow-localhost` is
        `auto`, which is `always` under Kubernetes
        ([`config.go` L69-70](https://github.com/cilium/cilium/blob/v1.20.1/pkg/option/config.go#L69-L70)),
        and the cilium component sets no other. That is how the kubelet's
        probes arrive, and how a host-network pod on that node does.

    Default-deny is stated rather than left to follow from the ingress rules
    being there, because imposing it is what this policy is for: the
    cluster-wide baseline leaves it off (rfc-007 §4.5), and a policy that
    leaves it off is ignored when Cilium decides an endpoint's mode
    ([`intro.rst` L56-58](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/security/policy/intro.rst#L56-L58)),
    so this one decides it. Egress is not this policy's: it states no egress
    rule and leaves default-deny off in that direction, so the pods reach out
    as the baseline lets them. Restricting it is the component's, and a
    component's first egress rule switches default-deny on for egress on
    every pod it selects, since one policy requesting it is enough
    ([`rule.go` L118-136](https://github.com/cilium/cilium/blob/v1.20.1/pkg/policy/api/rule.go#L118-L136)):
    the policy that carries that rule carries the allow to the cluster's DNS
    as well, or those pods resolve no name.

    :param resource_name: The logical name, which carries the declaring
        component's name; the policy's is this with `-default-deny`.
    :param name: The namespace's own name, which the component's other
        resources and its routes are declared in.
    :param looser: The level the namespace is held to instead of
        `restricted`, and why.
    """
    ns = namespace(resource_name, name=name, looser=looser, labels=conventions.routes.ROUTE_LABEL, opts=opts)
    policy = crds.cilium.v2.CiliumNetworkPolicy(
        f'{resource_name}-default-deny',
        metadata=k8s.meta.v1.ObjectMetaArgs(name='default-deny', namespace=ns.metadata.name),
        spec=crds.cilium.v2.CiliumNetworkPolicySpecArgs(
            description=(
                "Admit to every pod in the namespace the namespace's own pods and what a Gateway forwards, "
                'and nothing else.'
            ),
            endpoint_selector=crds.cilium.v2.CiliumNetworkPolicySpecEndpointSelectorArgs(),
            ingress=[
                crds.cilium.v2.CiliumNetworkPolicySpecIngressArgs(
                    from_endpoints=[crds.cilium.v2.CiliumNetworkPolicySpecIngressFromEndpointsArgs()]
                ),
                crds.cilium.v2.CiliumNetworkPolicySpecIngressArgs(from_entities=[INGRESS_ENTITY]),
            ],
            enable_default_deny=crds.cilium.v2.CiliumNetworkPolicySpecEnableDefaultDenyArgs(ingress=True, egress=False),
        ),
        opts=opts,
    )
    return ApplicationNamespace(namespace=ns, policy=policy)


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

    data: Annotated[Mapping[str, pulumi.Input[str]] | None, Secret] = field(default=None, repr=False, compare=False)
    type: str | None = None
    immutable: bool | None = None
    labels: Mapping[str, str] | None = None
    annotations: Mapping[str, str] | None = None


def sealed_secret(
    resource_name: str,
    *,
    name: str,
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

    :param resource_name: The logical name, which carries the declaring
        component's name.
    :param name: The name of the object and of the Secret it becomes.
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
        resource_name,
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


def sealed_ciphertext(value: SealedValue, configured: object) -> dict[str, str] | None:
    """The ciphertext of each of a census row's keys, out of what the declaring stack holds under its sealed key.

    `configured` is that whole object as the program read it, `None` when the
    stack holds no sealed value at all. The read is the inverse of
    `SealedValue.path` -- the value's name, then each data key -- so what the
    sealing command wrote is what is found.

    `None` when the stack holds nothing under the row's name: a value not yet
    sealed in, which `sealed_value` declares as nothing until the bring-up's
    gate has passed (physical/bring-up.md §1). Anything else that is not a
    non-empty string per key is a write gone wrong, and is refused as a
    `ShapeRefused` naming its path and its type, never its content
    (`kluster.lib.config`): ciphertext does not belong in a run log either.
    """
    if configured is None:
        return None
    if not isinstance(configured, Mapping):
        raise _not_a_mapping(conventions.sealed.CONFIG_KEY, configured, 'one entry per value')
    held = cast('Mapping[str, object]', configured).get(value.name)
    if held is None:
        return None
    if not isinstance(held, Mapping):
        raise _not_a_mapping(f'{conventions.sealed.CONFIG_KEY}["{value.name}"]', held, 'its data keys')
    data = cast('Mapping[str, object]', held)
    return {key: text(data.get(key), value.path(key)) for key in value.keys}


def _not_a_mapping(what: str, found: object, expected: str) -> ShapeRefused:
    return ShapeRefused(
        f'{what} must hold {expected}, and is {type(found).__name__}', what=what, shape=Shape.WRONG_TYPE
    )


def sealed_value(
    resource_name: str,
    value: SealedValue,
    *,
    ciphertext: Mapping[str, pulumi.Input[str]] | None,
    opts: pulumi.ResourceOptions | None = None,
) -> crds.bitnami.v1alpha1.SealedSecret | None:
    """The SealedSecret of a census row, named, placed and scoped as the row says; none while its ciphertext is absent.

    A row's ciphertext is sealed to the row's name and namespace at the row's
    scope (`conventions.sealed`), so the object is declared from the row
    rather than from arguments a caller could get wrong. `ciphertext` is what
    the program read for the row with `sealed_ciphertext`. The row names the
    object and not the resource: `resource_name`, the logical name, is the
    caller's, and carries the declaring component's name as every child's
    does (style/pulumi.md), whichever row the component declares.

    **Absent, it declares nothing and warns, naming the value.** A sealed
    value can be made only once the controller that opens it runs, which is
    after the cluster's first `up`, so the stacks merge ahead of their values
    and whatever reads one stays pending until the commit that adds it. That
    is a dated exception to the rule that a value the program cannot run
    without stops the run at the read (style/pulumi.md, "Layering"), and it
    retires when the bring-up's gate has passed (physical/bring-up.md §1):
    from then on an absent value is refused by name. Until then the warning
    is what tells an `up` that declared nothing from one that declared the
    value.
    """
    if ciphertext is None:
        pulumi.log.warn(
            f"sealed value {value.name!r} ({value.namespace}) is absent from this stack's "
            f'{conventions.sealed.CONFIG_KEY}, so no SealedSecret is declared for it and what reads it stays pending '
            f'until it is sealed in (physical/bring-up.md §1)'
        )
        return None
    return sealed_secret(
        resource_name,
        name=value.name,
        namespace=value.namespace,
        encrypted_data=ciphertext,
        scope=value.scope,
        opts=opts,
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
