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

Both namespaces are `privileged` (cluster-infra.md §0), each with its reason
at its declaration: NFD's worker reads the host through host paths, and the
plugin mounts the GPU's device nodes. The plugin's DaemonSet runs in the
operator's namespace, which is where the operator creates it.

**Both per-node agents run at `conventions.NODE_AGENT_PRIORITY_CLASS`**, so no
pod the scheduler places on their node, a held one preempting included, can
take their room. NFD's worker is given it through its chart. The plugin's
DaemonSet is the operator's, built from a manifest it embeds, and neither that
manifest nor `GpuDevicePlugin` names a class, so a mutating admission policy
sets it on that DaemonSet as the operator writes it (`plugin_priority`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.lib.k8s import LooserLevel, NodeRequirement, PodSecurity, helm_chart, namespace
from kluster.lib.versions import ChartPin
from putils import Component

__all__ = (
    'GPU_NODE',
    'GPU_NODE_LABEL',
    'GPU_PLUGIN',
    'GPU_RESOURCE',
    'PLUGIN_PRIORITY_POLICY',
    'IntelGpuPlugin',
    'NodeFeatureDiscovery',
    'PluginPriority',
    'gpu_values',
    'nfd_values',
    'operator_values',
    'plugin_daemonset',
    'plugin_priority',
)

#: The label the plugin chart's `NodeFeatureRule` sets on a node with an Intel
#: GPU, and that the plugin selects its nodes by.
GPU_NODE_LABEL = 'intel.feature.node.kubernetes.io/gpu'

#: The label and the value the plugin chart's `NodeFeatureRule` sets on a node
#: with an Intel GPU (`templates/gpu.yaml` of the pinned chart): the nodes the
#: plugin selects, and so the nodes that may offer `GPU_RESOURCE`.
GPU_NODE = NodeRequirement(GPU_NODE_LABEL, 'true')

#: The resource the plugin offers a GPU bound to `i915` as, which a pod
#: requests to be given the worker's GPU. `i915` is the driver the worker's
#: image carries (`kluster.components.talos.image.HOMELAB_EXTENSIONS`); a GPU
#: bound to `xe` is offered as `gpu.intel.com/xe` instead.
GPU_RESOURCE = 'gpu.intel.com/i915'

#: The name of the `GpuDevicePlugin` the plugin's chart declares (`gpu_values`),
#: which the operator names the plugin's DaemonSet after.
GPU_PLUGIN = 'gpu'


def nfd_values() -> dict[str, object]:
    """NFD's values: its post-delete cleanup, a hook, off, and its worker at the class per-node agents run at.

    The cleanup is a Helm hook, which `helm.v4.Chart` drops, so it is switched
    off rather than left to vanish (cluster-infra.md §1.2). It would remove
    the labels NFD set from the nodes once the chart is uninstalled; without
    it, a destroy leaves them on the nodes, where nothing selects them once
    the plugin is gone too.

    The worker is a per-node agent its DaemonSet keeps on its node, which the
    chart leaves at no class, and so at 0, where a pod held to that node
    could preempt it.
    """
    return {
        'postDeleteCleanup': False,
        'worker': {'priorityClassName': conventions.NODE_AGENT_PRIORITY_CLASS},
    }


def operator_values() -> dict[str, object]:
    """The operator's values: the GPU's controller alone, of the device kinds it can run."""
    return {'manager': {'devices': {'gpu': True}}}


def gpu_values() -> dict[str, object]:
    """The plugin's values: its nodes selected by the label its own `NodeFeatureRule` sets."""
    return {
        'name': GPU_PLUGIN,
        'nodeFeatureRule': True,
        'nodeSelector': {GPU_NODE.key: GPU_NODE.value},
    }


# -- The plugin's class: a layer over an upstream gap --------------------------
#
# The layer `plugin_priority` describes. Removing it deletes:
#
# -   this section, from here to `NodeFeatureDiscovery`;
# -   its four names in `__all__` (`PLUGIN_PRIORITY_POLICY`, `PluginPriority`,
#     `plugin_daemonset`, `plugin_priority`), and the imports only it uses:
#     `json`, `dataclass` and `pulumi_kubernetes`;
# -   in `IntelGpuPlugin`, the call that sets `self.priority`, the
#     `*self.priority.resources` in the plugin chart's `depends_on`, and the
#     docstring's sentence on the policy;
# -   the module docstring's sentence on the plugin's DaemonSet;
# -   the sentences on the policy in declarative/cluster-infra.md §1, item 7;
# -   the section on the plugin's class in `tests/test_operators.py`, its
#     tripwire included;
# -   the operator chart's renovate note on the DaemonSet's name.

#: The name of the DaemonSet in the manifest the operator embeds and builds
#: each plugin's from (`deployments/gpu_plugin/base/intel-gpu-plugin.yaml` at
#: v0.36.0).
_PLUGIN_DAEMONSET_BASE = 'intel-gpu-plugin'

#: The label the API server sets on every namespace, holding its name.
_NAMESPACE_NAME_LABEL = 'kubernetes.io/metadata.name'

#: The mutating admission policy that sets the plugin's class, and its binding.
PLUGIN_PRIORITY_POLICY = 'intel-gpu-plugin-priority'


def plugin_daemonset(plugin: str) -> str:
    """The name of the DaemonSet the operator creates for the `GpuDevicePlugin` named `plugin`.

    `NewDaemonSet` in the operator's `pkg/controllers/gpu/controller.go` at
    v0.36.0 names it `SuffixedName(<the embedded manifest's name>, <the
    plugin's name>)`, and `SuffixedName` (`pkg/controllers/reconciler.go`)
    joins the two with a `-`.
    """
    return f'{_PLUGIN_DAEMONSET_BASE}-{plugin}'


@dataclass(frozen=True)
class PluginPriority:
    """What `plugin_priority` emitted: the policy that sets the plugin's class, and its one binding."""

    policy: k8s.admissionregistration.v1.MutatingAdmissionPolicy
    binding: k8s.admissionregistration.v1.MutatingAdmissionPolicyBinding

    @property
    def resources(self) -> list[pulumi.Resource]:
        """Both, for what has to be installed behind them."""
        return [self.policy, self.binding]


def plugin_priority(resource_name: str, *, plugin: str, opts: pulumi.ResourceOptions | None = None) -> PluginPriority:
    """The policy that puts the DaemonSet the operator creates for `plugin` at the class per-node agents run at, and its binding.

    **A layer over an upstream gap, removed whole.** The operator builds the
    plugin's DaemonSet from a manifest it embeds, which names no class, and
    `GpuDevicePlugin` has no field to give it one (`NewDaemonSet` in its
    `pkg/controllers/gpu/controller.go`, from v0.36.0 through upstream `main`
    past v0.37.1). It is removed once `GpuDevicePlugin` gains a priority
    class field, which `gpu_values` then sets; the list above the layer is
    what the removal deletes. The operator's definition is bundled with the
    program, and a test reads it and goes red once it takes such a field.
    The detour leans on two things of the operator's that no test of ours
    can observe, the DaemonSet's name and its namespace, so an operator bump
    re-reads `NewDaemonSet`, as the chart's renovate note says.

    The policy matches the creates and updates of one DaemonSet, the one the
    operator derives from `plugin`'s name, in the operator's namespace, and
    applies the class onto its pod template, on every write the API server
    admits once it has taken the policy in. That is the boundary of the
    guarantee. The program installs the plugin's chart behind the policy and
    its binding, which orders the writes to the API, while the API server
    takes the policy in through its informers, after its write. And the
    operator writes the DaemonSet only when it creates it and when one of
    the fields its `UpdateDaemonSet` compares differs: the image, the init
    container, the node selector, the arguments or the tolerations. So a
    DaemonSet written before the policy took effect, or before this layer
    existed, keeps no class until its next write, and
    `kubectl rollout restart` is such a write: a patch, which the policy
    matches as an update (declarative/cluster-infra.md §1, item 7). A
    failure to apply refuses the write (`failurePolicy: Fail`), which
    leaves the plugin unscheduled, its `GpuDevicePlugin` naming no
    DaemonSet and the operator logging the refusal, rather than scheduled
    at 0.
    """
    a = k8s.admissionregistration.v1
    priority_class = json.dumps(conventions.NODE_AGENT_PRIORITY_CLASS)
    configuration = (
        'Object{spec: Object.spec{template: Object.spec.template{spec: Object.spec.template.spec{'
        f'priorityClassName: {priority_class}'
        '}}}}'
    )
    policy = a.MutatingAdmissionPolicy(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(name=PLUGIN_PRIORITY_POLICY),
        spec=a.MutatingAdmissionPolicySpecArgs(
            failure_policy='Fail',
            match_constraints=a.MatchResourcesArgs(
                namespace_selector=k8s.meta.v1.LabelSelectorArgs(
                    match_labels={_NAMESPACE_NAME_LABEL: conventions.INTEL_DEVICE_PLUGINS_NAMESPACE}
                ),
                resource_rules=[
                    a.NamedRuleWithOperationsArgs(
                        api_groups=['apps'],
                        api_versions=['v1'],
                        operations=['CREATE', 'UPDATE'],
                        resources=['daemonsets'],
                        resource_names=[plugin_daemonset(plugin)],
                    )
                ],
            ),
            mutations=[
                a.MutationArgs(
                    patch_type='ApplyConfiguration',
                    apply_configuration=a.ApplyConfigurationArgs(expression=configuration),
                )
            ],
            reinvocation_policy='Never',
        ),
        opts=opts,
    )
    binding = a.MutatingAdmissionPolicyBinding(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(name=PLUGIN_PRIORITY_POLICY),
        spec=a.MutatingAdmissionPolicyBindingSpecArgs(policy_name=PLUGIN_PRIORITY_POLICY),
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=[policy])),
    )
    return PluginPriority(policy=policy, binding=binding)


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
            f'{name}-namespace',
            name=conventions.NFD_NAMESPACE,
            looser=LooserLevel(
                level=PodSecurity.PRIVILEGED,
                reason="the worker reads the host's `/boot`, `/sys`, `/proc/swaps`, libraries, OS release and "
                'local feature files through host paths, which `baseline` refuses',
            ),
            opts=behind,
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
    defines the `GpuDevicePlugin` and admits it through its webhook, and
    behind the policy that sets its DaemonSet's class (`plugin_priority`),
    so the operator's writes carry the class once the API server has taken
    the policy in; `plugin_priority` says what that leaves out. Each is
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
            f'{name}-namespace',
            name=conventions.INTEL_DEVICE_PLUGINS_NAMESPACE,
            looser=LooserLevel(
                level=PodSecurity.PRIVILEGED,
                reason="the plugin's DaemonSet, which the operator creates in this namespace, mounts the GPU's "
                'device nodes from the host, which `baseline` refuses',
            ),
            opts=behind,
        )
        self.operator = helm_chart(
            f'{name}-operator',
            pin=operator_chart,
            namespace=self.namespace.metadata.name,
            values=operator_values(),
            opts=behind,
        )
        self.priority = plugin_priority(f'{name}-priority', plugin=GPU_PLUGIN, opts=behind)
        self.gpu = helm_chart(
            f'{name}-gpu',
            pin=gpu_chart,
            namespace=self.namespace.metadata.name,
            values=gpu_values(),
            opts=self.child_opts(depends_on=[*after, self.operator, *self.priority.resources]),
        )

        self.register_outputs({})
