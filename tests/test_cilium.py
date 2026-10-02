"""Cilium in the `k8s-base` program, declared against mocks.

The program runs whole, against a `physical` that has published its addresses
(`k8s_base_installation`), and the cases read what it handed the provider: the
chart's values, the Gateway API definitions, the Gateways' class and its
configuration, the two address pools and the baseline policy (rfc-007 §4,
§5.1).

Literals written here are the upstream projects' contracts: the values both
guides for Cilium on Talos prescribe, KubePrism's port, and the addresses
Talos and the cloud fix. What the program derives from a convention is held to
that convention.

Every run is under the parent backstop `kluster.main` installs, so a resource
the component leaves unparented fails it here as it would in `pulumi
preview`.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio
from k8s_base_installation import (
    GATEWAY_API_DEFINITIONS,
    OUTPUTS,
    PUBLISHED,
    VERSIONS_CONFIG,
    Physical,
    release_assets,
)
from mock_monitor import Declaration, declaring, run_under_backstop
from pulumi.output import UNKNOWN

from kluster import conventions
from kluster.lib.k8s import KUBECONFIG_KEY
from kluster.lib.stack_addresses import UnusableAddressOutput
from kluster.lib.versions import ManifestPin, versions
from kluster.stacks import k8s_base

CHART = 'kubernetes:helm.sh/v4:Chart'
CONFIG_GROUP = 'kubernetes:yaml/v2:ConfigGroup'
SERVICE = 'kubernetes:core/v1:Service'
CLASS_CONFIG = 'crds:cilium.io/v2alpha1:CiliumGatewayClassConfig'
GATEWAY_CLASS = 'crds:gateway.networking.k8s.io/v1:GatewayClass'
POOL = 'crds:cilium.io/v2:CiliumLoadBalancerIPPool'
CLUSTERWIDE_POLICY = 'crds:cilium.io/v2:CiliumClusterwideNetworkPolicy'

#: The kinds whose definitions Cilium's operator registers once elected,
#: rather than the chart or the Gateway API's release asset carrying them.
OPERATOR_KINDS = (CLASS_CONFIG, POOL, CLUSTERWIDE_POLICY)

#: The agent's capabilities and the state-cleaning container's, exactly as
#: Cilium's guide for Talos writes them (`k8s-install-talos-linux.rst` at
#: v1.20.1, the `cilium-helm-install` block) and Talos' guide repeats.
GUIDE_AGENT_CAPABILITIES = [
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
]
GUIDE_CLEAN_STATE_CAPABILITIES = ['NET_ADMIN', 'SYS_ADMIN', 'SYS_RESOURCE']

#: KubePrism, the node-local API server front Talos runs on every node.
KUBEPRISM = ('localhost', 7445)

#: The cloud's metadata range, and the one address in it Talos answers pods'
#: DNS on (Talos host DNS).
METADATA_RANGE = '169.254.0.0/16'
TALOS_HOST_DNS = '169.254.116.108/32'


class Run:
    """One run of the program: what it registered, the pins it fetched, and the refusal that stopped it."""

    def __init__(self, monitor: Physical, fetched: list[ManifestPin], refused: UnusableAddressOutput | None) -> None:
        self.monitor = monitor
        self.fetched = fetched
        self.refused = refused

    def inputs(self, typ: str, name: str) -> dict[str, Any]:
        return self.monitor.inputs_of(name, typ)

    def values(self) -> dict[str, Any]:
        return self.inputs(CHART, 'cilium')['values']

    def urn(self, typ: str, name: str) -> str:
        return next(
            urn for urn, request in self.monitor.registrations.items() if (request.type, request.name) == (typ, name)
        )


async def declare(physical: Physical, *, preview: bool = False) -> Run:
    """The whole program against `physical`, a refusal caught and every registration settled."""
    config = {f'kluster:{KUBECONFIG_KEY}': 'a-fake-kubeconfig-that-reaches-no-cluster'}
    pulumi.runtime.set_all_config(config | VERSIONS_CONFIG, secret_keys=list(config))
    monitor = await run_under_backstop(physical, stack=conventions.STACK_NAMES.k8s_base, preview=preview)
    before = asyncio.all_tasks()
    refused = None
    with release_assets() as fetched:
        try:
            async with declaring():
                await k8s_base.main()
        except UnusableAddressOutput as error:
            refused = error
        _ = await asyncio.gather(*(asyncio.all_tasks() - before - {asyncio.current_task()}), return_exceptions=True)
    return Run(monitor, fetched, refused)


@pytest_asyncio.fixture(scope='module')
async def applied() -> Run:
    """The program against a `physical` that has published every output."""
    return await declare(Physical())


# -- The Gateway API definitions and the chart -------------------------------


def test_the_gateway_api_definitions_are_the_asset_their_pin_names(applied: Run) -> None:
    """The release asset is fetched by its pin, once, and what came back is what is applied."""
    assert [(pin.repository, pin.release, pin.asset) for pin in applied.fetched] == [
        ('example/gateway-api', 'v1.6.1', 'experimental-install.yaml')
    ]
    assert applied.inputs(CONFIG_GROUP, 'cilium-gateway-api')['yaml'] == GATEWAY_API_DEFINITIONS


def test_the_chart_is_installed_from_its_pin_after_the_definitions(applied: Run) -> None:
    """The chart reads its pin, and waits for the Gateway API definitions the operator looks for when it starts."""
    pin = versions.chart['cilium']
    chart = applied.inputs(CHART, 'cilium')

    assert (chart['chart'], chart['version']) == (pin.reference, pin.version)
    assert applied.urn(CONFIG_GROUP, 'cilium-gateway-api') in applied.monitor.depends_on('cilium', CHART)


def test_the_values_both_guides_prescribe_are_set_as_written(applied: Run) -> None:
    """Kubernetes IPAM, the capability lists without `SYS_MODULE`, and Talos' own control groups."""
    values = applied.values()

    assert values['ipam'] == {'mode': 'kubernetes'}
    assert values['securityContext']['capabilities'] == {
        'ciliumAgent': GUIDE_AGENT_CAPABILITIES,
        'cleanCiliumState': GUIDE_CLEAN_STATE_CAPABILITIES,
    }
    assert values['cgroup'] == {'autoMount': {'enabled': False}, 'hostRoot': '/sys/fs/cgroup'}


