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
from types import SimpleNamespace
from typing import Any, cast

import pulumi
import pulumi.runtime.settings
import pytest
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

from kluster import conventions
from kluster.gateway import zerotier
from kluster.physical import homelab
from kluster.physical.guardrails import Guardrails
from kluster.stacks import physical

LB_ADDRESS = '203.0.113.10'
LB_ADDRESS_V6 = '2001:db8::10'
VNIC_ID = 'ocid1.vnic.oc1.phx.augmented'
AVAILABILITY_DOMAIN = 'ZRbp:PHX-AD-1'
AUGMENTED_ID = 'ocid1.instance.oc1.phx.augmented'
OBJECT_NAMESPACE = 'axmpletenancy'
TENANCY_ID = 'ocid1.tenancy.oc1..test'
IDCS_ENDPOINT = 'https://idcs-example.identity.oraclecloud.com'
IDENTITY_DOMAIN = 'Default'
B2_REGION = 'us-west-004'
BUDGET_RECIPIENTS = ['alerts@example.invalid', 'second@example.invalid']
ZT_NETWORK_ID = '0123456789abcdef'
KUBECONFIG = 'apiVersion: v1\nkind: Config\n'
TALOSCONFIG = 'context: kluster\n'

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
    'kluster:gatewayBgpPeer': '192.168.80.1/32',
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
        if args.typ == 'talos:cluster/kubeconfig:Kubeconfig':
            outputs['kubeconfigRaw'] = KUBECONFIG
        if args.typ == 'zerotier:index/identity:Identity':
            outputs |= {'identityId': f'{args.name}-node', 'publicKey': 'public', 'privateKey': 'private'}
        if args.typ == 'zerotier:index/network:Network':
            outputs['networkId'] = ZT_NETWORK_ID
        if args.typ == 'oci:Core/instance:Instance':
            outputs['availabilityDomain'] = AVAILABILITY_DOMAIN
        if args.typ == 'oci:Identity/domainsUser:DomainsUser':
            outputs['ocid'] = 'ocid1.user.oc1..chunks'
        if args.typ == 'oci:Identity/domainsCustomerSecretKey:DomainsCustomerSecretKey':
            outputs |= {'accessKey': 'chunk-access-key', 'secretKey': 'chunk-secret-key'}
        if args.typ == 'b2:index/bucket:Bucket':
            outputs['bucketId'] = 'b2-bucket-id'
        if args.typ == 'b2:index/applicationKey:ApplicationKey':
            outputs |= {'applicationKeyId': args.name + '-key-id', 'applicationKey': args.name + '-secret'}
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
            case 'talos:client/getConfiguration:getConfiguration':
                return {'talosConfig': TALOSCONFIG}, []
            case 'talos:cluster/getHealth:getHealth':
                return {'id': 'healthy'}, []
            case 'oci:ObjectStorage/getNamespace:getNamespace':
                return {'namespace': OBJECT_NAMESPACE}, []
            case 'talos:imageFactory/getUrls:getUrls':
                return {'urls': {'diskImage': 'https://factory.talos.dev/image/test/v1.11.0/oracle-arm64.qcow2'}}, []
            case _:
                return {}, []


#: The compartment the stack acts in, as `conventions` will carry it once the
#: mint has made it. It is patched in rather than configured because that is
#: what the program reads: a compartment is a boundary this repository decides,
#: so it is code and not a config key (credentials.md §3).
COMPARTMENT = conventions.Compartment(
    consumer=conventions.PHYSICAL,
    name=f'{conventions.CLUSTER_NAME}-{conventions.PHYSICAL}',
    ocid='ocid1.compartment.test',
)


#: The whole of what the stack reads out of configuration. Site facts the
#: program cannot derive, plus the OCI provider's own credential namespace —
#: the tenancy OCID is read from there rather than restated, because the mint
#: that issues the key writes it beside the key.
STACK_CONFIG = {
    'kluster:talosVersion': 'v1.11.0',
    'oci:tenancyOcid': TENANCY_ID,
    'kluster:ociIdentityDomainUrl': IDCS_ENDPOINT,
    'kluster:ociIdentityDomainName': IDENTITY_DOMAIN,
    'kluster:b2Region': B2_REGION,
    'kluster:budgetAlertRecipients': json.dumps(BUDGET_RECIPIENTS),
    # Read by the §3 domain, which announces itself as unwritten only after
    # its arguments have been evaluated.
    'kluster:libvirtUri': 'qemu+ssh://host.invalid/system',
    'kluster:libvirtStorageDir': '/var/lib/libvirt/kluster',
    'kluster:haosDomainUuid': '00000000-0000-0000-0000-000000000000',
    **GATEWAY_CONFIG,
}


