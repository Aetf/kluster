"""Everything `packages/crds` is generated from.

The chart set of [declarative/cluster-infra.md](../../../../docs/declarative/cluster-infra.md)
§1, written down as versions. Every entry carries the *floor* it has to clear —
a minimum stated in the design docs, with the section that states it — so a
version bump that would violate one is visible in the diff rather than only at
the next `up`.

Where a floor exists, the pin is the newest release that clears it; where none
exists, the pin is simply the newest release. Floors are on the **operator**,
which for several projects is the chart's `appVersion` rather than its chart
version: `cloudnative-pg` 0.29.0 ships CNPG 1.30.0. Both are recorded, and a
test holds `app_version` to `min_app_version`.

Only the sources whose CRDs the cluster actually declares are rendered.
Everything else in §1 is listed anyway, with `crds=False`, so this file is a
complete register of what the stack installs rather than a partial one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field


def version_tuple(version: str) -> tuple[int, ...]:
    """The numeric components of a version, for comparing one against a floor.

    Upstream is not consistent about the leading `v` (`v1.21.1` and `1.20.1`
    are both here), and a floor is written as far as it is meaningful —
    `1.26` covers every `1.26.x`. Comparing tuples handles both, and stops at
    the first non-numeric component so a pre-release suffix cannot make a
    version sort below the release it precedes.
    """
    components: list[int] = []
    for part in version.lstrip('vV').split('.'):
        match = re.match(r'\d+', part)
        if match is None:
            break
        components.append(int(match.group()))
    return tuple(components)


# --- Tools ----------------------------------------------------------------

#: Helm 3, deliberately: `pulumi-kubernetes`' `helm.v4.Chart` renders with the
#: Helm **3** SDK (cluster-infra.md §1.2), so rendering the CRD bundle with a
#: Helm 3 binary renders it the way the cluster will. Helm 4 is a different
#: renderer and would make this bundle a prediction about a tool nobody runs.
# renovate: datasource=github-releases depName=helm/helm versioning=semver
HELM_VERSION = '3.20.0'
HELM_URL = f'https://get.helm.sh/helm-v{HELM_VERSION}-linux-amd64.tar.gz'

#: The archive's own published digest. Recomputed on every download, so a
#: truncated or substituted tarball fails the run instead of rendering
#: something else. A version bump updates both lines together.
HELM_SHA256 = 'dbb4c8fc8e19d159d1a63dda8db655f9ffa4aac1b9a6b188b34a40957119b286'

#: Pinned rather than `latest`: this binary decides the shape of every
#: generated module *and* the `pulumi-kubernetes` version `packages/crds`
#: declares, so an unpinned one would rewrite the bindings without a bump.
# renovate: datasource=github-releases depName=pulumi/crd2pulumi
CRD2PULUMI_VERSION = 'v1.6.2'

# --- Sources --------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Chart:
    """An upstream Helm chart, and how much of it is CRDs."""

    name: str
    """The chart name inside `repo`."""

    repo: str
    """The chart repository URL."""

    version: str
    """The chart version, which is not necessarily the operator's version."""

    app_version: str
    """The operator version this chart version ships, for the floor check."""

    floor: str
    """Why the pin cannot go below where it is — the doc section that says so,
    or `NO FLOOR` when the docs record none and the pin is simply the latest."""

    min_app_version: str | None = None
    """The `app_version` this pin may not fall below, when `floor` names one."""

    crds: bool = True
    """Whether the chart renders any CustomResourceDefinition at all."""

    values: Mapping[str, str] = field(default_factory=dict[str, str])
    """`--set` values needed to make the chart render its CRDs."""


@dataclass(frozen=True, kw_only=True)
class ReleaseManifest:
    """A plain YAML bundle published as a GitHub release asset."""

    repo: str
    tag: str
    asset: str
    floor: str


@dataclass(frozen=True, kw_only=True)
class SourceTree:
    """CRD YAML that lives in a project's source tree and nowhere else.

    Cilium is the case: its chart installs no CustomResourceDefinition, because
    the agent registers its own at runtime. The definitions still exist as
    checked-in YAML at each release tag, which is what makes an offline render
    of them possible at all.
    """

    repo: str
    ref: str
    paths: Sequence[str]
    floor: str


#: Gateway API is not a chart (cluster-infra.md §1 item 1). The **experimental**
#: channel is required, not preferred: the ExternalAuth HTTPRoute filter
#: (GEP-1494) that gates every non-OIDC app ships only there (cluster-infra.md
#: §2, "Route-level auth").
MANIFESTS: Sequence[ReleaseManifest] = (
    ReleaseManifest(
        repo='kubernetes-sigs/gateway-api',
        # renovate: datasource=github-releases depName=kubernetes-sigs/gateway-api
        tag='v1.6.1',
        asset='experimental-install.yaml',
        floor='cluster-infra.md §1 item 1 — experimental channel, for ExternalAuth (GEP-1494)',
    ),
)

#: Cilium's CRDs, from the tag matching the pinned chart. The two move
#: together: bindings generated from a different release than the one running
#: describe fields the cluster does not have.
SOURCE_TREES: Sequence[SourceTree] = (
    SourceTree(
        repo='cilium/cilium',
        # renovate: datasource=github-releases depName=cilium/cilium
        ref='v1.20.1',
        paths=(
            'pkg/k8s/apis/cilium.io/client/crds/v2',
            'pkg/k8s/apis/cilium.io/client/crds/v2alpha1',
        ),
        floor='cluster-infra.md §2 — ≥1.20 for the ExternalAuth route filter, above the ≥1.16 tunnel-mode Egress Gateway floor (architecture.md §3.2)',
    ),
)

