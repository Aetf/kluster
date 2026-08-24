"""What the machine configuration must say.

These assertions are the design's load-bearing claims (declarative/physical.md
§2) in executable form: get any of them wrong and the cluster either does not
come up or comes up quietly insecure.
"""

import json
from typing import Any

from kluster import conventions
from kluster.physical import talos

ENDPOINT = 'https://203.0.113.10:6443'
SANS = ['203.0.113.10', 'api.example.test']


def documents() -> list[dict[str, Any]]:
    return [json.loads(patch) for patch in talos.patches(endpoint=ENDPOINT, cert_sans=SANS)]


def test_kubespan_and_kubeprism_are_on() -> None:
    machine = documents()[0]['machine']
    assert machine['network']['kubespan']['enabled'] is True
    # No kube-proxy exists to fall back on.
    assert machine['features']['kubePrism']['enabled'] is True
    assert machine['features']['kubePrism']['port'] == conventions.KUBEPRISM_PORT


def test_the_cluster_is_dual_stack_ipv4_first() -> None:
    network = documents()[0]['cluster']['network']
    assert network['podSubnets'][0] == str(conventions.POD_CIDR_V4)
    assert network['serviceSubnets'][0] == str(conventions.SERVICE_CIDR_V4)
    assert ':' in network['podSubnets'][1]
    assert ':' in network['serviceSubnets'][1]


def test_cilium_installs_itself() -> None:
    # Talos must ship no CNI: nodes stay NotReady until k8s-base lands Cilium.
    assert documents()[0]['cluster']['network']['cni'] == {'name': 'none'}


def test_control_planes_also_carry_workloads() -> None:
    assert documents()[0]['cluster']['allowSchedulingOnControlPlanes'] is True


def test_the_public_apiserver_is_hardened() -> None:
    api_server = documents()[0]['cluster']['apiServer']
    assert api_server['certSANs'] == SANS
    assert api_server['extraArgs']['anonymous-auth'] == 'false'
    assert 'audit-log-path' in api_server['extraArgs']


def test_the_kubelet_reserves_room_for_the_node() -> None:
    assert documents()[0]['machine']['kubelet']['extraConfig']['systemReserved'] == talos.SYSTEM_RESERVED


def test_ingress_defaults_to_block_and_enumerates_host_ports_only() -> None:
    firewall = [doc for doc in documents() if doc.get('kind', '').startswith('Network')]
    assert firewall[0] == {'apiVersion': 'v1alpha1', 'kind': 'NetworkDefaultActionConfig', 'ingress': 'block'}

    opened = {port for doc in firewall[1:] for port in doc['portSelector']['ports']}
    assert opened == set(talos.HOST_PORTS)
    # Service ports are answered by the BPF datapath before nftables sees
    # them, so an app port here would be a cross-stack leak.
    assert not opened & {port for port, _ in conventions.PUBLIC_PORT_CENSUS}


def test_the_homelab_worker_can_be_given_bgp() -> None:
    opened = {
        port
        for patch in talos.patches(endpoint=ENDPOINT, cert_sans=SANS, extra_ports=[179])
        for doc in [json.loads(patch)]
        if doc.get('kind') == 'NetworkRuleConfig'
        for port in doc['portSelector']['ports']
    }
    assert 179 in opened
