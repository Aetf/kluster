"""Cilium in the `k8s-base` program, declared against mocks.

The program runs whole, against a `physical` that has published its addresses
(`k8s_base_installation`), and the cases read what it handed the provider: the
chart's values, the Gateway API definitions, the Gateways' class and its
configuration, the three address pools, the baseline policy and the BGP session
to the gateway (rfc-007 §4, §5.1). The session's cases run it twice more, with
the session's password sealed in and as the stack stands until it is
(physical/bring-up.md §1).

Literals written here are the upstream projects' contracts: the values both
guides for Cilium on Talos prescribe, KubePrism's port, the addresses Talos
and the cloud fix, the label Talos puts on a control plane node, and the
families and the password's key as Cilium spells them. What the program
derives from a convention is held to that convention, and where the session
has a second end, to what the gateway's stack declares of it.

Every run is under the parent backstop `kluster.main` installs, so a resource
the component leaves unparented fails it here as it would in `pulumi
preview`.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio
from k8s_base_installation import (
    CHART,
    GATEWAY_API_DEFINITIONS,
    OUTPUTS,
    PUBLISHED,
    VERSIONS_CONFIG,
    Autonaming,
    Physical,
    Run,
    applied,
    declare,
    release_assets,
)
from mock_monitor import Declaration, declaring, run_under_backstop
from pulumi.output import UNKNOWN

from kluster import conventions
from kluster.components import talos
from kluster.components.gateway import routing
from kluster.conventions.cluster import POOL_WORKER
from kluster.lib.k8s import KUBECONFIG_KEY
from kluster.lib.stack_addresses import Held
from kluster.lib.versions import versions

CONFIG_GROUP = 'kubernetes:yaml/v2:ConfigGroup'
SERVICE = 'kubernetes:core/v1:Service'
CLASS_CONFIG = 'crds:cilium.io/v2alpha1:CiliumGatewayClassConfig'
GATEWAY_CLASS = 'crds:gateway.networking.k8s.io/v1:GatewayClass'
POOL = 'crds:cilium.io/v2:CiliumLoadBalancerIPPool'
CLUSTERWIDE_POLICY = 'crds:cilium.io/v2:CiliumClusterwideNetworkPolicy'
BGP_CLUSTER = 'crds:cilium.io/v2:CiliumBGPClusterConfig'
BGP_PEER = 'crds:cilium.io/v2:CiliumBGPPeerConfig'
BGP_ADVERTISEMENT = 'crds:cilium.io/v2:CiliumBGPAdvertisement'
GATEWAY = 'crds:gateway.networking.k8s.io/v1:Gateway'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'

#: The kinds whose definitions Cilium's operator registers once elected,
#: rather than the chart or the Gateway API's release asset carrying them.
OPERATOR_KINDS = (CLASS_CONFIG, POOL, CLUSTERWIDE_POLICY, BGP_CLUSTER, BGP_PEER, BGP_ADVERTISEMENT)

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


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The program against a `physical` that has published every output."""
    return await applied()


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
    """Kubernetes IPAM, the capability lists without `SYS_MODULE`, and Talos' own control groups.

    The agent's list is the guides' as they write it, followed by what the
    design adds to it, none of which is `SYS_MODULE`.
    """
    values = applied.values('cilium')
    capabilities = values['securityContext']['capabilities']

    assert values['ipam'] == {'mode': 'kubernetes'}
    assert capabilities['ciliumAgent'][: len(GUIDE_AGENT_CAPABILITIES)] == GUIDE_AGENT_CAPABILITIES
    assert 'SYS_MODULE' not in capabilities['ciliumAgent']
    assert capabilities['cleanCiliumState'] == GUIDE_CLEAN_STATE_CAPABILITIES
    assert values['cgroup'] == {'autoMount': {'enabled': False}, 'hostRoot': '/sys/fs/cgroup'}


