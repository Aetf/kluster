"""The cloud nodes and the load balancer in front of them.

A1 instances, one per machine configuration the component is handed, each a
control-plane node *and* an ingress node (architecture.md §1.1): etcd quorum
lives in one region, and the same machines terminate public traffic. They are
spread across availability domains before fault domains, so losing one takes
neither quorum nor ingress with it.

One of them additionally carries the **dedicated VIP**: a secondary
private IP and the reserved public IP that OCI 1:1-NATs onto it. Nothing about
the node is workload-specific, and a workload that needs the address finds the
node through scheduling constraints declared beside the workload
(architecture.md §3.2). Block volumes are a separate capability, attached per
entry of the fleet's volume table (`storage`).

The balancer forwards two lists of ports, both conventions (`forwarded`). The
management ports belong to the cluster rather than to any service -- they are
`conventions.MANAGEMENT_PORTS`, which the node firewall opens and the cluster
endpoint names from the same structure. Every other port is a row of the
public port census the balancer fronts (`conventions.PUBLIC_PORT_CENSUS`):
`apps` holds no OCI credential (credentials.md §3), so a service's listener is
declared here from its row, never beside the service (rfc-007 §5.3). Everything
declared per port is named after the port's field in the management
structure (`kubernetes`, say) or the census row's name (`https`), never after
its number -- the backend set and the listener in Pulumi and on the balancer,
the backends in Pulumi and, through the autoname derived from that, on the
balancer: a name is an identity that state and the balancer key on, and the
number is a value either structure lets anyone edit.

The balancer is dual-stack, and OCI's listeners and backend sets are not: each
carries one `ip_version`, a listener forwards only to a backend set of its own
family, and a backend set holds only backends of that family. So every
port it forwards is declared once per family the balancer holds (`FAMILIES`) --
a listener, a backend set and a backend per node for each -- and a port served
on one family alone is a port the other address of the same anchor refuses.
IPv4 is the family the cluster endpoint names, and its children carry the
port's name alone; every other family's carry the family after the name
(`kubernetes-ipv6`, `https-ipv6`).

The load balancer is a component of its own because the dependency runs
through it: a node's machine configuration names the cluster endpoint, which
*is* the load balancer's address, while the backends that point at the nodes
are separate resources created afterwards. Declaring both in one component
would ask Pulumi to resolve a cycle that does not actually exist.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

import pulumi
import pulumi_oci as oci

from kluster import conventions
from kluster.conventions import Front, Transports
from putils import Component, async_output, resolve

#: How the two families are told apart in the balancer's address list. The
#: separator decides it for every literal OCI hands back — an IPv4 literal
#: carries no colon and an IPv6 literal no dot — and it keeps deciding it if a
#: read leaves the address record's own `ip_version` field unset.
FAMILY_SEPARATOR: Mapping[str, str] = {'IPv4': '.', 'IPv6': ':'}

#: The families the balancer holds a public address of, spelled the way a
#: listener's and a backend set's `ip_version` takes them. The balancer's own
#: `nlb_ip_version` is these joined (`IPV4_AND_IPV6`), so the addresses it is
#: handed and the listeners that answer on them are one list.
FAMILIES: tuple[str, ...] = ('IPV4', 'IPV6')

#: The family the cluster endpoint and the certificate SANs name. Its children
#: carry the field alone; `named` puts every other family's after the field.
ENDPOINT_FAMILY = 'IPV4'


def management_ports() -> list[tuple[str, int]]:
    """Each management port as its field in `conventions.ManagementPorts` and its number.

    The field is what everything declared per port is named after; the number
    is only ever an input.
    """
    ports = conventions.MANAGEMENT_PORTS
    return list(zip(ports._fields, ports, strict=True))


#: The listener protocol that carries each set of transports. A port served
#: on both is one listener of `TCP_AND_UDP`, not a listener per transport
#: (OCI: Managing listeners).
LISTENER_PROTOCOL: Mapping[Transports, str] = {
    Transports.TCP: 'TCP',
    Transports.UDP: 'UDP',
    Transports.TCP_AND_UDP: 'TCP_AND_UDP',
}


class Forwarded(NamedTuple):
    """One port the balancer forwards to every cloud node, on each family it holds."""

    name: str
    """What everything declared for it is named after: a management port's
    field, or a census row's name."""
    port: int
    transports: Transports