#: The §1 chart set. Order follows the install order, so the register reads
#: like the dependency chain it encodes.
CHARTS: Sequence[Chart] = (
    Chart(
        name='cilium',
        repo='https://helm.cilium.io/',
        # renovate: datasource=helm depName=cilium registryUrl=https://helm.cilium.io/
        version='1.20.1',
        app_version='1.20.1',
        min_app_version='1.20',
        floor='cluster-infra.md §2 — ExternalAuth route filter (GEP-1494); also covers the ≥1.16 tunnel-mode Egress Gateway floor',
        # The chart installs none: the agent registers them at runtime, so they
        # come from SOURCE_TREES instead.
        crds=False,
    ),
    Chart(
        name='sealed-secrets',
        # The project moved from the `bitnami-labs` organization to `bitnami`
        # in 2026, and the old GitHub Pages repository is gone rather than
        # redirected — the previous URL answers 404.
        repo='https://bitnami.github.io/sealed-secrets',
        # renovate: datasource=helm depName=sealed-secrets registryUrl=https://bitnami.github.io/sealed-secrets
        version='2.19.3',
        app_version='0.39.1',
        floor='NO FLOOR',
    ),
    Chart(
        name='cert-manager',
        repo='https://charts.jetstack.io',
        # renovate: datasource=helm depName=cert-manager registryUrl=https://charts.jetstack.io
        version='v1.21.1',
        app_version='v1.21.1',
        floor='NO FLOOR',
        # cert-manager ships its CRDs as templates behind this switch rather
        # than in the chart's `crds/` directory, so the render has to ask.
        values={'crds.enabled': 'true'},
    ),
    Chart(
        name='cloudnative-pg',
        repo='https://cloudnative-pg.github.io/charts',
        # renovate: datasource=helm depName=cloudnative-pg registryUrl=https://cloudnative-pg.github.io/charts
        version='0.29.0',
        app_version='1.30.0',
        min_app_version='1.26',
        floor='cluster-infra.md §1 item 5 — declarative offline in-place major upgrades (workloads.md §4)',
    ),
    Chart(
        name='plugin-barman-cloud',
        repo='https://cloudnative-pg.github.io/charts',
        # renovate: datasource=helm depName=plugin-barman-cloud registryUrl=https://cloudnative-pg.github.io/charts
        version='0.7.1',
        app_version='v0.14.0',
        floor='NO FLOOR',
    ),
    Chart(
        name='volsync',
        repo='https://backube.github.io/helm-charts/',
        # renovate: datasource=helm depName=volsync registryUrl=https://backube.github.io/helm-charts/
        version='0.16.0',
        app_version='0.16.0',
        floor='NO FLOOR',
    ),
    Chart(
        name='victoria-metrics-k8s-stack',
        repo='https://victoriametrics.github.io/helm-charts/',
        # renovate: datasource=helm depName=victoria-metrics-k8s-stack registryUrl=https://victoriametrics.github.io/helm-charts/
        version='0.91.2',
        app_version='v1.150.0',
        floor='NO FLOOR',
    ),
    Chart(
        name='node-feature-discovery',
        repo='https://kubernetes-sigs.github.io/node-feature-discovery/charts',
        # renovate: datasource=helm depName=node-feature-discovery registryUrl=https://kubernetes-sigs.github.io/node-feature-discovery/charts
        version='0.19.0',
        app_version='v0.19.0',
        floor='NO FLOOR',
    ),
    Chart(
        name='intel-device-plugins-operator',
        repo='https://intel.github.io/helm-charts/',
        # renovate: datasource=helm depName=intel-device-plugins-operator registryUrl=https://intel.github.io/helm-charts/
        version='0.36.0',
        app_version='0.36.0',
        floor='NO FLOOR',
    ),
    Chart(
        name='intel-device-plugins-gpu',
        repo='https://intel.github.io/helm-charts/',
        # renovate: datasource=helm depName=intel-device-plugins-gpu registryUrl=https://intel.github.io/helm-charts/
        version='0.36.0',
        app_version='0.36.0',
        floor='NO FLOOR',
        # The GpuDevicePlugin object it creates is defined by the operator
        # chart above; this one is a plain workload.
        crds=False,
    ),
    Chart(
        name='metrics-server',
        repo='https://kubernetes-sigs.github.io/metrics-server/',
        # renovate: datasource=helm depName=metrics-server registryUrl=https://kubernetes-sigs.github.io/metrics-server/
        version='3.14.0',
        app_version='0.9.0',
        floor='NO FLOOR',
        # `metrics.k8s.io` is an aggregated APIService, not a CRD.
        crds=False,
    ),
    Chart(
        name='reloader',
        repo='https://stakater.github.io/stakater-charts',
        # renovate: datasource=helm depName=reloader registryUrl=https://stakater.github.io/stakater-charts
        version='2.2.16',
        app_version='v1.4.21',
        floor='NO FLOOR',
        crds=False,
    ),
)

#: Groups that reach the renderer but must not reach the bindings.
#:
#: `fpga.intel.com` rides along in the Intel operator chart and belongs to the
#: retired FPGA plugin; `gateway.networking.x-k8s.io` is the experimental
#: channel's *extended* group (XBackend, XMesh, XBackendTrafficPolicy), which
#: nothing here declares and whose module name collides with the standard
#: group's under `crd2pulumi`'s first-segment naming.
DROPPED_GROUPS: frozenset[str] = frozenset(
    {
        'fpga.intel.com',
        'gateway.networking.x-k8s.io',
    }
)