def test_the_proxy_replacement_is_on_and_reaches_the_api_server_through_kubeprism(applied: Run) -> None:
    """With no kube-proxy, the agent reaches the API server through the node-local front on every node."""
    values = applied.values('cilium')

    assert values['kubeProxyReplacement'] is True
    assert (values['k8sServiceHost'], values['k8sServicePort']) == KUBEPRISM


def test_the_mtu_is_kubespans_link(applied: Run) -> None:
    """The underlying network's MTU, which the agent takes the tunnel's overhead off itself."""
    assert applied.values('cilium')['MTU'] == talos.kubespan_document()['mtu']
    assert applied.values('cilium')['routingMode'] == 'tunnel'


#: The feature switches the design rests on that nothing this suite declares
#: depends on in a way it can read: a BGP session the gateway's stack peers
#: with, and the tunnel the MTU above is sized for. Both are off the chart's
#: default, so a value dropped is a feature gone.
FEATURE_SWITCHES: dict[str, tuple[tuple[str, ...], object]] = {
    'bgp-control-plane': (('bgpControlPlane', 'enabled'), True),
    'tunnel-over-vxlan': (('tunnelProtocol',), 'vxlan'),
}

#: Where each metrics server listens, as the chart's defaults place them: the
#: agent's, the operator's and Envoy's, each switched by `prometheus.enabled`
#: under its own path.
METRICS_PORTS: dict[tuple[str, ...], int] = {
    ('prometheus',): 9962,
    ('operator', 'prometheus'): 9963,
    ('envoy', 'prometheus'): 9964,
}


@pytest.mark.parametrize('switch', FEATURE_SWITCHES)
def test_each_feature_the_design_rests_on_is_switched_as_it_needs(applied: Run, switch: str) -> None:
    path, expected = FEATURE_SWITCHES[switch]

    assert _at(applied.values('cilium'), path) == expected


def _at(values: object, path: tuple[str, ...]) -> object:
    for key in path:
        values = cast('dict[str, object]', values).get(key) if isinstance(values, dict) else None
    return values


def test_every_metrics_port_the_node_firewall_opens_has_its_server_switched_on(applied: Run) -> None:
    """A port the firewall opens to the pods with nothing behind it is a scrape that fails (physical.md §2)."""
    values = applied.values('cilium')
    opened = [path for path, port in METRICS_PORTS.items() if port in talos.POD_REACHED_PORTS]

    assert opened
    for path in opened:
        assert _at(values, (*path, 'enabled')) is True, path


def test_both_families_are_on_where_a_pool_hands_out_both(applied: Run) -> None:
    """A single-stack agent under a dual-stack pool and class leaves the v6 addresses unserved."""
    families = {
        ipaddress.ip_network(block['cidr']).version
        for pool in applied.monitor.of_type(POOL)
        for block in pool.inputs['spec']['blocks']
    }

    assert 6 in families
    assert _at(applied.values('cilium'), ('ipv6', 'enabled')) is True


def test_a_pod_detects_a_path_that_drops_what_is_too_big_on_its_own(applied: Run) -> None:
    """The chart's values state the `blackhole` mode, which a balancer front that relays no ICMP error leaves pods to.

    A pod sending to a client behind a smaller link learns of it from no
    packet-too-big (rfc-008 §4.1), so the kernel's own detection is what
    recovers the connection; the mode is the chart's default, and stating
    it keeps a moved default from taking it away.
    """
    assert applied.values('cilium')['pmtuDiscovery']['packetizationLayerPMTUDMode'] == 'blackhole'


def test_hubble_exports_flow_metrics(applied: Run) -> None:
    """Hubble's metrics server, which the node firewall opens to the scraper, serves only when some metric is named."""
    assert applied.values('cilium')['hubble']['metrics']['enabled']


def test_non_default_deny_policies_and_the_gateways_secret_sync_are_on(applied: Run) -> None:
    """Both are the chart's defaults; the baseline policy and the Gateways' certificates depend on them."""
    values = applied.values('cilium')

    assert values['enableNonDefaultDenyPolicies'] is True
    assert values['gatewayAPI']['enabled'] is True
    assert values['gatewayAPI']['secretsNamespace']['sync'] is True


