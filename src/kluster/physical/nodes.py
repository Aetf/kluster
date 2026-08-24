"""The cloud nodes and the load balancer in front of them.

Three A1 instances, each a control-plane node *and* an ingress node
(architecture.md §1.1): etcd quorum lives in one region, and the same three
machines terminate public traffic. They are spread across fault domains, so
losing one takes neither quorum nor ingress with it.

One of the three is **augmented** — it additionally carries a block volume, a
secondary private IP, and the reserved public IP that OCI 1:1-NATs onto it.
Nothing about the node is workload-specific: it is simply the node with extra
storage and networking, and the dedicated-VIP workload finds it through
scheduling constraints declared beside the workload (architecture.md §3.2).

Listeners are not a fixed list. The management ports live here because they
belong to the cluster rather than to any service; a service's listener is
declared beside the service that needs it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pulumi
import pulumi_oci as oci

from putils import Component, async_output, resolve

#: Talos' API and the Kubernetes API. Both are cluster-level, both terminate
#: on the nodes themselves, and neither belongs to a workload.
MANAGEMENT_PORTS: tuple[int, ...] = (6443, 50000)


class CloudNodes(Component):
    """The three A1 nodes, their NLB, and the augmented node's extra address."""

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
        fault_domains: Sequence[str],
        availability_domain: pulumi.Input[str],
        augmented: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        if augmented not in machine_configs:
            raise ValueError(f'augmented node {augmented!r} is not among {sorted(machine_configs)}')

        self.instances: dict[str, oci.core.Instance] = {}
        for index, (node, machine_config) in enumerate(sorted(machine_configs.items())):
            self.instances[node] = oci.core.Instance(
                f'{name}-{node}',
                compartment_id=compartment_id,
                availability_domain=availability_domain,
                # Spread by construction: a fault domain per node, wrapping if
                # the region ever offers fewer than three.
                fault_domain=fault_domains[index % len(fault_domains)],
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
                # Talos reads its machine config from the metadata service.
                metadata={'user_data': machine_config},
                # Legacy IMDS serves that config without authentication; v2's
                # header is a static string, so the baseline network policy is
                # what actually keeps pods away from it (architecture.md §4.1).
                instance_options=oci.core.InstanceInstanceOptionsArgs(
                    are_legacy_imds_endpoints_disabled=True,
                ),
                opts=self.child_opts(),
            )

        self.augmented = self.instances[augmented]

        # The dedicated VIP: a reserved address, so a node rebuild does not
        # change it, 1:1-NAT'd onto a secondary private IP that a LoadBalancer
        # Service can claim and an egress policy can source from.
        self.secondary_ip = oci.core.PrivateIp(
            f'{name}-vip1-private',
            vnic_id=async_output(self._augmented_vnic_id),
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
            nlb_ip_version='IPV4_AND_IPV6',
            opts=self.child_opts(),
        )

        self.backend_sets = {
            port: oci.networkloadbalancer.BackendSet(
                f'{name}-nlb-{port}',
                name=f'port{port}',
                network_load_balancer_id=self.load_balancer.id,
                policy='FIVE_TUPLE',
                is_preserve_source=True,
                health_checker=oci.networkloadbalancer.BackendSetHealthCheckerArgs(protocol='TCP', port=port),
                opts=self.child_opts(),
            )
            for port in MANAGEMENT_PORTS
        }

        self.backends = [
            oci.networkloadbalancer.Backend(
                f'{name}-nlb-{port}-{node}',
                backend_set_name=self.backend_sets[port].name,
                network_load_balancer_id=self.load_balancer.id,
                target_id=instance.id,
                port=port,
                opts=self.child_opts(),
            )
            for port in MANAGEMENT_PORTS
            for node, instance in sorted(self.instances.items())
        ]

        self.listeners = [
            oci.networkloadbalancer.Listener(
                f'{name}-nlb-listener-{port}',
                name=f'port{port}',
                network_load_balancer_id=self.load_balancer.id,
                default_backend_set_name=self.backend_sets[port].name,
                port=port,
                protocol='TCP',
                opts=self.child_opts(),
            )
            for port in MANAGEMENT_PORTS
        ]

        self.register_outputs({})

    async def _augmented_vnic_id(self) -> str:
        """The primary VNIC of the augmented node.

        Instances expose their attachments rather than their VNICs, so the id
        is read back through the attachment list.
        """
        instance_id, compartment_id = await resolve(self.augmented.id, self.augmented.compartment_id)
        attachments = await oci.core.get_vnic_attachments_output(
            compartment_id=compartment_id,
            instance_id=instance_id,
        ).future()
        assert attachments is not None
        return attachments.vnic_attachments[0].vnic_id