@pytest_asyncio.fixture(autouse=True)
async def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(conventions.OCI_COMPARTMENTS, conventions.PHYSICAL, COMPARTMENT)
    pulumi.runtime.set_all_config(dict(STACK_CONFIG))
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
    with pytest.raises(NotImplementedError, match=r'physical §3 homelab'):
        await physical.main()


@pytest.mark.asyncio
async def test_a_compartment_that_does_not_exist_yet_names_the_command_that_makes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The one state the mapping can be in that the stack cannot act on: the
    # compartment is named but has never been created, so there is no OCID to
    # declare anything in. What matters is that the refusal names the command
    # that produces one -- a lookup failure here would say nothing at all.
    monkeypatch.setitem(
        conventions.OCI_COMPARTMENTS,
        conventions.PHYSICAL,
        conventions.Compartment(consumer=conventions.PHYSICAL, name=COMPARTMENT.name),
    )

    with pytest.raises(conventions.CompartmentMissing, match=r'credentials derived oci-physical mint'):
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
async def test_the_cluster_credentials_are_exported_and_stay_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two outputs every stack that speaks to the cluster is built on.

    They are cluster-admin credentials, so the interesting half of this is not
    that they exist but that they carry their secret marking all the way out to
    the stack export — an export that lost it would print the cluster's keys in
    a deployment log.
    """
    exported: dict[str, object] = {}

    def record(name: str, value: object) -> None:
        exported[name] = value

    monkeypatch.setattr(physical.pulumi, 'export', record)

    with pytest.raises(NotImplementedError):
        await physical.main()

    kubeconfig = cast('pulumi.Output[str]', exported['kubeconfig'])
    talosconfig = cast('pulumi.Output[str]', exported['talosconfig'])
    assert await kubeconfig.is_secret()
    assert await talosconfig.is_secret()
    assert await kubeconfig.future() == KUBECONFIG
    assert await talosconfig.future() == TALOSCONFIG


def declare_storage() -> physical.Storage:
    """§1 and §5 on their own, against a stub of the one node they read.

    The augmented instance is the whole of what this domain needs from the
    fleet: the availability domain a volume may be attached within, and the
    instance it attaches to. Stubbing it keeps the assertions below about the
    wiring rather than about the fleet, which `test_cloud_nodes` already holds.
    """
    nodes = SimpleNamespace(
        augmented=SimpleNamespace(
            availability_domain=pulumi.Output.from_input(AVAILABILITY_DOMAIN),
            id=pulumi.Output.from_input(AUGMENTED_ID),
        )
    )
    return physical._declare_storage(  # pyright: ignore[reportPrivateUsage]
        config=pulumi.Config(),
        compartment_id=COMPARTMENT.require(),
        tenancy_id=TENANCY_ID,
        nodes=cast('Any', nodes),
    )


def declare_guardrails() -> Guardrails:
    return physical._declare_guardrails(  # pyright: ignore[reportPrivateUsage]
        config=pulumi.Config(),
        compartment_id=COMPARTMENT.require(),
        tenancy_id=TENANCY_ID,
    )


@pytest.mark.asyncio
async def test_the_cache_volume_is_created_and_attached_where_the_augmented_node_is() -> None:
    """A block volume attaches only within its own availability domain.

    Both halves come off the instance rather than out of a constant, because
    the domain a node lands in is itself decided at apply time from what the
    region offers — a volume pinned to a remembered domain would fail to
    attach the first time the placement list came back in another order.
    """
    declared = declare_storage()

    assert await declared.cache.volume.availability_domain.future() == AVAILABILITY_DOMAIN
    assert await declared.cache.attachment.instance_id.future() == AUGMENTED_ID


@pytest.mark.asyncio
async def test_the_chunk_credential_is_granted_on_one_bucket_in_the_configured_domain() -> None:
    """The policy is the whole of what the S3 key may do.

    A customer secret key carries no scope of its own — it is only as confined
    as the group its user is in — so the single statement here is the blast
    radius, and it names the identity domain configuration supplies rather
    than assuming a tenancy's domain is called anything in particular.
    """
    from kluster.physical import storage

    declared = declare_storage()

    statements = await declared.chunks.policy.statements.future()
    assert statements == [
        storage.statement(
            domain=IDENTITY_DOMAIN,
            group=storage.CHUNK_IDENTITY,
            compartment_id=COMPARTMENT.require(),
            bucket=conventions.BUCKET_CHUNKS,
        )
    ]


@pytest.mark.asyncio
async def test_the_backup_bucket_is_not_hosted_by_the_provider_it_insures() -> None:
    """The placement rule, as the two endpoints a consumer is handed.

    Tenancy loss is an enumerated risk, so the bucket the cluster is rebuilt
    from answers on another provider entirely; the chunk bucket may sit in the
    nodes' own region because it backs a replica whose other full copy is on
    the homelab NAS (storage.md §4).
    """
    declared = declare_storage()

    assert declared.backup.endpoint == f'https://s3.{B2_REGION}.backblazeb2.com'
    chunk_endpoint = await declared.chunks.endpoint.future()
    assert chunk_endpoint == (
        f'https://{OBJECT_NAMESPACE}.compat.objectstorage.{conventions.OCI_REGION}.oraclecloud.com'
    )


@pytest.mark.asyncio
async def test_the_bucket_census_is_exported_for_the_stacks_that_fill_the_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Names, endpoints and credentials — what configuring a mover takes.

    Nothing downstream re-derives a bucket name from a convention and hopes it
    matches: the export is the contract. That each credential half is
    classified as a secret is a property of the resource it comes from, and
    is held by the suites for those components.
    """
    exported: dict[str, object] = {}

    def record(name: str, value: object) -> None:
        exported[name] = value

    monkeypatch.setattr(physical.pulumi, 'export', record)

    with pytest.raises(NotImplementedError):
        await physical.main()

    assert exported['chunk_bucket'] == conventions.BUCKET_CHUNKS
    assert exported['backup_bucket'] == conventions.BUCKET_BACKUP
    assert exported['backup_endpoint'] == f'https://s3.{B2_REGION}.backblazeb2.com'

    endpoint = cast('pulumi.Output[str]', exported['chunk_endpoint'])
    assert await endpoint.future() == (
        f'https://{OBJECT_NAMESPACE}.compat.objectstorage.{conventions.OCI_REGION}.oraclecloud.com'
    )
    secret_key = cast('pulumi.Output[str]', exported['chunk_secret_key'])
    assert await secret_key.future() == 'chunk-secret-key'

    # The one consumer that exists whether or not any application does.
    keys = cast('dict[str, dict[str, pulumi.Output[str]]]', exported['backup_keys'])
    assert set(keys) == {'etcd'}
    assert await keys['etcd']['id'].future() == 'kluster-backup-etcd-key-id'