def test_the_proxy_replacement_is_on_and_reaches_the_api_server_through_kubeprism(applied: Run) -> None:
    """With no kube-proxy, the agent reaches the API server through the node-local front on every node."""
    values = applied.values()

    assert values['kubeProxyReplacement'] is True
    assert (values['k8sServiceHost'], values['k8sServicePort']) == KUBEPRISM


def test_the_mtu_is_kubespans_link(applied: Run) -> None:
    """The underlying network's MTU, which the agent takes the tunnel's overhead off itself."""
    assert applied.values()['MTU'] == conventions.KUBESPAN_MTU
    assert applied.values()['routingMode'] == 'tunnel'


#: The feature switches the design rests on, by their path in the chart's
#: values, and what each must be. Most are not the chart's default, so a value
#: dropped is a feature gone: a single-stack cluster under a dual-stack pool and
#: class, a BGP session with no control plane to hold it, and metrics ports the
#: node firewall opens (physical.md §2) with nothing behind them.
FEATURE_SWITCHES: dict[str, tuple[tuple[str, ...], object]] = {
    'both-families': (('ipv6', 'enabled'), True),
    'bgp-control-plane': (('bgpControlPlane', 'enabled'), True),
    'tunnel-over-vxlan': (('tunnelProtocol',), 'vxlan'),
    'agent-metrics': (('prometheus', 'enabled'), True),
    'operator-metrics': (('operator', 'prometheus', 'enabled'), True),
    'envoy-metrics': (('envoy', 'prometheus', 'enabled'), True),
    'hubble-relay-off': (('hubble', 'relay', 'enabled'), False),
    'hubble-ui-off': (('hubble', 'ui', 'enabled'), False),
}


@pytest.mark.parametrize('switch', FEATURE_SWITCHES)
def test_each_feature_the_design_rests_on_is_switched_as_it_needs(applied: Run, switch: str) -> None:
    path, expected = FEATURE_SWITCHES[switch]
    value: object = applied.values()
    for key in path:
        value = cast('dict[str, object]', value).get(key) if isinstance(value, dict) else None

    assert value == expected


def test_hubble_exports_flow_metrics(applied: Run) -> None:
    """Hubble's metrics server, which the node firewall opens to the scraper, serves only when some metric is named."""
    assert applied.values()['hubble']['metrics']['enabled']


