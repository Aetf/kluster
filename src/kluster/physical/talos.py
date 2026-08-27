"""Talos day-1: secrets, machine configuration, apply, bootstrap, health.

The provider chain of declarative/physical.md §2, one resource each: the
cluster PKI, a machine configuration per node, the configuration apply that
carries later changes over apid, the one-time bootstrap of the first control
plane, the kubeconfig and client configuration, and the health check that
gates everything downstream.

The chain is two components because the day it describes is two days.
`TalosCluster` is day 0: the PKI and the configuration each machine boots
with, delivered out of band as instance metadata or a seed image, and
therefore knowable before any machine exists. `TalosDay1` is what happens
once they do: it needs the address each machine answers apid on, which is a
fact about a running machine rather than a decision this program makes.

Splitting them is not an aesthetic choice. An OCI instance's `user_data` *is*
its machine configuration, so a configuration naming something the cloud only
assigns to the finished instance — the augmented node's secondary private IP —
would wait on the instance that is waiting on it. Day 1 carries those
addresses instead, over apid, on top of the configuration the machine booted.

The homelab worker's address runs the other way. Nothing assigns it — the LAN
offers a lease and this program wants a constant, because the gateway's BGP
neighbour statement names that constant too — so the worker states its own
address, and states it in the configuration it boots with.

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
from ipaddress import IPv4Address, IPv4Interface
from typing import Any, Literal, cast

import pulumi
from pulumiverse_talos import client, machine
from pulumiverse_talos.cluster import Kubeconfig, get_health_output

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

#: A default route's destination, as Talos spells a route network. The same
#: characters as the firewall subnet above and a different thing entirely:
#: there it is who may come in, here it is where everything goes out.
DEFAULT_ROUTE_V4 = '0.0.0.0/0'

#: The home LAN's router, and so the worker VM's default route. It is the same
#: box the worker peers with over BGP — that LAN has one router — but the two
#: are stated separately because they authorize different things: this is
#: where the node's traffic goes, and `bgp_peers` is who may talk routing to
#: it. It is needed at all only because the worker's address is static: the
#: lease that would have carried a default route is the thing being refused.
HOME_ROUTER_IPV4 = IPv4Address('192.168.80.1')


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


@dataclass(frozen=True)
class StaticAddress:
    """A node's own addressing on a network that will not hand it out.

    `address` carries its prefix rather than being a bare host, because the
    prefix is what makes the LAN a connected route; `gateway` is the next hop
    for everything else.
    """

    address: IPv4Interface
    gateway: IPv4Address


#: Nodes whose machine configuration has to state their address, because
#: nothing else will. Only the homelab worker qualifies. A cloud node is
#: handed its address by the platform it boots on, but the worker is a VM on a
#: LAN whose only offer is a DHCP lease — and three other places already name
#: its address as a constant: the gateway's FRR neighbour statement, the
#: gateway's port forward for the qbittorrent peer port, and day 1's apid
#: endpoint. A lease would make all three a guess (physical/homelab-host.md
#: §2).
#:
#: This is a table rather than a constructor input on purpose. The address is
#: not a decision a caller makes: a stack free to pass one could tell the
#: machine an address the router was never told about, which is the failure
#: the constant exists to prevent.
STATIC_ADDRESSES: Mapping[str, StaticAddress] = {
    conventions.HOMELAB_NODE: StaticAddress(
        address=IPv4Interface(f'{conventions.HOMELAB_NODE_IPV4}/{conventions.VLAN_SERVER.prefixlen}'),
        gateway=HOME_ROUTER_IPV4,
    ),
}


def static_address_patch(static: StaticAddress) -> dict[str, Any]:
    """Configure the node's interface itself: address, subnet, default route.

    The counterpart of `secondary_address_patch` for a machine no platform
    configures on its behalf. Two differences from that one, which are the
    same decision twice: DHCP is off, and the address carries the LAN's prefix
    instead of /32. With no lease there is no subnet route to conflict with,
    and with no subnet route the address has to bring one. The default route
    is then explicit, because carrying it was the lease's other job.

    Whatever else the lease carried goes with it. Resolvers fall back to
    Talos' own defaults, which is what the cloud nodes effectively use too;
    IPv6 is untouched, because the GUA this design expects is SLAAC and SLAAC
    is the kernel's, not DHCP's.

    The interface is selected the way `secondary_address_patch` selects it —
    `physical: true`, not a name. A name (`eth0`, `ens3`, `enp1s0`) is a
    property of the PCI topology QEMU happens to build and of the kernel's
    naming policy, neither of which this program decides; `physical: true`
    matches an ordinary Ethernet link, which is what a virtio NIC presents as
    and which the worker has exactly one of.
    """
    return {
        'machine': {
            'network': {
                'interfaces': [
                    {
                        'deviceSelector': {'physical': True},
                        'dhcp': False,
                        'addresses': [str(static.address)],
                        'routes': [{'network': DEFAULT_ROUTE_V4, 'gateway': str(static.gateway)}],
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
    static_address: StaticAddress | None = None,
    secondary_address: str | None = None,
    bgp_peer: str | None = None,
) -> list[str]:
    """The patch list for one node, as the provider wants it.

    `bgp_peer` is a subnet, not a host: it is who may open a BGP session with
    this node, and the answer for the homelab worker is the gateway alone.

    `static_address` is the node's own address where no platform assigns one,
    and `secondary_address` an extra address on top of whichever it already
    has; no node has both, and a node with neither is configured by its lease.
    """
    documents: list[Mapping[str, Any]] = [node_patch(), local_path_patch()]
    if role == 'controlplane':
        documents.append(control_plane_patch(cert_sans=cert_sans, secretbox_secret=secretbox_secret))
    if static_address is not None:
        documents.append(static_address_patch(static_address))
    if secondary_address is not None:
        documents.append(secondary_address_patch(secondary_address))
    extra = [Opening(BGP_PORT, (bgp_peer,))] if bgp_peer is not None else []
    documents += ingress_firewall_documents(extra)
    return [json.dumps(document) for document in documents]


class TalosCluster(Component, pulumi_type='kluster:physical:TalosCluster'):
    """The cluster PKI and the configuration each machine boots with.

    The secrets land in Pulumi state — the provider's ephemeral resources do
    not bridge — which is why the state backend is passphrase-encrypted behind
    client-certificate TLS.

    Day 0 delivers each machine its configuration out of band: `user_data` in
    OCI instance metadata for the cloud nodes, a cloud-init seed for the
    homelab VM. Everything that needs a machine to answer — applying later
    changes, bootstrapping etcd, the health gate, the two credentials the
    later stacks are built on — is `TalosDay1`.

    :param control_plane_nodes: node names, in the order they take the
        cluster; the first one is the node that bootstraps etcd.
    :param worker_nodes: node names that get a worker configuration.
    :param bgp_peers: node name to the subnet allowed to open a BGP session
        with it (the homelab worker's gateway). This is a site fact rather
        than a machine one, so it is part of what the machine boots with.

    A node named in `STATIC_ADDRESSES` also boots with its own address, its
    LAN's prefix and a default route, rather than with whatever a DHCP server
    offers it.
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

        self.cluster_name = cluster_name
        self._endpoint = endpoint
        self._talos_version = talos_version
        self._cert_sans = tuple(cert_sans)
        self._bgp_peers = dict(bgp_peers or {})
        unknown = sorted(set(self._bgp_peers) - set(self.roles))
        if unknown:
            raise ValueError(f'BGP peers name nodes that are not in the cluster: {unknown}')

        self.secrets = machine.Secrets(
            f'{name}-secrets',
            talos_version=talos_version,
            # Regenerating cluster PKI is a rebuild, not an update.
            opts=self.child_opts(protect=True),
        )

        self.configurations = {node: self._render(node) for node in self.roles}

        self.register_outputs({})

    # -- configuration ------------------------------------------------------

    def configuration(
        self,
        node: str,
        *,
        secondary_address: pulumi.Input[str] | None = None,
    ) -> pulumi.Output[machine.GetConfigurationResult]:
        """One node's machine configuration, optionally with an extra address.

        Without `secondary_address` this is what the machine boots with,
        rendered once. With one it is a second rendering of the same
        configuration that additionally puts that address on the node's
        interface — a configuration that only exists to be applied over apid,
        because the address it names is assigned to an instance that the first
        rendering is an input to.
        """
        if secondary_address is None:
            return self.configurations[node]
        return self._render(node, secondary_address=secondary_address)

    def _render(
        self,
        node: str,
        *,
        secondary_address: pulumi.Input[str] | None = None,
    ) -> pulumi.Output[machine.GetConfigurationResult]:
        role = self.roles[node]
        return machine.get_configuration_output(
            cluster_name=self.cluster_name,
            cluster_endpoint=self._endpoint,
            machine_type=role,
            # The provider's input and output types describe the same
            # structure under different names.
            machine_secrets=cast('Any', self.secrets.machine_secrets),
            talos_version=self._talos_version,
            # The endpoint is the load balancer's address, which exists only
            # once that resource does — so the patches are computed inside an
            # async_output rather than at declaration time.
            config_patches=async_output(partial(self._patches, node, role, secondary_address)),
        )

    async def _patches(self, node: str, role: Role, secondary_address: pulumi.Input[str] | None) -> list[str]:
        return patches(
            role=role,
            cert_sans=await _resolved(self._cert_sans),
            secretbox_secret=await self._secretbox_secret() if role == 'controlplane' else None,
            static_address=STATIC_ADDRESSES.get(node),
            secondary_address=await _resolved_one(secondary_address),
            bgp_peer=await _resolved_one(self._bgp_peers.get(node)),
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

    # -- outputs ------------------------------------------------------------

    @property
    def machine_configs(self) -> dict[str, pulumi.Output[str]]:
        """Per-node configuration, ready to become `user_data` or a seed ISO."""
        return {node: configuration.machine_configuration for node, configuration in self.configurations.items()}


class TalosDay1(Component, pulumi_type='kluster:physical:TalosDay1'):
    """Apply, bootstrap, health, and the credentials the rest of the world reads.

    Everything here talks to machines that already run, over apid on port
    50000, which is why it is a component of its own: it is declared with the
    addresses those machines answer on, and those exist only once the
    instances and the worker VM do.

    :param cluster: the PKI and the day-0 configuration these machines booted.
    :param addresses: node name to the address talosctl reaches it at. Every
        node of the cluster needs one — a cluster is not healthy because the
        nodes somebody listed are.
    :param secondary_addresses: node name to an extra address to put on its
        interface (the augmented node's dedicated VIP).
    """

    def __init__(
        self,
        name: str,
        *,
        cluster: TalosCluster,
        addresses: Mapping[str, pulumi.Input[str]],
        secondary_addresses: Mapping[str, pulumi.Input[str]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self.cluster = cluster
        self._addresses = dict(addresses)
        self._secondary_addresses = dict(secondary_addresses or {})
        for label, keyed in (
            ('addresses', self._addresses),
            ('secondary addresses', self._secondary_addresses),
        ):
            unknown = sorted(set(keyed) - set(cluster.roles))
            if unknown:
                raise ValueError(f'{label} name nodes that are not in the cluster: {unknown}')
        missing = sorted(set(cluster.roles) - set(self._addresses))
        if missing:
            raise ValueError(f'day 1 needs an address for every node, and {missing} have none')

        # What is applied is the booted configuration plus whatever only exists
        # now: the augmented node's secondary private IP.
        self.configurations = {
            node: cluster.configuration(node, secondary_address=self._secondary_addresses.get(node))
            for node in cluster.roles
        }

        client_configuration = cast('Any', cluster.secrets.client_configuration)

        # Node-serial by construction: each apply waits for the one before it,
        # so a change that reboots the machine never takes the quorum with it.
        self.applies: dict[str, machine.ConfigurationApply] = {}
        previous: list[pulumi.Resource] = []
        for node in cluster.roles:
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

        first = cluster.control_plane_nodes[0]
        # Bootstrap is a once-per-cluster operation on one node: running it on
        # a second control plane would try to start a second etcd cluster.
        self.bootstrap = machine.Bootstrap(
            f'{name}-bootstrap',
            node=self._addresses[first],
            endpoint=self._addresses[first],
            client_configuration=client_configuration,
            opts=self.child_opts(depends_on=[self.applies[first]]),
        )

        self.kubeconfig_source = Kubeconfig(
            f'{name}-kubeconfig',
            node=self._addresses[first],
            endpoint=self._addresses[first],
            client_configuration=client_configuration,
            opts=self.child_opts(depends_on=[self.bootstrap]),
        )

        self._client_configuration = client.get_configuration_output(
            cluster_name=cluster.cluster_name,
            client_configuration=client_configuration,
            endpoints=async_output(partial(self._addresses_of, cluster.control_plane_nodes)),
            nodes=async_output(partial(self._addresses_of, tuple(cluster.roles))),
            opts=pulumi.InvokeOutputOptions(parent=self),
        )

        # The gate. This data source does not return until the cluster reports
        # healthy, so anything that resolves it is ordered behind a working
        # cluster rather than behind a resource that merely finished.
        self.health = get_health_output(
            client_configuration=client_configuration,
            control_plane_nodes=async_output(partial(self._addresses_of, cluster.control_plane_nodes)),
            worker_nodes=async_output(partial(self._addresses_of, cluster.worker_nodes)),
            endpoints=async_output(partial(self._addresses_of, cluster.control_plane_nodes)),
            opts=pulumi.InvokeOutputOptions(parent=self, depends_on=[self.bootstrap, *self.applies.values()]),
        )
        self._kubeconfig = pulumi.Output.secret(async_output(self._healthy_kubeconfig))

        self.register_outputs({})

    # -- inputs prepared asynchronously -------------------------------------

    async def _healthy_kubeconfig(self) -> str:
        """The kubeconfig, resolved behind the health check rather than beside it."""
        _, raw = await resolve(self.health, self.kubeconfig_source.kubeconfig_raw)
        return str(raw)

    async def _addresses_of(self, nodes: Sequence[str]) -> list[str]:
        return await _resolved([self._addresses[node] for node in nodes])

    # -- outputs ------------------------------------------------------------

    @property
    def kubeconfig(self) -> pulumi.Output[str]:
        """Cluster-admin credentials, released only once the cluster is healthy."""
        return self._kubeconfig

    @property
    def talosconfig(self) -> pulumi.Output[str]:
        """The talosctl client configuration: the same PKI, for the machine API."""
        return pulumi.Output.secret(self._client_configuration.apply(lambda config: config.talos_config))


async def _resolved(inputs: Sequence[pulumi.Input[str]]) -> list[str]:
    if not inputs:
        return []
    values = await resolve(*inputs)
    return [str(value) for value in (values if len(inputs) > 1 else (values,))]


async def _resolved_one(value: pulumi.Input[str] | None) -> str | None:
    return None if value is None else str(await resolve(value))