def test_no_bpf_masquerading_no_legacy_routing_switch_and_no_egress_gateway(applied: Run) -> None:
    """None of them is set: each would take the host's `netfilter` out of a path the design depends on.

    Absence rather than `False`, because the chart's own defaults are the
    state the design describes, and a value set either way is a decision
    nobody wrote down.
    """
    values = applied.values('cilium')

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
    assert applied.values('cilium')['gatewayAPI']['gatewayClass']['create'] == 'false'


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


def _selects_its_own_name(run: Run, name: str) -> bool:
    """Whether a pool selects the Services labeled with its own name, under the one key every pool uses."""
    selectors = {
        it.inputs['metadata']['name']: it.inputs['spec']['serviceSelector'] for it in run.monitor.of_type(POOL)
    }
    keys = {key for selector in selectors.values() for key in selector['matchLabels']}
    (key,) = keys
    return selectors[name] == {'matchLabels': {key: name}}


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
    assert _selects_its_own_name(applied, conventions.POOL_INTERNET)


def test_the_lan_pool_is_its_range_and_selects_its_own_services(applied: Run) -> None:
    """Both families of the `lan` range, and the label that is not the `internet` pool's."""
    lan = conventions.LAN_POOL
    spec = _pool(applied, lan.name)

    assert sorted(ipaddress.ip_network(block['cidr']).version for block in spec['blocks']) == [4, 6]
    assert _selects_its_own_name(applied, lan.name)


def test_the_worker_pool_is_the_workers_two_addresses_and_selects_its_own_services(applied: Run) -> None:
    """The worker's IPv4, a convention, and the GUA `physical` published for it, one single-address block each.

    A `SiteForward` port's Service asks for them, since the site's forward and
    pinhole end at the worker's own address (rfc-008 §4.1); no other address
    belongs here, and the label is the pool's own.
    """
    spec = _pool(applied, POOL_WORKER)

    assert sorted(block['cidr'] for block in spec['blocks']) == sorted(
        [f'{conventions.HOMELAB_NODE_IPV4}/32', f'{PUBLISHED[OUTPUTS.worker_gua]}/128']
    )
    assert _selects_its_own_name(applied, POOL_WORKER)


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
    ('output', 'value', 'preview', 'held', 'pool'),
    [
        # The node GUAs a targeted apply of `physical` left as the sentinel:
        # unknown in a preview, absent in an update.
        (OUTPUTS.node_guas, UNKNOWN, True, Held.UNKNOWN, conventions.POOL_INTERNET),
        (OUTPUTS.node_guas, None, False, Held.ABSENT, conventions.POOL_INTERNET),
        (OUTPUTS.node_private_ips, {}, False, Held.EMPTY_MAPPING, conventions.POOL_INTERNET),
        (OUTPUTS.cluster_endpoint_v6, '203.0.113.10', False, Held.WRONG_FAMILY, conventions.POOL_INTERNET),
        (OUTPUTS.vip1_private, {}, False, Held.MAPPING, conventions.POOL_INTERNET),
        # Unset until the bring-up reads it off the worker's advertisement.
        (OUTPUTS.worker_gua, None, False, Held.ABSENT, POOL_WORKER),
        (OUTPUTS.worker_gua, '192.168.70.10', False, Held.WRONG_FAMILY, POOL_WORKER),
    ],
    ids=[
        'guas-unknown',
        'guas-absent',
        'node-ips-elided',
        'balancer-v6-wrong-family',
        'vip-elided',
        'worker-gua-absent',
        'worker-gua-wrong-family',
    ],
)
@pytest.mark.asyncio
async def test_an_output_that_is_not_an_address_stops_the_run_by_name(
    output: str, value: object, preview: bool, held: Held, pool: str
) -> None:
    """A pool is declared from nothing a StackReference can read back in place of an address.

    The read is answered here rather than by the mock monitor for `output`,
    because the mock drops an output that is `None` or unknown on its way to
    the reader. The pool reading it is never declared with what the output held.
    """
    original = pulumi.StackReference.get_output

    def read_back(reference: pulumi.StackReference, name: pulumi.Input[str]) -> pulumi.Output[Any]:
        return pulumi.Output.from_input(value) if name == output else original(reference, name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.StackReference, 'get_output', read_back)
        run = await declare(Physical(), preview=preview)

    assert run.refused is not None
    assert repr(output) in str(run.refused)
    assert (run.refused.output, run.refused.held) == (output, held)
    assert pool not in {it.inputs['metadata']['name'] for it in run.monitor.of_type(POOL)}


