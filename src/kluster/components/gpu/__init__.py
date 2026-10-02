"""Node Feature Discovery and the Intel GPU device plugin: a node's GPU offered to pods as a resource.

Both are installed from the start, and the plugin is inert until the homelab
worker's GPU is bound to it, so binding it needs no `k8s-base` change
(declarative/cluster-infra.md §1, item 7). What keeps it inert is a node label.
The plugin's `GpuDevicePlugin` selects the nodes labeled `GPU_NODE_LABEL`, and
the one rule that sets the label is the plugin chart's own `NodeFeatureRule`,
which NFD evaluates on every node: an Intel display-class PCI device whose
kernel has `i915` or `xe` loaded or enabled. No node has one before the GPU is
bound, so the DaemonSet the operator creates for the plugin schedules nowhere.

NFD runs on every node regardless; it is the node-labeling half, and the GPU
plugin is its one consumer today.

The operator itself is not inert at its pinned chart, 0.36.0. That chart
registers two mutating webhooks on every pod creation in the cluster, the
FPGA's and SGX's, and an operator running the GPU's controller alone serves
neither: each call is answered 404 and fails open (`failurePolicy: Ignore`),
after up to the 10 s default timeout when the worker is unreachable. Chart
0.37.0 removes the FPGA's and registers SGX's only for an operator that runs
SGX's controller, and Aetf/kluster#428, renovate's in-cluster bump, moves both
Intel pins to it.

Both namespaces are `privileged` (cluster-infra.md §0): NFD's worker reads the
host's `/boot`, `/sys`, `/proc/swaps`, libraries, OS release and local
feature files through host paths, and the plugin mounts the GPU's device
nodes. The plugin's DaemonSet runs in the operator's namespace, which is where
the operator creates it.
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi

from kluster.lib.k8s import PodSecurity, helm_chart, namespace
from kluster.lib.versions import ChartPin
from putils import Component

__all__ = (
    'GPU_NODE_LABEL',
    'INTEL_NAMESPACE',
    'NFD_NAMESPACE',
    'IntelGpuPlugin',
    'NodeFeatureDiscovery',
    'gpu_values',
    'nfd_values',
    'operator_values',
)

#: The namespace NFD runs in, its upstream's default.
NFD_NAMESPACE = 'node-feature-discovery'

#: The namespace the Intel device-plugin operator runs in, and so the one the
#: GPU plugin's DaemonSet runs in: the operator's upstream default.
INTEL_NAMESPACE = 'inteldeviceplugins-system'

#: The label the plugin chart's `NodeFeatureRule` sets on a node with an Intel
#: GPU, and that the plugin selects its nodes by.
GPU_NODE_LABEL = 'intel.feature.node.kubernetes.io/gpu'


def nfd_values() -> dict[str, object]:
    """NFD's values: its post-delete cleanup, a hook, off.

    The cleanup is a Helm hook, which `helm.v4.Chart` drops, so it is switched
    off rather than left to vanish (cluster-infra.md §1.2). It would remove
    the labels NFD set from the nodes once the chart is uninstalled; without
    it, a destroy leaves them on the nodes, where nothing selects them once
    the plugin is gone too.
    """
    return {'postDeleteCleanup': False}


def operator_values() -> dict[str, object]:
    """The operator's values: the GPU's controller alone, of the device kinds it can run."""
    return {'manager': {'devices': {'gpu': True}}}


def gpu_values() -> dict[str, object]:
    """The plugin's values: its nodes selected by the label its own `NodeFeatureRule` sets."""
    return {
        'name': 'gpu',
        'nodeFeatureRule': True,
        'nodeSelector': {GPU_NODE_LABEL: 'true'},
    }


class NodeFeatureDiscovery(Component, pulumi_type='kluster:gpu:NodeFeatureDiscovery'):
    """NFD and its definitions, from the chart's pin, in a namespace of their own.

    `after` is what NFD is installed behind. It is stated on the children
    rather than left to the component's own `depends_on`, which Pulumi does
    not push down to a component's children.
    """

    def __init__(
        self,
        name: str,
        *,
        chart: ChartPin,
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        behind = self.child_opts(depends_on=list(after))

        self.namespace = namespace(
            f'{name}-namespace', name=NFD_NAMESPACE, pod_security=PodSecurity.PRIVILEGED, opts=behind
        )
        self.chart = helm_chart(
            name, pin=chart, namespace=self.namespace.metadata.name, values=nfd_values(), opts=behind
        )

        self.register_outputs({})


class IntelGpuPlugin(Component, pulumi_type='kluster:gpu:IntelGpuPlugin'):
    """The Intel device-plugin operator and the `GpuDevicePlugin` it runs, each from its pin.

    `after` is what both are installed behind, and it has to hold cert-manager
    and NFD: the operator's chart declares a cert-manager `Issuer` and
    `Certificate` for its webhook, and the plugin's chart declares NFD's
    `NodeFeatureRule`s. The plugin's chart is behind the operator's too, which
    defines the `GpuDevicePlugin` and admits it through its webhook. Each is
    stated on the children rather than left to the component's own
    `depends_on`, which Pulumi does not push down to a component's children.

    The operator's image is published for amd64 alone and its chart selects
    amd64 nodes, so it runs only on the homelab worker: an `up` that changes
    either chart needs the worker Ready, since the operator's Deployment and
    the webhook that admits the `GpuDevicePlugin` are both on it.
    """

    def __init__(
        self,
        name: str,
        *,
        operator_chart: ChartPin,
        gpu_chart: ChartPin,
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        behind = self.child_opts(depends_on=list(after))

        self.namespace = namespace(
            f'{name}-namespace', name=INTEL_NAMESPACE, pod_security=PodSecurity.PRIVILEGED, opts=behind
        )
        self.operator = helm_chart(
            f'{name}-operator',
            pin=operator_chart,
            namespace=self.namespace.metadata.name,
            values=operator_values(),
            opts=behind,
        )
        self.gpu = helm_chart(
            f'{name}-gpu',
            pin=gpu_chart,
            namespace=self.namespace.metadata.name,
            values=gpu_values(),
            opts=self.child_opts(depends_on=[*after, self.operator]),
        )

        self.register_outputs({})
