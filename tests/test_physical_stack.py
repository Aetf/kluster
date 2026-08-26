"""The physical program as a whole.

A smoke test with teeth: it declares the entire stack against mocks, which is
what catches wiring mistakes — a resource argument the provider would reject,
or a dependency ordered so that the endpoint is needed before it exists.

The stack is also an inventory. Domains the design calls for but nobody has
written yet are still called, and refuse by name; the suite holds both halves
of that — the declared graph really is declared, and the first gap really does
say which domain it is.
"""

from collections.abc import Callable
from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio

from kluster import gateway
from kluster.physical import homelab
from kluster.stacks import physical

LB_ADDRESS = '203.0.113.10'
VNIC_ID = 'ocid1.vnic.oc1.phx.augmented'


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        if args.typ == 'oci:Core/vcn:Vcn':
            outputs['ipv6cidrBlocks'] = ['2603:c020:8000:1200::/56']
        if args.typ == 'oci:NetworkLoadBalancer/networkLoadBalancer:NetworkLoadBalancer':
            outputs['ipAddresses'] = [{'ipAddress': LB_ADDRESS, 'isPublic': True}]
        if args.typ == 'talos:machine/secrets:Secrets':
            outputs['machineSecrets'] = {'cluster': {'id': 'test'}}
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        match args.token:
            case 'oci:Core/getServices:getServices':
                return {'services': [{'id': 'ocid1.service.os', 'name': 'Object Storage', 'cidrBlock': 'oci-os'}]}, []
            case 'oci:Core/getVnicAttachments:getVnicAttachments':
                return {'vnicAttachments': [{'vnicId': VNIC_ID}]}, []
            case 'oci:Identity/getAvailabilityDomains:getAvailabilityDomains':
                return {'availabilityDomains': [{'name': 'ZRbp:PHX-AD-1'}]}, []
            case 'oci:Identity/getFaultDomains:getFaultDomains':
                return {'faultDomains': [{'name': f'FAULT-DOMAIN-{n}'} for n in (1, 2, 3)]}, []
            case 'talos:machine/getConfiguration:getConfiguration':
                return {'machineConfiguration': 'machine: {}'}, []
            case 'talos:imagefactory/getUrls:getUrls':
                return {'urls': {'diskImage': 'https://factory.talos.dev/image/test/v1.11.0/oracle-arm64.qcow2'}}, []
            case _:
                return {}, []


@pytest_asyncio.fixture(autouse=True)
async def setup() -> None:
    pulumi.runtime.set_all_config(
        {
            'kluster:compartmentId': 'ocid1.compartment.test',
            'kluster:talosVersion': 'v1.11.0',
        }
    )
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='physical', preview=False)


@pytest.mark.asyncio
async def test_the_stack_declares_and_then_names_its_first_gap() -> None:
    # Everything above the gap is declared for real against the mocks, which
    # is where a wiring mistake would surface; the gap itself is the program
    # refusing to pretend the rest of the design exists.
    with pytest.raises(NotImplementedError, match=r'physical §1/§5 storage'):
        await physical.main()


#: Every domain of the design that has no implementation, and the text its
#: refusal must carry. A domain that quietly declared nothing would be far
#: worse than one that stops the run: the stack would come up looking whole.
SEAMS: list[tuple[str, Callable[[], object]]] = [
    (
        'physical §1/§5 storage',
        lambda: physical._declare_storage(  # pyright: ignore[reportPrivateUsage]
            compartment_id='ocid1.compartment.test',
            nodes=cast('Any', None),
        ),
    ),
    (
        'physical §1 guardrails',
        lambda: physical._declare_guardrails(compartment_id='ocid1.compartment.test'),  # pyright: ignore[reportPrivateUsage]
    ),
    (
        'physical §2 Talos day-1',
        lambda: physical._declare_talos_day1(  # pyright: ignore[reportPrivateUsage]
            cluster=cast('Any', None),
            nodes=cast('Any', None),
        ),
    ),
    (
        'physical §3 homelab',
        lambda: homelab.declare(
            'kluster',
            cluster=cast('Any', None),
            connection_uri='qemu+ssh://host/system',
            storage_dir='/var/lib/libvirt/kluster',
            bridge='kvmbr1',
            vcpus=12,
            memory_gib=10,
            disk_gb=60,
            haos_domain_uuid='00000000-0000-0000-0000-000000000000',
        ),
    ),
    (
        'physical §4 gateway',
        lambda: gateway.declare_estate(
            'kluster',
            host='10.144.1.1',
            host_key='ssh-ed25519 AAAA',
            private_key='-----BEGIN OPENSSH PRIVATE KEY-----',
            bgp_neighbour=cast('Any', None),
        ),
    ),
    (
        'physical §4 gateway',
        lambda: gateway.declare_firewall(
            'kluster',
            api_url='https://gateway.invalid',
            api_key='key',
            site='default',
            worker_gua='2001:db8::1',
        ),
    ),
    (
        'physical §4 gateway',
        lambda: gateway.declare_zerotier('kluster', api_token='token', network_id='0123456789abcdef'),
    ),
]


@pytest.mark.parametrize(('expected', 'seam'), SEAMS)
def test_an_unwritten_domain_refuses_by_name(expected: str, seam: Callable[[], object]) -> None:
    with pytest.raises(NotImplementedError, match=expected):
        seam()


def test_the_provider_sdks_import() -> None:
    """The bridged SDKs are committed, so a broken one is a broken checkout.

    Each one is generated from a Terraform provider through Pulumi's bridge
    and carries the parameterization that names its upstream; importing the
    resource the design actually uses is the cheapest proof that the
    generation produced something usable.
    """
    import pulumi_b2
    import pulumi_libvirt
    import pulumi_unifi
    import pulumi_zerotier

    assert pulumi_b2.Bucket
    assert pulumi_libvirt.Domain
    assert pulumi_unifi.FirewallZonePolicy
    assert pulumi_zerotier.Member