# -- The BGP session to the gateway --------------------------------------------

PASSWORD = conventions.sealed.BGP_PASSWORD

#: The ciphertext the sealed run's configuration holds for the session's
#: password.
SEALED_PASSWORD = 'AgB-the-sealed-bgp-password'

#: The label Talos puts on every control plane node (`node_label_spec.go` at
#: v1.13.9, L83–85), and the key Cilium reads a BGP password under
#: (`bgp-control-plane-configuration.rst` at v1.20.1, "MD5 Password").
TALOS_CONTROL_PLANE_LABEL = 'node-role.kubernetes.io/control-plane'
CILIUM_PASSWORD_KEY = 'password'

#: The lowest port the kernel lets a process bind without
#: `CAP_NET_BIND_SERVICE` (`net.ipv4.ip_unprivileged_port_start`'s default),
#: which Talos leaves as it is.
UNPRIVILEGED_PORT_FLOOR = 1024


async def _declared(*, sealed: dict[str, object] | None) -> tuple[Run, list[str]]:
    """The whole program with `sealed` as its sealed values, and every warning it logged."""
    from kluster.stacks import k8s_base

    config = {f'kluster:{KUBECONFIG_KEY}': 'a-fake-kubeconfig-that-reaches-no-cluster'}
    if sealed is not None:
        config[f'kluster:{conventions.sealed.CONFIG_KEY}'] = json.dumps(sealed)
    pulumi.runtime.set_all_config(config | VERSIONS_CONFIG, secret_keys=[f'kluster:{KUBECONFIG_KEY}'])
    monitor = await run_under_backstop(Autonaming(), stack=conventions.STACK_NAMES.k8s_base)
    warned: list[str] = []

    def warn(message: str, *_: object, **__: object) -> None:
        warned.append(message)

    with release_assets() as fetched, pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.log, 'warn', warn)
        async with declaring():
            await k8s_base.main()
    return Run(monitor, fetched), warned


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='unsealed')
async def unsealed_fixture() -> tuple[Run, list[str]]:
    """The stack as it stands before the session's password is sealed in."""
    return await _declared(sealed=None)


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='sealed')
async def sealed_fixture() -> Run:
    """The session's password sealed in."""
    run, _ = await _declared(sealed={PASSWORD.name: dict.fromkeys(PASSWORD.keys, SEALED_PASSWORD)})
    return run


def _one(run: Run, kind: str) -> dict[str, Any]:
    (declared,) = [it.inputs for it in run.monitor.of_type(kind)]
    return declared


