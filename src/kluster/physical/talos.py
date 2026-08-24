"""Talos day-1: secrets, machine configuration, bootstrap (declarative/physical.md §2).

Day-2 is deliberately `talosctl` — upgrades, `upgrade-k8s` and etcd snapshots
are imperative operations, and wrapping them in fake-declarative command
resources would buy drift detection that isn't real.

Machine configuration is composed from typed Python dicts and handed to the
provider as patches. JSON is emitted rather than YAML because it *is* YAML,
and the program then needs no serializer dependency to state its own
configuration.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

import pulumi
from pulumiverse_talos import machine

from kluster import conventions
from putils import Component, async_output, resolve

#: Kubelet reservations exist so eviction has something to work with: an
#: unreserved node starves its own control plane before the kubelet notices.
SYSTEM_RESERVED = {'cpu': '200m', 'memory': '512Mi', 'ephemeral-storage': '1Gi'}

#: Ports that terminate in the host network namespace. Service ports are
#: deliberately absent: LoadBalancer traffic is answered by Cilium's BPF
#: datapath at tc ingress, ahead of nftables, so declared frontends serve
#: without a firewall entry while undeclared ports fall through to
#: default-deny (declarative/physical.md §2).
HOST_PORTS: tuple[int, ...] = (
    50000,  # apid
    51820,  # KubeSpan
    6443,  # kube-apiserver — a hostNetwork static pod
    10250,  # kubelet, intra-cluster
)


def cluster_patch(*, endpoint: str, cert_sans: Sequence[str]) -> dict[str, Any]:
    """The parts every node in the cluster shares."""
    return {
        'machine': {
            'features': {
                # There is no kube-proxy to fall back on, so the node-local
                # apiserver front is mandatory rather than an optimization.
                'kubePrism': {'enabled': True, 'port': conventions.KUBEPRISM_PORT},
            },
            'network': {
                'kubespan': {'enabled': True},
            },
            'kubelet': {
                'extraConfig': {'systemReserved': SYSTEM_RESERVED},
            },
        },
        'cluster': {
            # KubeSpan peers find each other through discovery; without it the
            # mesh has no membership.
            'discovery': {'enabled': True},
            'allowSchedulingOnControlPlanes': True,
            'network': {
                'cni': {'name': 'none'},
                # IPv4 first: the cluster is dual-stack with v4 primary
                # (architecture.md §1.3).
                'podSubnets': [str(conventions.POD_CIDR_V4), str(conventions.POD_CIDR_V6)],
                'serviceSubnets': [str(conventions.SERVICE_CIDR_V4), str(conventions.SERVICE_CIDR_V6)],
            },
            'apiServer': {
                'certSANs': list(cert_sans),
                # A public 6443 warrants both, defaults notwithstanding.
                'extraArgs': {'anonymous-auth': 'false', 'audit-log-path': '/var/log/audit/kube/kube-apiserver.log'},
            },
            'etcd': {
                'advertisedSubnets': [str(conventions.VCN_CIDR)],
            },
        },
    }


def ingress_firewall_documents(extra_ports: Sequence[int] = ()) -> list[dict[str, Any]]:
    """The node-local firewall: default-deny plus one rule per host port.

    Talos expresses this as separate configuration documents rather than as
    v1alpha1 fields, so they travel as their own patches.
    """
    documents: list[dict[str, Any]] = [
        {'apiVersion': 'v1alpha1', 'kind': 'NetworkDefaultActionConfig', 'ingress': 'block'}
    ]
    documents += [
        {
            'apiVersion': 'v1alpha1',
            'kind': 'NetworkRuleConfig',
            'name': f'port-{port}',
            'portSelector': {'ports': [port], 'protocol': 'tcp'},
            'ingress': [{'subnet': '0.0.0.0/0'}, {'subnet': '::/0'}],
        }
        for port in (*HOST_PORTS, *extra_ports)
    ]
    return documents


def patches(
    *,
    endpoint: str,
    cert_sans: Sequence[str],
    extra_ports: Sequence[int] = (),
    extra: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """The patch list for one node, as the provider wants it."""
    documents: list[Mapping[str, Any]] = [
        cluster_patch(endpoint=endpoint, cert_sans=cert_sans),
        *ingress_firewall_documents(extra_ports),
        *extra,
    ]
    return [json.dumps(document) for document in documents]


class TalosCluster(Component, pulumi_type='kluster:physical:TalosCluster'):
    """Cluster PKI and the per-node machine configuration.

    The secrets land in Pulumi state — the provider's ephemeral resources do
    not bridge — which is why the state backend is passphrase-encrypted behind
    client-certificate TLS.
    """

    def __init__(
        self,
        name: str,
        *,
        cluster_name: str,
        endpoint: pulumi.Input[str],
        cert_sans: Sequence[pulumi.Input[str]],
        control_plane_nodes: Sequence[str],
        talos_version: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self._endpoint = endpoint
        self._cert_sans = cert_sans

        self.secrets = machine.Secrets(
            f'{name}-secrets',
            talos_version=talos_version,
            # Regenerating cluster PKI is a rebuild, not an update.
            opts=self.child_opts(protect=True),
        )

        self.configurations = {
            node: machine.get_configuration_output(
                cluster_name=cluster_name,
                cluster_endpoint=endpoint,
                machine_type='controlplane',
                # The provider's input and output types describe the same
                # structure under different names.
                machine_secrets=cast('Any', self.secrets.machine_secrets),
                talos_version=talos_version,
                # The endpoint is the load balancer's address, which exists
                # only once that resource does — so the patches are computed
                # inside an async_output rather than at declaration time.
                config_patches=async_output(self._patches),
            )
            for node in control_plane_nodes
        }

        self.register_outputs({})

    async def _patches(self) -> list[str]:
        endpoint, *sans = await resolve(self._endpoint, *self._cert_sans)
        return patches(endpoint=str(endpoint), cert_sans=[str(san) for san in sans])

    @property
    def machine_configs(self) -> dict[str, pulumi.Output[str]]:
        """Per-node configuration, ready to become instance `user_data`."""
        return {node: configuration.machine_configuration for node, configuration in self.configurations.items()}