@pytest.mark.asyncio
async def test_the_quota_zeroes_a_family_before_it_allows_a_shape_in_it() -> None:
    """Statement order is the default-deny.

    A later statement supersedes an earlier one for the same quota, so the
    wildcard `zero` has to come first: reversed, the policy would end up
    allowing nothing at all, and a fleet that cannot replace a node is exactly
    what the envelope must not forbid.
    """
    guardrails = declare_guardrails()
    statements = guardrails.statements

    for family in ('compute-core', 'compute-memory'):
        zeroed = next(index for index, text in enumerate(statements) if text.startswith(f'zero {family}'))
        allowed = next(index for index, text in enumerate(statements) if text.startswith(f'set {family}'))
        assert zeroed < allowed, family

    # Quota statements have no OCID form: the compartment appears by the name
    # this program gave it, which is why that name is a convention.
    assert all(text.endswith(f'in compartment {COMPARTMENT.name}') for text in statements)


@pytest.mark.asyncio
async def test_the_budget_alerts_reach_the_addresses_configuration_names() -> None:
    """The only signal this stack raises that does not go through the cluster."""
    guardrails = declare_guardrails()

    for rule in guardrails.alerts.values():
        assert await rule.recipients.future() == ','.join(BUDGET_RECIPIENTS)


def test_a_recipient_list_that_is_not_a_list_of_addresses_is_refused() -> None:
    pulumi.runtime.set_all_config(
        dict(STACK_CONFIG) | {'kluster:budgetAlertRecipients': json.dumps('one@example.invalid')}
    )

    with pytest.raises(TypeError, match='list of email addresses'):
        declare_guardrails()


def test_the_tenancy_is_read_from_the_key_the_mint_writes() -> None:
    """One value, written by one command and read by one program.

    The tenancy OCID is not configuration of this program's own: it is part of
    the signing configuration the credential mint installs. Asserting against
    the minter's constant is what keeps the reader from drifting onto a key
    nothing fills.
    """
    from kluster.scripts.credentials import derived

    assert f'{physical.OCI_NAMESPACE}:{physical.OCI_TENANCY_KEY}' == derived.OCI_TENANCY_KEY


#: Every site fact the stack takes as configuration, including the tenancy it
#: reads out of the OCI provider's own namespace. A first `up` is run against a
#: half-filled configuration more often than not, so what matters is that each
#: missing value stops the run by naming itself rather than failing later
#: inside a provider call.
SITE_FACTS = [
    'oci:tenancyOcid',
    'kluster:ociIdentityDomainUrl',
    'kluster:ociIdentityDomainName',
    'kluster:b2Region',
    'kluster:budgetAlertRecipients',
]


@pytest.mark.parametrize('key', SITE_FACTS)
@pytest.mark.asyncio
async def test_a_site_fact_the_configuration_lacks_refuses_by_name(key: str) -> None:
    pulumi.runtime.set_all_config({name: value for name, value in STACK_CONFIG.items() if name != key})

    with pytest.raises(pulumi.ConfigMissingError, match=key.partition(':')[2]):
        await physical.main()


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