def test_non_default_deny_policies_and_the_gateways_secret_sync_are_on(applied: Run) -> None:
    """Both are the chart's defaults; the baseline policy and the Gateways' certificates depend on them."""
    values = applied.values()

    assert values['enableNonDefaultDenyPolicies'] is True
    assert values['gatewayAPI']['enabled'] is True
    assert values['gatewayAPI']['secretsNamespace']['sync'] is True


def test_no_bpf_masquerading_no_legacy_routing_switch_and_no_egress_gateway(applied: Run) -> None:
    """None of them is set: each would take the host's `netfilter` out of a path the design depends on.

    Absence rather than `False`, because the chart's own defaults are the
    state the design describes, and a value set either way is a decision
    nobody wrote down.
    """
    values = applied.values()

    assert 'masquerade' not in values.get('bpf', {})
    assert 'hostLegacyRouting' not in values.get('bpf', {})
    assert 'egressGateway' not in values


# -- The Gateways' class -------------------------------------------------------


def test_the_class_configuration_states_cluster_no_node_ports_and_both_families(applied: Run) -> None:
    """The declared fields, read as declared: the definition would default the policy the same way."""
    service = applied.inputs(CLASS_CONFIG, 'cilium-gateway-class')['spec']['service']

    assert service['externalTrafficPolicy'] == 'Cluster'
    assert service['allocateLoadBalancerNodePorts'] is False
    assert service['ipFamilyPolicy'] == 'RequireDualStack'
    assert sorted(service['ipFamilies']) == ['IPv4', 'IPv6']


def test_the_gateway_class_names_its_configuration_and_the_chart_declares_no_other(applied: Run) -> None:
    """The class is the one the Gateways name, and it carries the configuration the chart's own class lacks."""
    config = applied.inputs(CLASS_CONFIG, 'cilium-gateway-class')['metadata']
    spec = applied.inputs(GATEWAY_CLASS, 'cilium-gateway-class')['spec']

    assert spec['controllerName'] == 'io.cilium/gateway-controller'
    assert spec['parametersRef'] == {
        'group': 'cilium.io',
        'kind': 'CiliumGatewayClassConfig',
        'name': config['name'],
        'namespace': config['namespace'],
    }
    assert applied.values()['gatewayAPI']['gatewayClass']['create'] == 'false'


def test_every_service_on_the_node_addresses_states_cluster(applied: Run) -> None:
    """Every Service the stack puts on the `internet` pool, and every Service the Gateways' class makes, states `Cluster`.

    LB IPAM shares an address only between Services of one policy (rfc-007
    §4.4). The Gateways' Services are the class configuration's to make, so
    the configuration counts as one.
    """
    internet = {conventions.LB_POOL_LABEL: conventions.POOL_INTERNET}
    services = [
        declaration.inputs['spec']
        for declaration in applied.monitor.of_type(SERVICE)
        if internet.items() <= declaration.inputs.get('metadata', {}).get('labels', {}).items()
    ]
    services += [declaration.inputs['spec']['service'] for declaration in applied.monitor.of_type(CLASS_CONFIG)]

    assert services
    assert {service.get('externalTrafficPolicy') for service in services} == {'Cluster'}


def test_the_gateway_class_waits_for_its_definition_and_its_configuration(applied: Run) -> None:
    """The class's definition is the Gateway API asset's, and the class names a configuration that must exist first.

    Sent before either, the class's create is an unknown kind or a class
    pointing at nothing, which only the provider's retry would get past.
    """
    after = applied.monitor.depends_on('cilium-gateway-class', GATEWAY_CLASS)

    assert applied.urn(CONFIG_GROUP, 'cilium-gateway-api') in after
    assert applied.urn(CLASS_CONFIG, 'cilium-gateway-class') in after


# -- The pools -----------------------------------------------------------------


def _pool(run: Run, name: str) -> dict[str, Any]:
    (spec,) = [it.inputs['spec'] for it in run.monitor.of_type(POOL) if it.inputs['metadata']['name'] == name]
    return spec


