"""Cilium: the datapath, the Gateway API's definitions and class, the address pools and the baseline policy.

The installation is the one Cilium's and Talos' guides both document for Talos,
and every value beyond theirs is one the design needs (rfc-007 §4.1,
cluster-infra.md §2). What the machine configuration carries of the same
installation -- no CNI of Talos' own, no kube-proxy, host DNS forwarding and
KubeSpan's MTU -- is the `physical` stack's Talos component.

Declared in the order the cluster needs them in, which the dependencies carry:
the Gateway API definitions, a release asset rather than a chart, then the
chart, which installs the agent and the operator; then the objects whose
definitions the operator registers once elected -- the class configuration,
the two address pools and the baseline policy. The `GatewayClass`'s own
definition is the asset's, so it waits for that and for its configuration.

The Gateways themselves, their certificates and the BGP session are later
pieces of the same component (rfc-007 §14).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.lib.k8s import helm_chart, lb_pool_labels
from kluster.lib.versions import ChartPin
from putils import Component, async_output, resolve

__all__ = (
    'AGENT_CAPABILITIES',
    'CLEAN_STATE_CAPABILITIES',
    'GATEWAY_CLASS',
    'HOST_DNS_ADDRESS',
    'METADATA_RANGE',
    'NAMESPACE',
    'Cilium',
    'InternetPoolMembers',
    'chart_values',
)

#: Where the chart installs, which is where both guides install it: under
#: Talos' Pod Security exemption, which the agent's host access needs
#: (cluster-infra.md §0).
NAMESPACE = 'kube-system'

#: The one `GatewayClass` every Gateway names (rfc-007 §5.1), declared here
#: rather than by the chart so that it can carry its configuration: the chart's
#: own class has no `parametersRef`, so it is switched off below.
GATEWAY_CLASS = 'cilium'

#: The controller the class hands its Gateways to, which Cilium's operator
#: answers to.
GATEWAY_CONTROLLER = 'io.cilium/gateway-controller'

#: The agent's capabilities as both guides list them: the chart's default list
#: without `SYS_MODULE`, since Talos lets no workload load a kernel module, and
#: with what the agent needs in its place.
AGENT_CAPABILITIES = (
    'CHOWN',
    'KILL',
    'NET_ADMIN',
    'NET_RAW',
    'IPC_LOCK',
    'SYS_ADMIN',
    'SYS_RESOURCE',
    'DAC_OVERRIDE',
    'FOWNER',
    'SETGID',
    'SETUID',
)

#: The state-cleaning init container's capabilities, as both guides list them.
CLEAN_STATE_CAPABILITIES = ('NET_ADMIN', 'SYS_ADMIN', 'SYS_RESOURCE')

#: Where Talos mounts the control-group hierarchy, which the agent reuses
#: rather than mounting its own.
CGROUP_ROOT = '/sys/fs/cgroup'

#: The Hubble metrics this installation exports: the flow-level ones, which
#: need no L7 visibility a policy would have to switch on.
HUBBLE_METRICS = ('drop', 'tcp', 'flow', 'icmp')

#: The link-local range the cloud's metadata service answers in, which serves
#: every cloud node its machine configuration (security-audit.md H1).
METADATA_RANGE = ipaddress.IPv4Network('169.254.0.0/16')

#: The address Talos' host DNS answers pods on, which the cluster DNS forwards
#: to and which sits inside `METADATA_RANGE` (rfc-007 §4.5). Talos fixes it.
HOST_DNS_ADDRESS = ipaddress.IPv4Network('169.254.116.108/32')


def chart_values() -> dict[str, Any]:
    """The chart's values: both guides' installation for Talos, and what the design adds to it.

    What is left unset is as deliberate as what is set. BPF masquerading, the
    legacy host-routing switch and the Egress Gateway stay at their defaults,
    off: BPF masquerading would bring BPF host routing, which bypasses the
    host's `netfilter` that the node firewall and KubeSpan's steering both
    live in, and both upstreams answer that with the legacy switch; the Egress
    Gateway refuses to start without BPF masquerading (rfc-007 §4.1).
    """
    return {
        # Both guides' values, as they write them.
        'ipam': {'mode': 'kubernetes'},
        'kubeProxyReplacement': True,
        'k8sServiceHost': 'localhost',
        'k8sServicePort': conventions.KUBEPRISM_PORT,
        'securityContext': {
            'capabilities': {
                'ciliumAgent': list(AGENT_CAPABILITIES),
                'cleanCiliumState': list(CLEAN_STATE_CAPABILITIES),
            },
        },
        'cgroup': {'autoMount': {'enabled': False}, 'hostRoot': CGROUP_ROOT},
        # Tunnel routing over KubeSpan, sized for KubeSpan's link: the agent
        # subtracts the tunnel's overhead from the MTU it is given, and the
        # link the tunnel crosses is selected by a firewall mark, which
        # detection would miss.
        'routingMode': 'tunnel',
        'tunnelProtocol': 'vxlan',
        'MTU': conventions.KUBESPAN_MTU,
        # The baseline policy leaves default-deny off, which only this lets a
        # policy do; without it the baseline would deny every pod all egress.
        'enableNonDefaultDenyPolicies': True,
        'ipv4': {'enabled': True},
        'ipv6': {'enabled': True},
        'gatewayAPI': {
            'enabled': True,
            'gatewayClass': {'create': 'false'},
            'secretsNamespace': {'sync': True},
        },
        'bgpControlPlane': {'enabled': True},
        # Off, so a `Local` Service's health check port gets no frontend on
        # its load-balancer address, which on the `internet` pool is a node's
        # own address, ahead of the node firewall (cluster-infra.md §2).
        'nodePort': {'enableHealthCheckLoadBalancerIP': False},
        'prometheus': {'enabled': True},
        'operator': {'prometheus': {'enabled': True}},
        'envoy': {'prometheus': {'enabled': True}},
        'hubble': {
            'metrics': {'enabled': list(HUBBLE_METRICS)},
            'relay': {'enabled': False},
            'ui': {'enabled': False},
        },
    }


@dataclass(frozen=True, kw_only=True)
class InternetPoolMembers:
    """The addresses the `internet` pool is made of, one class per field (rfc-007 §4.4).

    Each is a machine fact the `physical` stack publishes, so each arrives as
    an output of its own. The node addresses are the on-the-wire forms
    traffic from outside arrives at; the balancer's are answered for
    connections that start inside the cluster (architecture.md §3.2).
    """

    node_private_ips: pulumi.Input[Mapping[str, str]]
    """Cloud node name → its primary private IPv4, which OCI 1:1-NATs its public IPv4 onto."""
    node_guas: pulumi.Input[Mapping[str, str]]
    """Cloud node name → its GUA."""
    dedicated_vip: pulumi.Input[str]
    """The secondary private IPv4 the reserved public address is 1:1-NATed onto."""
    balancer_v4: pulumi.Input[str]
    """The balancer's public IPv4."""
    balancer_v6: pulumi.Input[str]
    """The balancer's public IPv6."""


