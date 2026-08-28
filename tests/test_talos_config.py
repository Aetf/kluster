"""What the machine configuration must say.

These assertions are the design's load-bearing claims (declarative/physical.md
§2) in executable form: get any of them wrong and the cluster either does not
come up or comes up quietly insecure.
"""

import json
from ipaddress import IPv4Interface
from typing import Any, cast

from kluster import conventions
from kluster.physical import talos

SANS = ['203.0.113.10', 'api.example.test']
SECRETBOX = 'c2VjcmV0Ym94LWtleS1tYXRlcmlhbC0zMi1ieXRlcw=='
#: The gateway's LAN address, as the only party allowed to speak BGP.
PEER = '192.0.2.1/32'


def documents(**kwargs: Any) -> list[dict[str, Any]]:
    kwargs.setdefault('cert_sans', SANS)
    kwargs.setdefault('secretbox_secret', SECRETBOX)
    return [json.loads(patch) for patch in talos.patches(**kwargs)]


def deep_merge(into: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Talos' strategic merge, as far as these patches use it: maps merge, leaves win."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            deep_merge(cast('dict[str, Any]', into[key]), cast('dict[str, Any]', value))
        else:
            into[key] = value
    return into


def merged(section: str, **kwargs: Any) -> dict[str, Any]:
    """The v1alpha1 documents' `machine` or `cluster` section, as Talos merges them."""
    result: dict[str, Any] = {}
    for document in documents(**kwargs):
        if 'kind' not in document:
            deep_merge(result, document.get(section, {}))
    return result


def firewall(**kwargs: Any) -> list[dict[str, Any]]:
    return [document for document in documents(**kwargs) if str(document.get('kind', '')).startswith('Network')]


def test_kubespan_and_kubeprism_are_on() -> None:
    machine = merged('machine')
    assert machine['network']['kubespan']['enabled'] is True
    # No kube-proxy exists to fall back on.
    assert machine['features']['kubePrism']['enabled'] is True
    assert machine['features']['kubePrism']['port'] == conventions.KUBEPRISM_PORT


def test_the_cluster_is_dual_stack_ipv4_first() -> None:
    network = merged('cluster')['network']
    assert network['podSubnets'][0] == str(conventions.POD_CIDR_V4)
    assert network['serviceSubnets'][0] == str(conventions.SERVICE_CIDR_V4)
    assert ':' in network['podSubnets'][1]
    assert ':' in network['serviceSubnets'][1]


def test_cilium_installs_itself() -> None:
    # Talos must ship no CNI: nodes stay NotReady until k8s-base lands Cilium.
    assert talos.control_plane_patch(cert_sans=SANS)['cluster']['network']['cni'] == {'name': 'none'}


def test_control_planes_also_carry_workloads() -> None:
    assert talos.control_plane_patch(cert_sans=SANS)['cluster']['allowSchedulingOnControlPlanes'] is True


def test_the_public_apiserver_is_hardened() -> None:
    api_server = talos.control_plane_patch(cert_sans=SANS)['cluster']['apiServer']
    assert api_server['certSANs'] == SANS
    assert api_server['extraArgs']['anonymous-auth'] == 'false'
    assert 'audit-log-path' in api_server['extraArgs']


def test_the_kubelet_reserves_room_for_the_node() -> None:
    assert merged('machine')['kubelet']['extraConfig']['systemReserved'] == talos.SYSTEM_RESERVED


def test_kubernetes_secrets_are_encrypted_at_rest() -> None:
    # etcd sits in a $0-trust tenancy and its snapshots leave the site every
    # hour (architecture.md §6.5): unencrypted secrets there are the whole
    # cluster's credentials in someone else's storage.
    assert (
        talos.control_plane_patch(cert_sans=SANS, secretbox_secret=SECRETBOX)['cluster']['secretboxEncryptionSecret']
        == SECRETBOX
    )


def test_a_control_plane_without_a_key_says_nothing_about_encryption() -> None:
    # Naming an empty key would disable encryption where the generated
    # configuration would have enabled it; omitting the field leaves Talos'
    # own generated secret in place.
    assert 'secretboxEncryptionSecret' not in talos.control_plane_patch(cert_sans=SANS)['cluster']


def test_local_path_has_a_directory_to_hand_out() -> None:
    # The StorageClass is k8s-base's; the kubelet mount underneath it is
    # machine configuration (storage.md §2).
    mounts = merged('machine')['kubelet']['extraMounts']
    assert [mount['destination'] for mount in mounts] == [conventions.LOCAL_PATH_ROOT]
    assert mounts[0]['source'] == conventions.LOCAL_PATH_ROOT
    assert mounts[0]['type'] == 'bind'
    # Without shared propagation a volume mounted into the directory later is
    # invisible to pods that already have it.
    assert 'rshared' in mounts[0]['options']


def test_the_augmented_node_answers_for_its_second_address() -> None:
    # OCI assigns the secondary private IP to the VNIC and leaves the guest
    # alone; unconfigured, the dedicated VIP reaches nothing.
    interfaces = merged('machine', secondary_address='10.20.0.42')['network']['interfaces']
    assert interfaces[0]['addresses'] == ['10.20.0.42/32']
    assert interfaces[0]['dhcp'] is True


def test_a_node_nothing_addresses_is_left_to_its_lease() -> None:
    # Every cloud node: the platform gives it an address, and the machine
    # configuration says nothing about interfaces at all.
    assert 'interfaces' not in merged('machine').get('network', {})


def test_the_worker_states_its_own_address_instead_of_leasing_one() -> None:
    # The gateway's FRR neighbour statement, the qbittorrent port forward and
    # day 1's apid endpoint all name this address as a constant; a lease would
    # make each of them a guess — and the cluster VLAN runs no DHCP server to
    # offer one in any case (physical/homelab-host.md §2).
    static = talos.STATIC_ADDRESSES[conventions.HOMELAB_NODE]
    interface = merged('machine', role='worker', static_address=static)['network']['interfaces'][0]
    assert interface['addresses'] == [f'{conventions.HOMELAB_NODE_IPV4}/{conventions.CLUSTER_VLAN_V4.prefixlen}']
    assert interface['dhcp'] is False


def test_the_static_address_brings_the_routes_the_lease_used_to() -> None:
    static = talos.STATIC_ADDRESSES[conventions.HOMELAB_NODE]
    interface = merged('machine', role='worker', static_address=static)['network']['interfaces'][0]
    # The subnet route comes from the address carrying the VLAN's prefix
    # rather than /32 — with DHCP off there is no leased subnet route to
    # conflict with, and without one the node cannot reach its own subnet.
    assert IPv4Interface(interface['addresses'][0]).network == conventions.CLUSTER_VLAN_V4
    # Everything else was the lease's other job, and now has to be said. The
    # next hop is the gateway's own leg on the same VLAN, which is what makes
    # it reachable without a route to reach it by.
    gateway = conventions.CLUSTER_VLAN_GATEWAY_V4
    assert interface['routes'] == [{'network': talos.DEFAULT_ROUTE_V4, 'gateway': str(gateway)}]
    assert gateway in conventions.CLUSTER_VLAN_V4


def test_the_interface_is_selected_rather_than_named() -> None:
    # `eth0`/`ens3`/`enp1s0` is a property of the PCI topology QEMU builds and
    # of the kernel's naming policy; neither is this program's to decide.
    static = talos.STATIC_ADDRESSES[conventions.HOMELAB_NODE]
    interface = merged('machine', role='worker', static_address=static)['network']['interfaces'][0]
    assert interface['deviceSelector'] == {'physical': True}
    assert 'interface' not in interface


def test_a_worker_carries_no_control_plane_configuration() -> None:
    cluster = merged('cluster', role='worker')
    # A worker has no apiserver to harden and no etcd to encrypt; the CNI is
    # the control plane's business.
    assert 'apiServer' not in cluster
    assert 'etcd' not in cluster
    assert 'secretboxEncryptionSecret' not in cluster
    assert 'allowSchedulingOnControlPlanes' not in cluster
    # What it does share: the mesh, the subnets, and the kubelet's reservation.
    assert cluster['network']['podSubnets'][0] == str(conventions.POD_CIDR_V4)
    assert merged('machine', role='worker')['network']['kubespan']['enabled'] is True


def test_ingress_defaults_to_block_and_enumerates_host_ports_only() -> None:
    rules = firewall()
    assert rules[0] == {'apiVersion': 'v1alpha1', 'kind': 'NetworkDefaultActionConfig', 'ingress': 'block'}

    opened = {port for rule in rules[1:] for port in rule['portSelector']['ports']}
    assert opened == set(talos.HOST_PORTS)
    # Service ports are answered by the BPF datapath before nftables sees
    # them, so an app port here would be a cross-stack leak.
    assert not opened & {port for port, _ in conventions.PUBLIC_PORT_CENSUS}


def bgp_rules(**kwargs: Any) -> list[dict[str, Any]]:
    return [
        rule
        for rule in firewall(**kwargs)
        if rule['kind'] == 'NetworkRuleConfig' and talos.BGP_PORT in rule['portSelector']['ports']
    ]


def test_the_homelab_worker_takes_bgp_from_the_gateway_alone() -> None:
    rules = bgp_rules(role='worker', bgp_peer=PEER)
    assert len(rules) == 1
    # An open BGP port would let anything on the LAN inject routes into the
    # cluster's own address pools (cluster-infra.md §2).
    assert rules[0]['ingress'] == [{'subnet': PEER}]


def test_nobody_else_speaks_bgp() -> None:
    assert not bgp_rules()