def _matches(selector: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    """Whether a present label selector matches `labels`, as Kubernetes reads one: every requirement holds."""
    match_labels = cast('dict[str, str]', selector.get('matchLabels', {}))
    expressions = cast('list[dict[str, Any]]', selector.get('matchExpressions', []))
    return all(labels.get(key) == value for key, value in match_labels.items()) and all(
        _holds(expression, labels) for expression in expressions
    )


def _holds(expression: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    key = cast('str', expression['key'])
    values = cast('list[str]', expression.get('values', []))
    match expression['operator']:
        case 'In':
            return key in labels and labels[key] in values
        case 'NotIn':
            return key not in labels or labels[key] not in values
        case 'Exists':
            return key in labels
        case 'DoesNotExist':
            return key not in labels
        case operator:
            raise AssertionError(f'no such selector operator: {operator}')


def test_the_cluster_configuration_selects_the_worker_alone(applied: Run) -> None:
    """The one node that runs a BGP instance is the worker; no control plane node does.

    Each node carries the label Talos puts on a control plane node, or does
    not. An absent selector applies the configuration to every node.
    """
    nodes: dict[str, dict[str, str]] = {
        **{node: {TALOS_CONTROL_PLANE_LABEL: ''} for node in conventions.CLOUD_NODES},
        conventions.HOMELAB_NODE: {},
    }
    selector = _one(applied, BGP_CLUSTER)['spec'].get('nodeSelector')

    selected = {node for node, labels in nodes.items() if selector is None or _matches(selector, labels)}

    assert selected == {conventions.HOMELAB_NODE}


def test_the_session_is_the_one_the_gateways_routing_daemon_names(applied: Run) -> None:
    """The two ends agree: each side's ASN is the one the other expects, at the worker's own default route.

    Read off the configuration the gateway's stack renders for its routing
    daemon, `router bgp <its ASN>` and `neighbor <worker> remote-as <the
    worker's>`, so the session is external and both ends say so.
    """
    gateway = routing.frr_config(neighbor=conventions.HOMELAB_NODE_IPV4, password='not-the-password')
    gateway_asn = re.search(r'^router bgp (\d+)$', gateway, re.M)
    worker_asn = re.search(r'^ neighbor \S+ remote-as (\d+)$', gateway, re.M)
    assert gateway_asn is not None
    assert worker_asn is not None
    (instance,) = _one(applied, BGP_CLUSTER)['spec']['bgpInstances']
    (peer,) = instance['peers']

    assert instance['localASN'] == int(worker_asn[1])
    assert peer['peerASN'] == int(gateway_asn[1])
    assert instance['localASN'] != peer['peerASN']
    assert peer['peerAddress'] == str(talos.STATIC_ADDRESSES[conventions.HOMELAB_NODE].gateway)
    assert peer['peerConfigRef'] == {'name': _one(applied, BGP_PEER)['metadata']['name']}


def test_the_cluster_configuration_waits_for_the_peer_configuration_it_names(applied: Run) -> None:
    """The instance's peer names its configuration, which is declared first."""
    (cluster,) = applied.monitor.of_type(BGP_CLUSTER)
    (instance,) = cluster.inputs['spec']['bgpInstances']
    (peer,) = instance['peers']
    (named,) = [
        it for it in applied.monitor.of_type(BGP_PEER) if it.inputs['metadata']['name'] == peer['peerConfigRef']['name']
    ]

    assert applied.urn(BGP_PEER, named.name) in applied.monitor.depends_on(cluster.name, cluster.typ)


def test_the_worker_listens_on_the_port_its_firewall_opens_to_the_gateway(applied: Run) -> None:
    """A session the gateway opens is answered: the instance listens where the worker's firewall admits the gateway."""
    (instance,) = _one(applied, BGP_CLUSTER)['spec']['bgpInstances']
    (peer,) = instance['peers']
    documents = [
        json.loads(document)
        for document in talos.patches(installer='an-installer', role='worker', bgp_peer=f'{peer["peerAddress"]}/32')
    ]

    opened = [
        document
        for document in documents
        if document.get('kind') == 'NetworkRuleConfig'
        and document['portSelector'] == {'ports': [instance['localPort']], 'protocol': 'tcp'}
    ]

    assert [rule['ingress'] for rule in opened] == [[{'subnet': f'{peer["peerAddress"]}/32'}]]


def test_an_instance_listening_below_1024_has_the_agent_allowed_to_bind_it(applied: Run) -> None:
    """A BGP instance listens from inside the agent, which binds a port below the kernel's floor only with the capability.

    Talos leaves the unprivileged port floor at the kernel's 1024, and the
    agent runs with every capability dropped but the chart's list, so an
    instance on a lower port fails to start, and Cilium's manual names the
    capability that lets it (`bgp-control-plane-configuration.rst` at
    v1.20.1, L47–52).
    """
    privileged = [
        instance['localPort']
        for config in applied.monitor.of_type(BGP_CLUSTER)
        for instance in config.inputs['spec']['bgpInstances']
        if instance.get('localPort') is not None and instance['localPort'] < UNPRIVILEGED_PORT_FLOOR
    ]
    capabilities = applied.values('cilium')['securityContext']['capabilities']['ciliumAgent']

    assert not privileged or 'NET_BIND_SERVICE' in capabilities, privileged


def test_the_peer_carries_the_password_reference_and_both_families(sealed: Run) -> None:
    """The password's Secret is the one the sealed value becomes, where the agent reads it; both families advertise.

    The Secret is named and placed by the sealed value's row, and the agent
    reads BGP secrets from the namespace the chart's values name, so the two
    have to be the same namespace. Each family selects the advertisement.
    """
    spec = _one(sealed, BGP_PEER)['spec']
    (secret,) = [
        it.inputs for it in sealed.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == PASSWORD.name
    ]
    advertisement = _one(sealed, BGP_ADVERTISEMENT)['metadata']['labels']

    assert spec['authSecretRef'] == secret['metadata']['name']
    assert secret['metadata']['namespace'] == sealed.values('cilium')['bgpControlPlane']['secretsNamespace']['name']
    assert secret['spec']['encryptedData'] == {CILIUM_PASSWORD_KEY: SEALED_PASSWORD}
    assert sorted((family['afi'], family['safi']) for family in spec['families']) == [
        ('ipv4', 'unicast'),
        ('ipv6', 'unicast'),
    ]
    for family in spec['families']:
        assert _matches(family['advertisements'], advertisement), family


def test_the_advertisement_selects_the_lan_pools_services(applied: Run) -> None:
    """The load-balancer address of a Service is announced exactly when the `lan` pool hands it one.

    Held against the Services the Gateways make, which carry their
    infrastructure labels: `lan-gw` and `media-gw` are announced, `internet-gw`
    is not. An absent selector announces nothing.
    """
    (advertisement,) = _one(applied, BGP_ADVERTISEMENT)['spec']['advertisements']
    lan_pool = _pool(applied, conventions.LAN_POOL.name)['serviceSelector']
    services = {
        it.inputs['metadata']['name']: it.inputs['spec']['infrastructure']['labels']
        for it in applied.monitor.of_type(GATEWAY)
    }
    selector = advertisement.get('selector')

    announced = {name for name, labels in services.items() if selector is not None and _matches(selector, labels)}

    assert (advertisement['advertisementType'], advertisement['service']['addresses']) == (
        'Service',
        ['LoadBalancerIP'],
    )
    assert announced == {name for name, labels in services.items() if _matches(lan_pool, labels)}
    assert announced
    assert announced != set(services)


def test_an_absent_password_declares_no_sealed_secret_warns_naming_it_and_leaves_the_session_declared(
    unsealed: tuple[Run, list[str]],
) -> None:
    """Until the password is sealed in, nothing is declared for it, the run says so, and the session waits for it."""
    run, warned = unsealed

    assert [it for it in run.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == PASSWORD.name] == []
    assert [message for message in warned if repr(PASSWORD.name) in message]
    assert _one(run, BGP_PEER)['spec']['authSecretRef'] == PASSWORD.name
    assert run.monitor.of_type(BGP_CLUSTER)


def test_the_password_waits_for_the_sealing_controller(sealed: Run) -> None:
    """A SealedSecret is an instance of the sealing controller's chart's definition."""
    (secret,) = [it for it in sealed.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == PASSWORD.name]

    assert sealed.urn(CHART, 'sealed-secrets') in sealed.monitor.depends_on(secret.name, secret.typ)