def test_the_internet_pool_is_the_node_addresses_the_vip_and_the_balancers_two(applied: Run) -> None:
    """One single-address block per member, and no other address: a node's public IPv4 is never one.

    The members are what `physical` published under the node, VIP and balancer
    outputs; the published fixture also carries the nodes' public IPv4s and
    the reserved address, which a pool built from every address would hold.
    """
    published = PUBLISHED
    expected = {
        *(f'{address}/32' for address in cast('dict[str, str]', published[OUTPUTS.node_private_ips]).values()),
        *(f'{address}/128' for address in cast('dict[str, str]', published[OUTPUTS.node_guas]).values()),
        f'{published[OUTPUTS.vip1_private]}/32',
        f'{published[OUTPUTS.cluster_endpoint]}/32',
        f'{published[OUTPUTS.cluster_endpoint_v6]}/128',
    }
    spec = _pool(applied, conventions.POOL_INTERNET)

    assert [block.keys() for block in spec['blocks']] == [{'cidr'}] * len(spec['blocks'])
    assert sorted(block['cidr'] for block in spec['blocks']) == sorted(expected)
    assert spec['serviceSelector'] == {'matchLabels': {conventions.LB_POOL_LABEL: conventions.POOL_INTERNET}}


def test_the_lan_pool_is_its_range_and_selects_its_own_services(applied: Run) -> None:
    """Both families of the `lan` range, and the label that is not the `internet` pool's."""
    lan = conventions.LAN_POOL
    spec = _pool(applied, lan.name)

    assert sorted(block['cidr'] for block in spec['blocks']) == sorted([str(lan.v4), str(lan.v6)])
    assert ipaddress.ip_network(str(lan.v6)).version == 6
    assert spec['serviceSelector'] == {'matchLabels': {conventions.LB_POOL_LABEL: lan.name}}


# -- The baseline policy -------------------------------------------------------


def test_the_baseline_denies_the_metadata_range_but_the_host_dns_address(applied: Run) -> None:
    """Every pod is denied the range the metadata service answers in, except the address it resolves names through."""
    spec = applied.inputs(CLUSTERWIDE_POLICY, 'cilium-baseline')['spec']

    assert spec['endpointSelector'] == {}
    assert spec['egressDeny'] == [{'toCIDRSet': [{'cidr': METADATA_RANGE, 'except': [TALOS_HOST_DNS]}]}]


def test_the_baseline_switches_default_deny_off_in_both_directions(applied: Run) -> None:
    """A deny rule alone would switch every selected pod to default-deny egress, so the policy says otherwise."""
    spec = applied.inputs(CLUSTERWIDE_POLICY, 'cilium-baseline')['spec']

    assert spec['enableDefaultDeny'] == {'egress': False, 'ingress': False}


def test_what_the_operator_defines_waits_for_the_chart(applied: Run) -> None:
    """An instance of a kind the operator registers is declared after the chart that installs the operator."""
    chart = applied.urn(CHART, 'cilium')
    instances: list[Declaration] = [it for kind in OPERATOR_KINDS for it in applied.monitor.of_type(kind)]

    assert instances
    for instance in instances:
        assert chart in applied.monitor.depends_on(instance.name, instance.typ), instance.name


# -- What `physical` hands over ----------------------------------------------


@pytest.mark.parametrize(
    ('output', 'value', 'preview', 'named'),
    [
        # The node GUAs a targeted apply of `physical` left as the sentinel:
        # unknown in a preview, absent in an update.
        (OUTPUTS.node_guas, UNKNOWN, True, 'unknown'),
        (OUTPUTS.node_guas, None, False, 'absent'),
        (OUTPUTS.node_private_ips, {}, False, 'empty mapping'),
        (OUTPUTS.cluster_endpoint_v6, '203.0.113.10', False, 'an IPv4 address'),
        (OUTPUTS.vip1_private, {}, False, 'a mapping'),
    ],
    ids=['guas-unknown', 'guas-absent', 'node-ips-elided', 'balancer-v6-wrong-family', 'vip-elided'],
)
@pytest.mark.asyncio
async def test_an_output_that_is_not_an_address_stops_the_run_by_name(
    output: str, value: object, preview: bool, named: str
) -> None:
    """The pool is declared from nothing a StackReference can read back in place of an address.

    The read is answered here rather than by the mock monitor for `output`,
    because the mock drops an output that is `None` or unknown on its way to
    the reader. The pool is never declared with what the output held.
    """
    original = pulumi.StackReference.get_output

    def read_back(reference: pulumi.StackReference, name: pulumi.Input[str]) -> pulumi.Output[Any]:
        return pulumi.Output.from_input(value) if name == output else original(reference, name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.StackReference, 'get_output', read_back)
        run = await declare(Physical(), preview=preview)

    assert run.refused is not None
    assert repr(output) in str(run.refused)
    assert named in str(run.refused)
    assert conventions.POOL_INTERNET not in {it.inputs['metadata']['name'] for it in run.monitor.of_type(POOL)}
