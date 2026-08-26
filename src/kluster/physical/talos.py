"""Talos day-1: secrets, machine configuration, apply, bootstrap, health.

The provider chain of declarative/physical.md §2, one resource each: the
cluster PKI, a machine configuration per node, the configuration apply that
carries later changes over apid, the one-time bootstrap of the first control
plane, the kubeconfig and client configuration, and the health check that
gates everything downstream.

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
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, cast

import pulumi
from pulumiverse_talos import client, cluster, machine

from kluster import conventions
from putils import Component, async_output, resolve

#: What a node is to Kubernetes. The cloud fleet is control planes that also
#: carry workloads (architecture.md §1.1); the homelab machine is a worker.
Role = Literal['controlplane', 'worker']

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

#: BGP. Only the homelab worker speaks it, and only with the gateway
#: (cluster-infra.md §2), so it is never part of the host-port census above.
BGP_PORT = 179

#: The whole internet, both families — what a management port on a public
#: node is exposed to whether or not it is written down.
ANYWHERE: tuple[str, ...] = ('0.0.0.0/0', '::/0')


@dataclass(frozen=True)
class Opening:
    """One hole in the node-local firewall: a port, and who may come through.

    Talos' ingress firewall is default-deny, so an `Opening` is the only way
    traffic reaches a listener in the host network namespace.
    """

    port: int
    subnets: tuple[str, ...] = ANYWHERE
    protocol: str = 'tcp'

    def document(self) -> dict[str, Any]:
        return {
            'apiVersion': 'v1alpha1',
            'kind': 'NetworkRuleConfig',
            'name': f'port-{self.port}',
            'portSelector': {'ports': [self.port], 'protocol': self.protocol},
            'ingress': [{'subnet': subnet} for subnet in self.subnets],
        }


def node_patch() -> dict[str, Any]:
    """What every machine in the cluster carries, whatever its role."""
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
            'network': {
                # IPv4 first: the cluster is dual-stack with v4 primary
                # (architecture.md §1.3).
                'podSubnets': [str(conventions.POD_CIDR_V4), str(conventions.POD_CIDR_V6)],
                'serviceSubnets': [str(conventions.SERVICE_CIDR_V4), str(conventions.SERVICE_CIDR_V6)],
            },
        },
    }


def control_plane_patch(*, cert_sans: Sequence[str], secretbox_secret: str | None = None) -> dict[str, Any]:
    """The parts only a control plane has: the apiserver, etcd, and the CNI.

    A worker's configuration has no apiserver to harden and no etcd to
    encrypt, and the CNI is installed by the control plane, so none of this
    belongs in the shared patch.
    """
    config: dict[str, Any] = {
        'cluster': {
            'allowSchedulingOnControlPlanes': True,
            'network': {'cni': {'name': 'none'}},
            'apiServer': {
                'certSANs': list(cert_sans),
                # A public 6443 warrants both, defaults notwithstanding.
                'extraArgs': {'anonymous-auth': 'false', 'audit-log-path': '/var/log/audit/kube/kube-apiserver.log'},
            },
            'etcd': {
                'advertisedSubnets': [str(conventions.VCN_CIDR)],
            },
        }
    }
    if secretbox_secret is not None:
        # Encryption at rest for Kubernetes secrets. etcd lives in a $0-trust
        # tenancy and its snapshots are shipped off-site hourly
        # (architecture.md §6.5), so the key material has to be stated here
        # rather than inherited from whatever the generator happened to do.
        config['cluster']['secretboxEncryptionSecret'] = secretbox_secret
    return config


def local_path_patch(root: str = conventions.LOCAL_PATH_ROOT) -> dict[str, Any]:
    """The directory the `local-path` StorageClass hands out (storage.md §2).

    The provisioner is `k8s-base`'s; the path underneath it is machine
    configuration, because the kubelet cannot serve a hostPath it has not
    been given a mount for. `rshared` propagation is what lets a volume
    mounted inside the directory afterwards still reach a pod.
    """
    return {
        'machine': {
            'kubelet': {
                'extraMounts': [
                    {
                        'destination': root,
                        'type': 'bind',
                        'source': root,
                        'options': ['bind', 'rshared', 'rw'],
                    }
                ]
            }
        }
    }


def secondary_address_patch(address: str) -> dict[str, Any]:
    """Put an extra address on the node's physical interface.

    OCI hands the augmented node a secondary private IP on its VNIC but does
    not configure the guest, so without this the dedicated VIP is an address
    the machine never answers for (architecture.md §3.2). It is added as a
    host route (/32) on purpose: the subnet route already arrives over DHCP,
    and a second one for the same prefix is a conflict, not a redundancy.
    """
    return {
        'machine': {
            'network': {
                'interfaces': [
                    {
                        'deviceSelector': {'physical': True},
                        'dhcp': True,
                        'addresses': [f'{address}/32'],
                    }
                ]
            }
        }
    }


def ingress_firewall_documents(extra: Sequence[Opening] = ()) -> list[dict[str, Any]]:
    """The node-local firewall: default-deny plus one rule per opening.

    Talos expresses this as separate configuration documents rather than as
    v1alpha1 fields, so they travel as their own patches.
    """
    openings = [*(Opening(port) for port in HOST_PORTS), *extra]
    return [
        {'apiVersion': 'v1alpha1', 'kind': 'NetworkDefaultActionConfig', 'ingress': 'block'},
        *(opening.document() for opening in openings),
    ]


def patches(
    *,
    role: Role = 'controlplane',
    cert_sans: Sequence[str] = (),
    secretbox_secret: str | None = None,
    secondary_address: str | None = None,
    bgp_peer: str | None = None,
) -> list[str]:
    """The patch list for one node, as the provider wants it.

    `bgp_peer` is a subnet, not a host: it is who may open a BGP session with
    this node, and the answer for the homelab worker is the gateway alone.
    """
    documents: list[Mapping[str, Any]] = [node_patch(), local_path_patch()]
    if role == 'controlplane':
        documents.append(control_plane_patch(cert_sans=cert_sans, secretbox_secret=secretbox_secret))
    if secondary_address is not None:
        documents.append(secondary_address_patch(secondary_address))
    extra = [Opening(BGP_PORT, (bgp_peer,))] if bgp_peer is not None else []
    documents += ingress_firewall_documents(extra)
    return [json.dumps(document) for document in documents]


class TalosCluster(Component, pulumi_type='kluster:physical:TalosCluster'):
    """Cluster PKI, per-node machine configuration, and the day-1 chain.

    The secrets land in Pulumi state — the provider's ephemeral resources do
    not bridge — which is why the state backend is passphrase-encrypted behind
    client-certificate TLS.

    Day-0 delivers each machine its configuration out of band: `user_data` in
    OCI instance metadata for the cloud nodes, a cloud-init seed for the
    homelab VM. `addresses` is what turns on day-1 on top of that — the
    address each machine answers apid on, which exists only once the machine
    does. Without it the component stops at the configurations, and
    `kubeconfig`/`talosconfig` have nothing to return.

    :param control_plane_nodes: node names, in the order they take the
        cluster; the first one is the node that bootstraps etcd.
    :param worker_nodes: node names that get a worker configuration.
    :param addresses: node name to the address talosctl reaches it at.
    :param secondary_addresses: node name to an extra address to configure on
        its interface (the augmented node's dedicated VIP).
    :param bgp_peers: node name to the subnet allowed to open a BGP session
        with it (the homelab worker's gateway).
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
        worker_nodes: Sequence[str] = (),
        addresses: Mapping[str, pulumi.Input[str]] | None = None,
        secondary_addresses: Mapping[str, pulumi.Input[str]] | None = None,
        bgp_peers: Mapping[str, pulumi.Input[str]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        if not control_plane_nodes:
            raise ValueError('a cluster needs at least one control-plane node')
        self.control_plane_nodes = tuple(control_plane_nodes)
        self.worker_nodes = tuple(worker_nodes)
        self.roles: dict[str, Role] = {
            **{node: cast('Role', 'controlplane') for node in self.control_plane_nodes},
            **{node: cast('Role', 'worker') for node in self.worker_nodes},
        }
        if len(self.roles) != len(self.control_plane_nodes) + len(self.worker_nodes):
            raise ValueError('a node is both a control plane and a worker')

        self._cert_sans = tuple(cert_sans)
        self._addresses = dict(addresses or {})
        self._secondary_addresses = dict(secondary_addresses or {})
        self._bgp_peers = dict(bgp_peers or {})
        for label, keyed in (
            ('addresses', self._addresses),
            ('secondary addresses', self._secondary_addresses),
            ('BGP peers', self._bgp_peers),
        ):
            unknown = sorted(set(keyed) - set(self.roles))
            if unknown:
                raise ValueError(f'{label} name nodes that are not in the cluster: {unknown}')
        if self._addresses and set(self._addresses) != set(self.roles):
            raise ValueError('day-1 needs an address for every node, or for none of them')

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
                machine_type=role,
                # The provider's input and output types describe the same
                # structure under different names.
                machine_secrets=cast('Any', self.secrets.machine_secrets),
                talos_version=talos_version,
                # The endpoint is the load balancer's address, which exists
                # only once that resource does — so the patches are computed
                # inside an async_output rather than at declaration time.
                config_patches=async_output(partial(self._patches, node, role)),
            )
            for node, role in self.roles.items()
        }

        self.applies: dict[str, machine.ConfigurationApply] = {}
        self.bootstrap: machine.Bootstrap | None = None
        self.kubeconfig_source: cluster.Kubeconfig | None = None
        self.health: pulumi.Output[cluster.GetHealthResult] | None = None
        self._client_configuration: pulumi.Output[client.GetConfigurationResult] | None = None
        self._kubeconfig: pulumi.Output[str] | None = None
        if self._addresses:
            self._declare_day1(name, cluster_name)

        self.register_outputs({})

    # -- day 1 --------------------------------------------------------------

    def _declare_day1(self, name: str, cluster_name: str) -> None:
        """Apply, bootstrap, and the credentials the rest of the world reads."""
        client_configuration = cast('Any', self.secrets.client_configuration)

        # Node-serial by construction: each apply waits for the one before it,
        # so a change that reboots the machine never takes the quorum with it.
        previous: list[pulumi.Resource] = []
        for node in self.roles:
            applied = machine.ConfigurationApply(
                f'{name}-{node}-config',
                node=self._addresses[node],
                endpoint=self._addresses[node],
                client_configuration=client_configuration,
                machine_configuration_input=self.configurations[node].machine_configuration,
                # A change needing a reboot is staged rather than applied, so
                # the reboot is an operator's decision and not a side effect.
                apply_mode='staged_if_needing_reboot',
                # `reset` wipes STATE and EPHEMERAL — every partition the node
                # has (provider issue #205). It stays off on every node,
                # whatever it carries: replacing a node is an explicit
                # procedure (drain, etcd leave, destroy, recreate), never
                # something a destroy of this resource does on its own.
                on_destroy=machine.ConfigurationApplyOnDestroyArgs(reset=False, graceful=True, reboot=False),
                opts=self.child_opts(depends_on=previous),
            )
            self.applies[node] = applied
            previous = [applied]

        first = self.control_plane_nodes[0]
        # Bootstrap is a once-per-cluster operation on one node: running it on
        # a second control plane would try to start a second etcd cluster.
        self.bootstrap = machine.Bootstrap(
            f'{name}-bootstrap',
            node=self._addresses[first],
            endpoint=self._addresses[first],
            client_configuration=client_configuration,
            opts=self.child_opts(depends_on=[self.applies[first]]),
        )

        self.kubeconfig_source = cluster.Kubeconfig(
            f'{name}-kubeconfig',
            node=self._addresses[first],
            endpoint=self._addresses[first],
            client_configuration=client_configuration,
            opts=self.child_opts(depends_on=[self.bootstrap]),
        )

        self._client_configuration = client.get_configuration_output(
            cluster_name=cluster_name,
            client_configuration=client_configuration,
            endpoints=async_output(partial(self._addresses_of, self.control_plane_nodes)),
            nodes=async_output(partial(self._addresses_of, tuple(self.roles))),
            opts=pulumi.InvokeOutputOptions(parent=self),
        )

        # The gate. This data source does not return until the cluster reports
        # healthy, so anything that resolves it is ordered behind a working
        # cluster rather than behind a resource that merely finished.
        self.health = cluster.get_health_output(
            client_configuration=client_configuration,
            control_plane_nodes=async_output(partial(self._addresses_of, self.control_plane_nodes)),
            worker_nodes=async_output(partial(self._addresses_of, self.worker_nodes)),
            endpoints=async_output(partial(self._addresses_of, self.control_plane_nodes)),
            opts=pulumi.InvokeOutputOptions(parent=self, depends_on=[self.bootstrap, *self.applies.values()]),
        )
        self._kubeconfig = pulumi.Output.secret(async_output(self._healthy_kubeconfig))

    # -- inputs prepared asynchronously -------------------------------------

    async def _healthy_kubeconfig(self) -> str:
        """The kubeconfig, resolved behind the health check rather than beside it."""
        assert self.health is not None and self.kubeconfig_source is not None
        _, raw = await resolve(self.health, self.kubeconfig_source.kubeconfig_raw)
        return str(raw)

    async def _patches(self, node: str, role: Role) -> list[str]:
        return patches(
            role=role,
            cert_sans=await self._resolved(self._cert_sans),
            secretbox_secret=await self._secretbox_secret() if role == 'controlplane' else None,
            secondary_address=await self._resolved_one(self._secondary_addresses.get(node)),
            bgp_peer=await self._resolved_one(self._bgp_peers.get(node)),
        )

    async def _secretbox_secret(self) -> str | None:
        """The generated key that encrypts Kubernetes secrets in etcd.

        Talos generates one with the rest of the bundle; naming it in a patch
        is what makes encryption at rest a property this program states rather
        than one it inherits. A bundle that arrives without one is reported as
        an error — the deployment fails rather than quietly bringing up a
        cluster whose secrets are readable in every etcd snapshot.
        """
        # The provider's result types are mappings that also expose their
        # fields as attributes, and only the mapping half survives being
        # resolved here — so the bundle is read by key. The SDK types every
        # field as present; what the provider returned is what decides.
        bundle = cast('Mapping[str, Any]', await resolve(self.secrets.machine_secrets))
        secret = cast('Mapping[str, Any]', bundle.get('secrets') or {}).get('secretbox_encryption_secret')
        if not secret:
            pulumi.error(
                'the machine secrets carry no secretbox key: etcd would hold Kubernetes secrets in clear', self
            )
            return None
        return str(secret)

    async def _addresses_of(self, nodes: Sequence[str]) -> list[str]:
        return await self._resolved([self._addresses[node] for node in nodes])

    async def _resolved(self, inputs: Sequence[pulumi.Input[str]]) -> list[str]:
        if not inputs:
            return []
        values = await resolve(*inputs)
        return [str(value) for value in (values if len(inputs) > 1 else (values,))]

    async def _resolved_one(self, value: pulumi.Input[str] | None) -> str | None:
        return None if value is None else str(await resolve(value))

    # -- outputs ------------------------------------------------------------

    @property
    def machine_configs(self) -> dict[str, pulumi.Output[str]]:
        """Per-node configuration, ready to become `user_data` or a seed ISO."""
        return {node: configuration.machine_configuration for node, configuration in self.configurations.items()}

    @property
    def kubeconfig(self) -> pulumi.Output[str]:
        """Cluster-admin credentials, released only once the cluster is healthy."""
        if self._kubeconfig is None:
            raise ValueError('the day-1 chain was not declared: no node addresses were given')
        return self._kubeconfig

    @property
    def talosconfig(self) -> pulumi.Output[str]:
        """The talosctl client configuration: the same PKI, for the machine API."""
        if self._client_configuration is None:
            raise ValueError('the day-1 chain was not declared: no node addresses were given')
        return pulumi.Output.secret(self._client_configuration.apply(lambda config: config.talos_config))
