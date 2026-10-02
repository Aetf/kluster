"""What only `update_crds` reads: the tools it runs, a source no pin locates, the groups it drops.

The chart set and the Gateway API manifest are not here: they are pins in the
`versions:` block of `Pulumi.yaml`, which the stack program installs from and
this script reads out of the file through the program's own parser
(`kluster.lib.versions`, docs/framework/pulumi.md §3.2). What stays is what no
stack program reads — the Helm binary and the CRD generator, each with its
digest; the directories of the Cilium source tree the definitions are read
from, at the ref the Cilium chart's pin names; and the definition groups that
reach the renderer but not the bindings.

Renovate reads exactly one thing in this file — the `crd2pulumi` version and
the digest beside it, through the custom manager in `renovate.json5` that moves
the pair together. The Helm binary moves by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from kluster.lib.versions import ChartPin

# --- Tools ----------------------------------------------------------------

#: Helm 3, deliberately: `pulumi-kubernetes`' `helm.v4.Chart` renders with the
#: Helm **3** SDK (cluster-infra.md §1.2), so rendering the CRD bundle with a
#: Helm 3 binary renders it the way the cluster will. Helm 4 is a different
#: renderer and would make this bundle a prediction about a tool nobody runs.
HELM_VERSION = '3.20.0'
HELM_URL = f'https://get.helm.sh/helm-v{HELM_VERSION}-linux-amd64.tar.gz'

#: The archive's own published digest. Recomputed on every download, so a
#: truncated or substituted tarball fails the run instead of rendering
#: something else. A version bump updates both lines together.
HELM_SHA256 = 'dbb4c8fc8e19d159d1a63dda8db655f9ffa4aac1b9a6b188b34a40957119b286'

#: Pinned rather than `latest`: this binary decides the shape of every
#: generated module, so an unpinned one would rewrite the bindings without a
#: bump. The `pulumi-kubernetes` version `packages/crds` declares is not its
#: to decide: the release it was built against is baked into the dependency
#: line it writes, and `cli` rewrites that line to the version the bindings
#: were generated against.
#:
#: The version is part of the asset's file name, and that is what lets the pin
#: below travel with a bump: renovate finds the next release's asset by
#: substituting the new version into this name.
CRD2PULUMI_VERSION = 'v1.6.2'
CRD2PULUMI_URL = (
    f'https://github.com/pulumi/crd2pulumi/releases/download/{CRD2PULUMI_VERSION}'
    f'/crd2pulumi-{CRD2PULUMI_VERSION}-linux-amd64.tar.gz'
)

#: The digest that release publishes for that asset, in its `checksums.txt`.
#: Recomputed on every download, so a truncated or substituted tarball fails
#: the run instead of generating bindings from a binary nobody pinned. The
#: manager in `renovate.json5` moves this line with the version above.
CRD2PULUMI_SHA256 = 'eda24f0fd79654784171357cffca7f6e99d46f1026da429ee416dbe993c73309'

# --- The source tree -------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SourceTree:
    """CRD YAML that lives in a project's source tree and nowhere else.

    Cilium is the case: its chart installs no CustomResourceDefinition, because
    the agent registers its own at runtime. The definitions still exist as
    checked-in YAML at each release tag, which is what makes an offline render
    of them possible at all.

    The ref is not written here. It is the release the chart named `chart`
    pins, so the bindings describe the release the cluster runs and a chart
    bump moves both; a second copy of the version would be a second place for
    it to be wrong.
    """

    repo: str
    chart: str
    """The `<name>` of the `versions:chart-<name>` pin whose version is the tag."""

    paths: Sequence[str]

    def ref(self, pin: ChartPin) -> str:
        """The tag the chart's version is released under."""
        if pin.name != self.chart:
            raise ValueError(f'{self.repo} is read at the version of the {self.chart} chart, not of {pin.name}')
        return f'v{pin.version}'


SOURCE_TREES: Sequence[SourceTree] = (
    SourceTree(
        repo='cilium/cilium',
        chart='cilium',
        paths=(
            'pkg/k8s/apis/cilium.io/client/crds/v2',
            'pkg/k8s/apis/cilium.io/client/crds/v2alpha1',
        ),
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
