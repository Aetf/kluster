"""What the machine configuration must say.

These assertions are the design's load-bearing claims (declarative/physical.md
§2) in executable form: get any of them wrong and the cluster either does not
come up or comes up quietly insecure.
"""

import json
from ipaddress import IPv4Interface
from typing import Any, cast

import pytest

from kluster import conventions
from kluster.components import talos

SANS = ['203.0.113.10', 'api.example.test']
SECRETBOX = 'c2VjcmV0Ym94LWtleS1tYXRlcmlhbC0zMi1ieXRlcw=='
#: The gateway's LAN address, as the only party allowed to speak BGP.
PEER = '192.0.2.1/32'
#: A node volume's name. Not a census row: what is held here is how any name
#: renders, and which rows exist is `conventions`' to say.
VOLUME = 'example-data'


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


def of_kind(kind: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Every document of one kind a node's patches carry."""
    return [document for document in documents(**kwargs) if document.get('kind') == kind]


def node_volumes(**kwargs: Any) -> list[dict[str, Any]]:
    """The node volumes a node's patches mount: its `partition` user volumes."""
    return [volume for volume in of_kind('UserVolumeConfig', **kwargs) if volume['volumeType'] == 'partition']


def uplink(**kwargs: Any) -> dict[str, Any]:
    """The one `LinkConfig` a node's patches carry, and the alias it names."""
    (link,) = of_kind('LinkConfig', **kwargs)
    (alias,) = of_kind('LinkAliasConfig', **kwargs)
    assert link['name'] == alias['name'] == talos.UPLINK
    return link


#: The node shapes the component renders: a plain control plane, the control
#: plane holding the dedicated VIP, a control plane carrying a node volume, and
#: the homelab worker with its own address and its BGP peer.
SHAPES: dict[str, dict[str, Any]] = {
    'control-plane': {},
    'dedicated-vip': {'secondary_address': '10.20.0.42'},
    'node-volume': {'volume': VOLUME},
    'homelab-worker': {
        'role': 'worker',
        'static_address': talos.STATIC_ADDRESSES[conventions.HOMELAB_NODE],
        'bgp_peer': PEER,
    },
}

#: What the pinned Talos release (`versions:talos` in Pulumi.yaml, v1.13)
#: deprecates among the fields this component has used, by path into the
#: `v1alpha1` document. Every field of `machine.network` is deprecated there
#: for the multi-document network configuration, so the section itself is
#: refused as well as the two fields named. A pin that moves to a later
#: minor adds what that release deprecates here. v1.14 deprecates much of
#: `machine` and `cluster`, `machine.kubelet` as a whole among it and
#: `machine.nodeLabels` for a `KubeNodeConfig` document v1.13 does not have;
#: that move waits on a provider built on v1.14's machinery
#: (Aetf/kluster-ops#475).
DEPRECATED = [
    ('machine', 'network'),
    ('machine', 'network', 'kubespan'),
    ('machine', 'network', 'interfaces'),
]

#: Where a kubelet mount would sit in `v1alpha1`.
KUBELET_MOUNTS = ('machine', 'kubelet', 'extraMounts')


def holds(document: dict[str, Any], path: tuple[str, ...]) -> bool:
    node: Any = document
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = cast('dict[str, Any]', node)[key]
    return True


@pytest.mark.parametrize('path', DEPRECATED, ids=['.'.join(path) for path in DEPRECATED])
@pytest.mark.parametrize('shape', list(SHAPES))
def test_no_patch_carries_a_field_the_pinned_release_deprecates(shape: str, path: tuple[str, ...]) -> None:
    assert [document for document in documents(**SHAPES[shape]) if holds(document, path)] == []


def test_kubespan_and_kubeprism_are_on() -> None:
    # KubeSpan in its own document, with nothing but `enabled` stated: every
    # other setting is Talos' default, and the document's defaults are the
    # ones `machine.network.kubespan` has.
    assert of_kind('KubeSpanConfig') == [{'apiVersion': 'v1alpha1', 'kind': 'KubeSpanConfig', 'enabled': True}]
    machine = merged('machine')
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
    # etcd sits in a $0-trust tenancy and its snapshots are designed to leave
    # the site every hour (storage.md §5, nodes.md §5 Tier 0): unencrypted
    # secrets there are the whole cluster's credentials in someone else's
    # storage.
    assert (
        talos.control_plane_patch(cert_sans=SANS, secretbox_secret=SECRETBOX)['cluster']['secretboxEncryptionSecret']
        == SECRETBOX
    )


def test_a_control_plane_without_a_key_says_nothing_about_encryption() -> None:
    # Naming an empty key would disable encryption where the generated
    # configuration would have enabled it; omitting the field leaves Talos'
    # own generated secret in place.
    assert 'secretboxEncryptionSecret' not in talos.control_plane_patch(cert_sans=SANS)['cluster']


@pytest.mark.parametrize('shape', list(SHAPES))
def test_local_path_hands_out_a_user_volume_and_no_kubelet_mount(shape: str) -> None:
    # The StorageClass is k8s-base's; the directory underneath it is machine
    # configuration (storage.md §2). The kubelet sees `/var/mnt` read-only, so
    # the directory has to be a mount of its own there: a `directory` user
    # volume, which Talos mounts at its root under the volume's name. Exactly
    # one, on every node, and nothing but its type stated, since Talos refuses
    # a disk, a filesystem, encryption or mount options for that type.
    directories = [
        volume for volume in of_kind('UserVolumeConfig', **SHAPES[shape]) if volume['volumeType'] == 'directory'
    ]
    assert [f'{conventions.USER_VOLUME_ROOT}/{volume["name"]}' for volume in directories] == [
        conventions.LOCAL_PATH_ROOT
    ]
    assert set(directories[0]) == {'apiVersion', 'kind', 'name', 'volumeType'}
    # A host path a pod reaches is a user volume, and never a kubelet mount
    # (physical.md §2): a base generated for Talos v1.14 carries a
    # `KubeletConfig`, beside which any `machine.kubelet` is refused.
    assert [document for document in documents(**SHAPES[shape]) if holds(document, KUBELET_MOUNTS)] == []


def test_the_node_holding_the_dedicated_vip_answers_for_its_second_address() -> None:
    # OCI assigns the secondary private IP to the VNIC and leaves the guest
    # alone; unconfigured, the dedicated VIP reaches nothing.
    link = uplink(secondary_address='10.20.0.42')
    assert link['addresses'] == [{'address': '10.20.0.42/32'}]
    # The address is all the link document says: no route and no MTU of its
    # own, so the lease's routes and the link's MTU stand.
    assert set(link) == {'apiVersion', 'kind', 'name', 'addresses'}


def test_the_node_holding_the_dedicated_vip_keeps_its_lease() -> None:
    # Configuring the link switches off Talos' default DHCP, so the lease is
    # stated: IPv4 on the same link, with no client identifier, which is the
    # request a `dhcp: true` interface makes.
    assert of_kind('DHCPv4Config', secondary_address='10.20.0.42') == [
        {'apiVersion': 'v1alpha1', 'kind': 'DHCPv4Config', 'name': talos.UPLINK, 'clientIdentifier': 'none'}
    ]
    assert not of_kind('DHCPv6Config', secondary_address='10.20.0.42')


def test_a_node_nothing_addresses_is_left_to_its_lease() -> None:
    # Every cloud node: the platform gives it an address, and the machine
    # configuration says nothing about links at all.
    for kind in ('LinkAliasConfig', 'LinkConfig', 'DHCPv4Config', 'DHCPv6Config'):
        assert not of_kind(kind)


def test_the_worker_states_its_own_address_instead_of_leasing_one() -> None:
    # The gateway's FRR neighbor statement, the qbittorrent port forward and
    # day 1's apid endpoint all name this address as a constant; a lease would
    # make each of them a guess — and the cluster VLAN runs no DHCP server to
    # offer one in any case (physical/homelab-host.md §2).
    static = talos.STATIC_ADDRESSES[conventions.HOMELAB_NODE]
    link = uplink(role='worker', static_address=static)
    assert link['addresses'] == [
        {'address': f'{conventions.HOMELAB_NODE_IPV4}/{conventions.CLUSTER_VLAN.v4.prefixlen}'}
    ]
    # No lease on the link, and none anywhere else: the VLAN has no server.
    assert not of_kind('DHCPv4Config', role='worker', static_address=static)
    assert not of_kind('DHCPv6Config', role='worker', static_address=static)


def test_the_static_address_brings_the_routes_the_lease_used_to() -> None:
    static = talos.STATIC_ADDRESSES[conventions.HOMELAB_NODE]
    link = uplink(role='worker', static_address=static)
    # The subnet route comes from the address carrying the VLAN's prefix
    # rather than /32 — with DHCP off there is no leased subnet route to
    # conflict with, and without one the node cannot reach its own subnet.
    assert IPv4Interface(link['addresses'][0]['address']).network == conventions.CLUSTER_VLAN.v4
    # Everything else was the lease's other job, and now has to be said. The
    # next hop is the gateway's own leg on the same VLAN, which is what makes
    # it reachable without a route to reach it by.
    gateway = conventions.CLUSTER_VLAN.require_gateway()
    # A route with no destination is the default route for its gateway's
    # family.
    assert link['routes'] == [{'gateway': str(gateway)}]
    assert gateway in conventions.CLUSTER_VLAN.v4
    # Nothing else on the link: its MTU is the platform's.
    assert set(link) == {'apiVersion', 'kind', 'name', 'addresses', 'routes'}


@pytest.mark.parametrize('shape', ['dedicated-vip', 'homelab-worker'])
def test_the_link_is_selected_rather_than_named(shape: str) -> None:
    # `eth0`/`ens3`/`enp1s0` is a property of the PCI topology the platform
    # builds and of the kernel's naming policy; neither is this program's to
    # decide. The alias's selector is shown physical links only, so `true`
    # is "the physical link".
    (alias,) = of_kind('LinkAliasConfig', **SHAPES[shape])
    assert alias['selector'] == {'match': 'true'}
    assert uplink(**SHAPES[shape])['name'] == talos.UPLINK


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
    assert of_kind('KubeSpanConfig', role='worker')[0]['enabled'] is True


def test_ingress_defaults_to_block_and_enumerates_host_ports_only() -> None:
    rules = firewall()
    assert rules[0] == {'apiVersion': 'v1alpha1', 'kind': 'NetworkDefaultActionConfig', 'ingress': 'block'}

    opened = {port for rule in rules[1:] for port in rule['portSelector']['ports']}
    assert opened == set(talos.HOST_PORTS)
    # Among them the two the balancer forwards, from the same structure: an
    # opening the firewall lost would leave a listener forwarding to a port
    # the nodes drop.
    assert set(conventions.MANAGEMENT_PORTS) <= opened
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


def test_a_node_volume_is_one_partition_filling_the_disk_that_is_not_the_boot_disk() -> None:
    (volume,) = node_volumes(volume=VOLUME)
    # Named for the volume: Talos mounts it at `/var/mnt/<name>` and finds it
    # again on every boot by the partition label `u-<name>` it wrote.
    assert volume['name'] == VOLUME
    # A partition, stated: a `disk` volume is located by its selector with
    # `system_disk` unbound, so it could never find the disk by exclusion.
    assert volume['volumeType'] == 'partition'
    # The one disk that is not the boot disk, which is one disk because a node
    # carries at most one volume.
    assert volume['provisioning']['diskSelector'] == {'match': '!system_disk'}
    # The whole of it, and the whole of it again after a resize.
    assert volume['provisioning']['maxSize'] == '100%'
    assert volume['provisioning']['grow'] is True
    # Stated, because a volume found carrying another filesystem is refused
    # rather than mounted: a default that moved would strand every volume.
    assert volume['filesystem'] == {'type': 'xfs'}
    # No encryption, and no mount options beyond Talos' own: every key source
    # binds the dataset to a machine a rebuild replaces or to the instance's
    # own metadata (storage.md §6).
    assert set(volume) == {'apiVersion', 'kind', 'name', 'volumeType', 'provisioning', 'filesystem'}


def test_a_node_volume_labels_its_node() -> None:
    # What a `local` PersistentVolume's node affinity selects on, so that no
    # workload names the node itself (rfc-002 §10.5).
    assert merged('machine', volume=VOLUME)['nodeLabels'] == {conventions.NODE_VOLUME_LABEL: VOLUME}


@pytest.mark.parametrize('shape', [shape for shape, arguments in SHAPES.items() if 'volume' not in arguments])
def test_a_node_without_a_volume_mounts_no_node_volume_and_carries_no_label(shape: str) -> None:
    assert not node_volumes(**SHAPES[shape])
    assert 'nodeLabels' not in merged('machine', **SHAPES[shape])


def test_a_node_volume_is_mounted_where_conventions_says_it_is() -> None:
    # The seam between Talos and the path every consumer is written against.
    # Talos mounts a user volume at `constants.UserVolumeMountPoint` joined
    # with its name, and nothing in the document can move it; the suite cannot
    # import Talos' constant, so this literal is it.
    assert conventions.node_volume_mount(VOLUME) == '/var/mnt/' + VOLUME