def forwarded() -> list[Forwarded]:
    """Every port the balancer forwards: each management port, then each census row it fronts.

    A row the dedicated VIP fronts has no listener: one would forward to three
    nodes, none of which answers on that port at the address the balancer
    reaches it on. A row served on UDP alone is refused. Its backend set
    would check the port over TCP, as every set here does, and a UDP check
    needs a request the application answers with a known reply, which a row
    cannot state (rfc-007 §5.3).
    """
    management = [Forwarded(field, port, Transports.TCP) for field, port in management_ports()]
    census = [
        Forwarded(row.name, row.port, row.transports)
        for row in conventions.PUBLIC_PORT_CENSUS
        if row.front is Front.BALANCER
    ]
    for entry in census:
        if 'tcp' not in entry.transports.protocols:
            raise ValueError(
                f'the public port {entry.name!r} is served on UDP alone, and its backend set has no TCP port to check'
            )
    return [*management, *census]


def user_data(machine_config: str) -> str:
    """`machine_config` as the `user_data` an OCI instance's metadata carries.

    Base64 in the standard alphabet, of the configuration's UTF-8 bytes. OCI
    documents the value as base64-encoded data (the API reference's
    `LaunchInstanceDetails.metadata`), and Talos's `oracle` platform decodes it
    with `base64.StdEncoding`, reading a value that does not decode as no
    configuration at all: the node boots into maintenance mode.
    """
    return base64.b64encode(machine_config.encode()).decode()


def named(field: str, family: str) -> str:
    """What everything declared for one forwarded port on one family is named after.

    `field` is the port's name (`Forwarded.name`): alone for the endpoint's
    family, with the family after it for any other. Both halves are
    identities that state and the balancer key on, so the rule is fixed: a
    family renamed into or out of the bare name is every one of its listeners
    and backend sets replaced.
    """
    return field if family == ENDPOINT_FAMILY else f'{field}-{family.lower()}'


class NodeLoadBalancer(Component, pulumi_type='kluster:cloud:NodeLoadBalancer'):
    """The NLB and a backend set per port it forwards, on each family it holds — the cluster's endpoint."""

    def __init__(
        self,
        name: str,
        *,
        compartment_id: pulumi.Input[str],
        subnet_id: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)

        self.load_balancer = oci.networkloadbalancer.NetworkLoadBalancer(
            f'{name}-nlb',
            compartment_id=compartment_id,
            subnet_id=subnet_id,
            display_name=f'{name}-nlb',
            is_private=False,
            # Client-address preservation is the backend set's
            # `is_preserve_source` below; this flag is the different,
            # transparent-routing mode and stays off.
            is_preserve_source_destination=False,
            nlb_ip_version='_AND_'.join(FAMILIES),
            opts=self.child_opts(),
        )

        #: What the balancer forwards, in the order it is declared; the
        #: backends that fill each set are `CloudNodes`'s, from this same list.
        self.forwarded = forwarded()

        #: Keyed by family and then by the name of what it forwards
        #: (`Forwarded.name`); `named` of the two is each backend set's OCI
        #: name and what its logical name carries.
        self.backend_sets: dict[str, dict[str, oci.networkloadbalancer.BackendSet]] = {
            family: {
                entry.name: oci.networkloadbalancer.BackendSet(
                    f'{name}-nlb-{named(entry.name, family)}',
                    name=named(entry.name, family),
                    network_load_balancer_id=self.load_balancer.id,
                    policy='FIVE_TUPLE',
                    # On for every set and every family: the apiserver's
                    # audit log on the public 6443 (security-audit.md M3) and
                    # Envoy's access log behind 443 (rfc-007 §5.1) record the
                    # client's address, which without preservation is the
                    # balancer's. It is also what makes the subnet's rule for
                    # a backend the same rule as the listener's: a forwarded
                    # packet reaches the node carrying the client's address.
                    is_preserve_source=True,
                    # Over TCP for every set, a port served on TCP and UDP
                    # included; `forwarded` refuses one with no TCP to check.
                    health_checker=oci.networkloadbalancer.BackendSetHealthCheckerArgs(protocol='TCP', port=entry.port),
                    # Stated for every family, IPv4 included: the API
                    # reference gives the field no default, and it is fixed
                    # when the set is created.
                    ip_version=family,
                    opts=self.child_opts(),
                )
                for entry in self.forwarded
            }
            for family in FAMILIES
        }

        self.listeners = [
            oci.networkloadbalancer.Listener(
                f'{name}-nlb-listener-{named(entry.name, family)}',
                name=named(entry.name, family),
                network_load_balancer_id=self.load_balancer.id,
                default_backend_set_name=self.backend_sets[family][entry.name].name,
                port=entry.port,
                protocol=LISTENER_PROTOCOL[entry.transports],
                ip_version=family,
                opts=self.child_opts(),
            )
            for family in FAMILIES
            for entry in self.forwarded
        ]

        self.register_outputs({})

    @property
    def address(self) -> pulumi.Output[str]:
        """The public IPv4 the cluster endpoint and certificate SANs name."""
        return self._public_address('IPv4')

    @property
    def address_v6(self) -> pulumi.Output[str]:
        """The public IPv6, which only the `dns` stack's cluster anchor names.

        The balancer is dual-stack, so it holds one public address of each
        family and the anchor's AAAA is as much a machine fact as its A
        (docs/declarative/dns.md §2). The endpoint and the certificate SANs
        stay on the IPv4 alone: they are what a node's machine configuration
        is written with, and a Talos node reaches its own cluster over the
        address family the fleet is uniformly reachable on.
        """
        return self._public_address('IPv6')

    def _public_address(self, family: str) -> pulumi.Output[str]:
        """The balancer's one public address of `family`.

        Public only: the list also carries the private address the balancer
        holds in its own subnet, which no caller of this ever wants.
        """
        separator = FAMILY_SEPARATOR[family]

        def public(addresses: Sequence[Any]) -> str:
            for address in addresses:
                text = str(address.ip_address or '')
                if address.is_public and separator in text:
                    return text
            raise ValueError(f'the load balancer has no public {family} address')

        return self.load_balancer.ip_addresses.apply(public)


