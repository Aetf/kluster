"""The physical program as a whole.

A smoke test with teeth: it declares the entire stack against mocks, which is
what catches wiring mistakes — a resource argument the provider would reject,
or a dependency ordered so that the endpoint is needed before it exists.

The stack is also an inventory. Domains the design calls for but nobody has
written yet are still called, and refuse by name; the suite holds both halves
of that — the declared graph really is declared, and the first gap really does
say which domain it is.
"""

import json
from collections.abc import Callable
from typing import Any, cast

import pulumi
import pulumi.runtime.settings
import pytest
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

from kluster import conventions
from kluster.gateway import zerotier
from kluster.physical import homelab
from kluster.stacks import physical

LB_ADDRESS = '203.0.113.10'
LB_ADDRESS_V6 = '2001:db8::10'
VNIC_ID = 'ocid1.vnic.oc1.phx.augmented'
ZT_NETWORK_ID = '0123456789abcdef'

#: What the gateway's three channels read out of stack configuration: the site
#: facts the program cannot derive. Every value here is invented; what the test
#: is for is that the keys line up and the values reach the right resource.
GATEWAY_CONFIG = {
    'kluster:gatewayHostKey': 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample',
    'kluster:gatewayPrivateKey': '-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n',
    'kluster:gatewayBgpPassword': 'a-session-password',
    'kluster:gatewayAcmeToken': 'a-zone-scoped-token',
    'kluster:gatewayRootfs': json.dumps(
        {name: {'url': f'https://example.invalid/{name}.raw', 'sha256': 'f' * 64} for name in conventions.GW_ESTATE}
    ),
    'kluster:gatewayAddresses': json.dumps(
        {'caddy': '10.0.5.10', 'adguard-alice': '10.0.5.11', 'adguard-bob': '10.0.5.12'}
    ),
    'kluster:unifiApiUrl': 'https://gateway.invalid',
    'kluster:unifiApiKey': 'a-controller-key',
    'kluster:workerGua': '2001:db8:1:80::238',
    'kluster:qbittorrentPeerPort': '51413',
    'kluster:zerotierApiToken': 'a-central-token',
    'kluster:zerotierNetworkId': ZT_NETWORK_ID,
    'kluster:zerotierMembers': json.dumps(
        {
            entry.name: {'id': f'{index:010x}'} | ({} if entry.address else {'address': f'10.144.200.{index}'})
            for index, entry in enumerate(zerotier.ROSTER)
            if not entry.generated
        }
    ),
}


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        if args.typ == 'oci:Core/vcn:Vcn':
            outputs['ipv6cidrBlocks'] = ['2603:c020:8000:1200::/56']
        if args.typ == 'oci:NetworkLoadBalancer/networkLoadBalancer:NetworkLoadBalancer':
            outputs['ipAddresses'] = [
                {'ipAddress': LB_ADDRESS, 'isPublic': True, 'ipVersion': 'IPV4'},
                {'ipAddress': LB_ADDRESS_V6, 'isPublic': True, 'ipVersion': 'IPV6'},
            ]
        if args.typ == 'talos:machine/secrets:Secrets':
            outputs['machineSecrets'] = {'cluster': {'id': 'test'}}
        if args.typ == 'zerotier:index/identity:Identity':
            outputs |= {'identityId': f'{args.name}-node', 'publicKey': 'public', 'privateKey': 'private'}
        if args.typ == 'zerotier:index/network:Network':
            outputs['networkId'] = ZT_NETWORK_ID
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        match args.token:
            case 'unifi:index/getFirewallZone:getFirewallZone':
                name = str(cast('dict[str, Any]', args.args)['name'])
                return {'id': f'zone-{name}', 'name': name, 'networks': [], 'site': 'default'}, []
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
            **GATEWAY_CONFIG,
        }
    )
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='physical', preview=False)
    # A bridged SDK registers its own parameterized package before it may
    # register a resource, and it gates that on a feature flag read out of a
    # synchronous cache that only the async negotiation fills.
    _ = await pulumi.runtime.settings.monitor_supports_feature('parameterization')


@pytest.mark.asyncio
async def test_the_stack_declares_and_then_names_its_first_gap() -> None:
    # Everything above the gap is declared for real against the mocks, which
    # is where a wiring mistake would surface; the gap itself is the program
    # refusing to pretend the rest of the design exists.
    with pytest.raises(NotImplementedError, match=r'physical §1/§5 storage'):
        await physical.main()


@pytest.mark.asyncio
async def test_the_anchor_contract_is_exported_under_the_names_dns_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three outputs `dns` builds its anchors from, by the names it uses.

    An output name is not an implementation detail here: `dns` writes it into
    a record, and a record whose content changed name after an apply is a
    replacement rather than an update. Asserting against the `dns` module's
    own constants is what keeps the two halves of the contract from drifting
    apart silently.
    """
    from kluster.stacks import dns

    exported: dict[str, object] = {}

    def record(name: str, value: object) -> None:
        exported[name] = value

    monkeypatch.setattr(physical.pulumi, 'export', record)

    # The exports are declared above the stack's first unwritten domain, so
    # the run that reaches them is the same run that stops there.
    with pytest.raises(NotImplementedError):
        await physical.main()

    for output in (dns.OUTPUT_CLUSTER_V4, dns.OUTPUT_CLUSTER_V6, dns.OUTPUT_VIP1_V4):
        assert output in exported, output

    addresses = (
        cast('pulumi.Output[str]', exported[dns.OUTPUT_CLUSTER_V4]),
        cast('pulumi.Output[str]', exported[dns.OUTPUT_CLUSTER_V6]),
    )
    assert [await address.future() for address in addresses] == [LB_ADDRESS, LB_ADDRESS_V6]


@pytest.mark.asyncio
async def test_the_gateway_arm_reads_the_configuration_its_three_channels_need() -> None:
    """The one domain below the gap that is written, exercised on its own.

    A run stops at the first unwritten domain, so the gateway is unreachable
    through `main` today; declaring it directly is what keeps its wiring —
    every configuration key, and which of them is a secret — under test until
    the domains above it are written.
    """
    physical.declare_gateway(pulumi.Config())
    await wait_for_rpcs(await_all_outstanding_tasks=False)


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