class Cilium(Component, pulumi_type='kluster:cilium:Cilium'):
    """The CNI, the Gateway API's definitions, the Gateways' class, the address pools and the baseline policy."""

    def __init__(
        self,
        name: str,
        *,
        chart: ChartPin,
        gateway_api_definitions: pulumi.Input[str],
        internet_pool: InternetPoolMembers,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self._internet_pool = internet_pool

        self.gateway_api = k8s.yaml.v2.ConfigGroup(
            f'{name}-gateway-api', yaml=gateway_api_definitions, opts=self.child_opts()
        )
        self.chart = helm_chart(
            name,
            pin=chart,
            namespace=NAMESPACE,
            values=chart_values(),
            opts=self.child_opts(depends_on=[self.gateway_api]),
        )
        # Everything below is an instance of a definition the operator
        # registers once elected, rather than one the chart carries. Waiting
        # for the chart is what orders them, and it is not proof the
        # definitions exist: the operator's readiness checks only its kvstore
        # and the API server, and its leader registers the definitions first
        # among its leader cells. The pools' and the baseline's definitions
        # are among those the agent blocks on at start, and the chart's await
        # waits for the agent; the class configuration's is registered right
        # after that set, and a create of a kind the server does not yet know
        # is retried with backoff.
        after_the_chart = self.child_opts(depends_on=[self.chart])

        self.class_config = crds.cilium.v2alpha1.CiliumGatewayClassConfig(
            f'{name}-gateway-class',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=GATEWAY_CLASS, namespace=NAMESPACE),
            spec=crds.cilium.v2alpha1.CiliumGatewayClassConfigSpecArgs(
                service=crds.cilium.v2alpha1.CiliumGatewayClassConfigSpecServiceArgs(
                    # Stated, though the configuration's own default is the
                    # same: a configuration that sets any field overrides the
                    # chart's policy too, so the policy is a decision here
                    # rather than a side effect (rfc-007 §5.1).
                    external_traffic_policy='Cluster',
                    allocate_load_balancer_node_ports=False,
                    ip_family_policy='RequireDualStack',
                    ip_families=['IPv4', 'IPv6'],
                ),
            ),
            opts=after_the_chart,
        )
        self.gateway_class = crds.gateway.v1.GatewayClass(
            f'{name}-gateway-class',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=GATEWAY_CLASS),
            spec=crds.gateway.v1.GatewayClassSpecArgs(
                controller_name=GATEWAY_CONTROLLER,
                parameters_ref=crds.gateway.v1.GatewayClassSpecParametersRefArgs(
                    group='cilium.io',
                    kind='CiliumGatewayClassConfig',
                    name=GATEWAY_CLASS,
                    namespace=NAMESPACE,
                ),
            ),
            opts=self.child_opts(depends_on=[self.gateway_api, self.class_config]),
        )

        self.internet_pool = crds.cilium.v2.CiliumLoadBalancerIPPool(
            f'{name}-{conventions.POOL_INTERNET}',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=conventions.POOL_INTERNET),
            spec=crds.cilium.v2.CiliumLoadBalancerIPPoolSpecArgs(
                blocks=async_output(self._internet_blocks),
                service_selector=crds.cilium.v2.CiliumLoadBalancerIPPoolSpecServiceSelectorArgs(
                    match_labels=lb_pool_labels(conventions.POOL_INTERNET)
                ),
            ),
            opts=after_the_chart,
        )
        lan = conventions.LAN_POOL
        self.lan_pool = crds.cilium.v2.CiliumLoadBalancerIPPool(
            f'{name}-{lan.name}',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=lan.name),
            spec=crds.cilium.v2.CiliumLoadBalancerIPPoolSpecArgs(
                blocks=[
                    crds.cilium.v2.CiliumLoadBalancerIPPoolSpecBlocksArgs(cidr=str(lan.v4)),
                    crds.cilium.v2.CiliumLoadBalancerIPPoolSpecBlocksArgs(cidr=str(lan.v6)),
                ],
                service_selector=crds.cilium.v2.CiliumLoadBalancerIPPoolSpecServiceSelectorArgs(
                    match_labels=lb_pool_labels(lan.name)
                ),
            ),
            opts=after_the_chart,
        )

        self.baseline = crds.cilium.v2.CiliumClusterwideNetworkPolicy(
            f'{name}-baseline',
            metadata=k8s.meta.v1.ObjectMetaArgs(name='baseline'),
            spec=crds.cilium.v2.CiliumClusterwideNetworkPolicySpecArgs(
                description=(
                    "Deny every pod the cloud metadata service's range, which serves each cloud node its machine "
                    'configuration, except the address Talos answers DNS on.'
                ),
                # Every pod; a node's own endpoint is selected by a node
                # selector, which this leaves out.
                endpoint_selector=crds.cilium.v2.CiliumClusterwideNetworkPolicySpecEndpointSelectorArgs(),
                egress_deny=[
                    crds.cilium.v2.CiliumClusterwideNetworkPolicySpecEgressDenyArgs(
                        to_cidr_set=[
                            crds.cilium.v2.CiliumClusterwideNetworkPolicySpecEgressDenyToCIDRSetArgs(
                                cidr=str(METADATA_RANGE), except_=[str(HOST_DNS_ADDRESS)]
                            )
                        ]
                    )
                ],
                # A deny rule alone switches its direction to default-deny,
                # which would deny every pod all egress; default-deny is the
                # per-namespace policies' to impose (workloads.md §1).
                enable_default_deny=crds.cilium.v2.CiliumClusterwideNetworkPolicySpecEnableDefaultDenyArgs(
                    egress=False, ingress=False
                ),
            ),
            opts=after_the_chart,
        )

        self.register_outputs({})

    async def _internet_blocks(self) -> list[crds.cilium.v2.CiliumLoadBalancerIPPoolSpecBlocksArgs]:
        """One block per member address, each a single address: a node's, the dedicated VIP's or the balancer's."""
        members = self._internet_pool
        node_private_ips, node_guas, dedicated_vip, balancer_v4, balancer_v6 = await resolve(
            members.node_private_ips,
            members.node_guas,
            members.dedicated_vip,
            members.balancer_v4,
            members.balancer_v6,
        )
        addresses = [
            *(node_private_ips[node] for node in sorted(node_private_ips)),
            *(node_guas[node] for node in sorted(node_guas)),
            dedicated_vip,
            balancer_v4,
            balancer_v6,
        ]
        return [
            crds.cilium.v2.CiliumLoadBalancerIPPoolSpecBlocksArgs(cidr=str(ipaddress.ip_network(address)))
            for address in addresses
        ]