class CloudNodes(Component, pulumi_type='kluster:cloud:CloudNodes'):
    """The A1 nodes, the backends that put them behind the balancer, and the dedicated VIP one of them holds."""

    def __init__(
        self,
        name: str,
        *,
        compartment_id: pulumi.Input[str],
        subnet_id: pulumi.Input[str],
        image_id: pulumi.Input[str],
        machine_configs: Mapping[str, pulumi.Input[str]],
        ocpus: float,
        memory_gb: float,
        boot_volume_gb: int,
        placements: pulumi.Input[Sequence[tuple[str, str]]],
        dedicated_vip_node: str,
        load_balancer: NodeLoadBalancer,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        if dedicated_vip_node not in machine_configs:
            raise ValueError(
                f'the dedicated VIP is declared on {dedicated_vip_node!r}, which is not among {sorted(machine_configs)}'
            )

        self._placements = placements
        self.instances: dict[str, oci.core.Instance] = {}
        for index, (node, machine_config) in enumerate(sorted(machine_configs.items())):
            self.instances[node] = oci.core.Instance(
                f'{name}-{node}',
                compartment_id=compartment_id,
                # Spread by construction: one placement per node, wrapping
                # if the region offers fewer placements than there are nodes.
                # The list is a regional fact read at apply time by whoever
                # builds this component, not a constant.
                availability_domain=async_output(lambda position=index: self._placement(position, 0)),
                fault_domain=async_output(lambda position=index: self._placement(position, 1)),
                display_name=f'{name}-{node}',
                shape='VM.Standard.A1.Flex',
                shape_config=oci.core.InstanceShapeConfigArgs(ocpus=ocpus, memory_in_gbs=memory_gb),
                source_details=oci.core.InstanceSourceDetailsArgs(
                    source_type='image',
                    source_id=image_id,
                    boot_volume_size_in_gbs=str(boot_volume_gb),
                ),
                create_vnic_details=oci.core.InstanceCreateVnicDetailsArgs(
                    subnet_id=subnet_id,
                    assign_public_ip='true',
                    assign_ipv6ip=True,
                    display_name=f'{name}-{node}',
                ),
                # Talos reads its machine config from the metadata service,
                # base64-encoded by `user_data` once the configuration resolves.
                metadata={'user_data': pulumi.Output.from_input(machine_config).apply(user_data)},
                # Legacy IMDS serves that config without authentication; v2's
                # header is a static string, so the baseline network policy is
                # what actually keeps pods away from it (architecture.md §4.1).
                instance_options=oci.core.InstanceInstanceOptionsArgs(
                    are_legacy_imds_endpoints_disabled=True,
                ),
                opts=self.child_opts(),
            )

        # The one node the dedicated VIP is mapped onto. That is one
        # capability of that node and not a bundle: which node the block
        # volumes follow is a separate decision that happens to name the same
        # machine today (rfc-002 §10.5).
        self.dedicated_vip = self.instances[dedicated_vip_node]

        # The dedicated VIP: a reserved address, so a node rebuild does not
        # change it, 1:1-NAT'd onto a secondary private IP that a LoadBalancer
        # Service can claim and an egress policy can source from.
        self.secondary_ip = oci.core.PrivateIp(
            f'{name}-vip1-private',
            vnic_id=async_output(self._dedicated_vip_vnic_id),
            display_name=f'{name}-vip1',
            opts=self.child_opts(),
        )
        self.reserved_ip = oci.core.PublicIp(
            f'{name}-vip1',
            compartment_id=compartment_id,
            lifetime='RESERVED',
            private_ip_id=self.secondary_ip.id,
            display_name=f'{name}-vip1',
            # Identity-bearing: the address is registered with a third party.
            opts=self.child_opts(protect=True),
        )

        #: Node → the one GUA its primary VNIC holds: what every IPv6 backend
        #: on it names, and what `physical` exports for the `internet` pool
        #: (rfc-007 §4.4). One lookup per node.
        self.guas: dict[str, pulumi.Output[str]] = {
            node: async_output(lambda node=node: self._ipv6_address(node)) for node in self.instances
        }
        self.backends = [
            oci.networkloadbalancer.Backend(
                f'{name}-nlb-{named(entry.name, family)}-{node}',
                backend_set_name=load_balancer.backend_sets[family][entry.name].name,
                network_load_balancer_id=load_balancer.load_balancer.id,
                # An instance OCID stands for the primary VNIC's primary
                # private IP, which is IPv4, so an IPv6 backend names its
                # address instead -- the node's GUA, read off that VNIC. OCI's
                # console guide says an address-named backend cannot join a
                # source-preserving set; Oracle's cloud controller manager
                # declares its IPv6 backends exactly so (`getBackends`). The
                # first `up` that creates these settles it, and
                # declarative/physical.md §6 names the fallback.
                target_id=instance.id if family == 'IPV4' else None,
                ip_address=None if family == 'IPV4' else self.guas[node],
                # No `name`: pulumi-oci autonames it from the logical name
                # (`<logical>-<7 hex>`), so the name on the balancer carries
                # the field and the family too. A backend's port is not
                # updatable, so a port edit replaces the backend; an autonamed
                # replacement gets a fresh name and is created before the old
                # one is deleted, where a fixed name makes the provider delete
                # first and leaves the node out of the set until its
                # replacement lands.
                port=entry.port,
                opts=self.child_opts(),
            )
            for family in FAMILIES
            for entry in load_balancer.forwarded
            for node, instance in sorted(self.instances.items())
        ]

        self.register_outputs({})

    async def _placement(self, position: int, half: int) -> str:
        placements = await resolve(self._placements)
        return str(placements[position % len(placements)][half])

    async def _dedicated_vip_vnic_id(self) -> str:
        """The primary VNIC of the node that holds the dedicated VIP."""
        return await self._primary_vnic_id(self.dedicated_vip)

    async def _ipv6_address(self, node: str) -> str:
        """The one IPv6 address `node` holds: the GUA its primary VNIC was created with.

        The instance was created with `assign_ipv6ip`, so a VNIC reading back
        with none, or with more than one to choose between, is refused rather
        than guessed at: either would put a wrong address in a backend set.
        """
        vnic_id = await self._primary_vnic_id(self.instances[node])
        vnic = await resolve(
            oci.core.get_vnic_output(
                vnic_id=vnic_id,
                # Parented for the provider, as the attachment lookup is.
                opts=pulumi.InvokeOptions(parent=self),
            )
        )
        addresses = list(vnic.ipv6addresses or [])
        if len(addresses) != 1:
            raise ValueError(f'{node} holds {len(addresses)} IPv6 addresses on its primary VNIC, not one')
        return str(addresses[0])

    async def _primary_vnic_id(self, instance: oci.core.Instance) -> str:
        """The primary VNIC of `instance`.

        Instances expose their attachments rather than their VNICs, so the id
        is read back through the attachment list.
        """
        instance_id, compartment_id = await resolve(instance.id, instance.compartment_id)
        attachments = await resolve(
            oci.core.get_vnic_attachments_output(
                compartment_id=compartment_id,
                instance_id=instance_id,
                # Parented, which is how an invoke inherits this component's
                # provider rather than falling to the disabled default one.
                opts=pulumi.InvokeOptions(parent=self),
            )
        )
        return attachments.vnic_attachments[0].vnic_id
