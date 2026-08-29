"""The physical program as a whole.

A smoke test with teeth: it declares the entire stack against mocks, which is
what catches wiring mistakes — a resource argument the provider would reject,
or a dependency ordered so that the endpoint is needed before it exists.

The stack is also an inventory, and as of the libvirt domain it is a complete
one: every domain the design calls for is written, so `main` runs end to end
here rather than stopping at a named gap. What replaces that gap as a test is
the same worry stated positively — each provider of the design has to appear in
what the run registered, because a domain that quietly declared nothing would
leave a stack that comes up looking whole.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pulumi
import pulumi.runtime.settings
import pytest
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

from compartments import with_compartment
from kluster import conventions
from kluster.components import homelab
from kluster.components.cloud import nodes
from kluster.components.cloud.guardrails import Guardrails
from kluster.lib import workstation
from kluster.stacks import physical

LB_ADDRESS = '203.0.113.10'
LB_ADDRESS_V6 = '2001:db8::10'
VNIC_ID = 'ocid1.vnic.oc1.phx.vip'
AVAILABILITY_DOMAIN = 'ZRbp:PHX-AD-1'
OBJECT_NAMESPACE = 'axmpletenancy'
TENANCY_ID = 'ocid1.tenancy.oc1..test'
BUDGET_RECIPIENTS = ['alerts@example.invalid', 'second@example.invalid']
ZT_NETWORK_ID = '0123456789abcdef'
#: A LAN name for the gateway, as the first-bring-up knob carries one. Its shape
#: is all that matters here: something that is not the overlay address.
BOOTSTRAP_HOST = 'gateway.invalid'
#: The worker VM's global address, as the operator reads it off the cluster
#: VLAN's router advertisement. Nothing derives it — that is the point of the
#: key — so the value only has to be inside a documentation prefix.
WORKER_GUA = '2001:db8:1:70::10'
KUBECONFIG = 'apiVersion: v1\nkind: Config\n'
TALOSCONFIG = 'context: kluster\n'
#: The libvirt client identity, as it arrives from configuration. Nothing
#: parses it here — the run writes it to a file and hands the provider that
#: path — so its shape only has to be something no test could mistake for real.
LIBVIRT_KEY = '-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n-----END OPENSSH PRIVATE KEY-----\n'

#: What the gateway's three channels read out of stack configuration: the site
#: facts the program cannot derive. Every value here is invented; what the test
#: is for is that the keys line up and the values reach the right resource.
GATEWAY_CONFIG = {
    'kluster:gatewayHostKey': 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample',
    'kluster:gatewayPrivateKey': '-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n',
    'kluster:gatewayBgpPassword': 'a-session-password',
    'kluster:gatewayAcmeToken': 'a-zone-scoped-token',
    'kluster:gatewayRootfs': json.dumps(
        {
            service.name: {'url': f'https://example.invalid/{service.name}.raw', 'sha256': 'f' * 64}
            for service in conventions.GW_SERVICES
        }
    ),
    'kluster:unifiApiKey': 'a-controller-key',
    'kluster:workerGua': WORKER_GUA,
    'kluster:qbittorrentPeerPort': '51413',
    'kluster:zerotierApiToken': 'a-central-token',
    'kluster:zerotierNetworkId': ZT_NETWORK_ID,
    'kluster:zerotierMembers': json.dumps(
        {
            entry.name: {'id': f'{index:010x}'} | ({} if entry.address else {'address': f'10.144.200.{index}'})
            for index, entry in enumerate(conventions.ZT_ROSTER)
            if not entry.generated
        }
    ),
}


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        #: Every resource type the run registered, so a test can ask which
        #: providers the program actually reached.
        self.registered: set[str] = set()
        #: What each resource was declared with, by name, so a test can ask
        #: what the stack handed a provider rather than only that it made one.
        self.inputs: dict[str, dict[str, Any]] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        self.registered.add(args.typ)
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        self.inputs[args.name] = dict(cast('dict[str, Any]', args.inputs))
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
            # Keyed by the resource name, so a test can tell one member's key
            # material from another's: which identity an export carries is the
            # whole of the contract the credential map reads it under.
            outputs |= {
                'identityId': f'{args.name}-node',
                'publicKey': 'public',
                'privateKey': f'{args.name}-secret',
            }
        if args.typ == 'zerotier:index/network:Network':
            outputs['networkId'] = ZT_NETWORK_ID
        if args.typ == 'oci:Core/instance:Instance':
            outputs['availabilityDomain'] = AVAILABILITY_DOMAIN
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
                # Two artefacts of the same family: the cloud nodes' OCI image
                # and the worker's `nocloud` disk image, which the factory
                # serves compressed.
                platform = str(cast('dict[str, Any]', args.args)['platform'])
                suffix = 'raw.xz' if platform == 'nocloud' else 'qcow2'
                url = f'https://factory.talos.dev/image/test/v1.11.0/{platform}-arch.{suffix}'
                return {'urls': {'diskImage': url}}, []
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
    'kluster:budgetAlertRecipients': json.dumps(BUDGET_RECIPIENTS),
    # The §3 domain: the credential the host is reached with, where the
    # worker's image and seed are written, and which domain is adopted rather
    # than built. There is no endpoint among them — it is derived.
    'kluster:libvirtPrivateKey': LIBVIRT_KEY,
    'kluster:libvirtStorageDir': '/var/lib/libvirt/kluster',
    'kluster:haosDomainUuid': '00000000-0000-0000-0000-000000000000',
    **GATEWAY_CONFIG,
}


@pytest_asyncio.fixture(autouse=True)
async def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Mocks:
    with_compartment(monkeypatch, COMPARTMENT)
    # The run materializes the libvirt session's credential into the checkout's
    # `.credentials/`, so every test here is pointed at a directory of its own:
    # a test suite that wrote into the tree it runs from would leave a key
    # behind and, worse, overwrite the operator's.
    monkeypatch.setattr(workstation, 'directory', lambda: tmp_path / workstation.DIRECTORY)
    pulumi.runtime.set_all_config(dict(STACK_CONFIG))
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project='kluster', stack='physical', preview=False)
    # A bridged SDK registers its own parameterized package before it may
    # register a resource, and it gates that on a feature flag read out of a
    # synchronous cache that only the async negotiation fills.
    _ = await pulumi.runtime.settings.monitor_supports_feature('parameterization')
    return mocks


#: The provider of each domain the design has, by the prefix its type tokens
#: carry: the cloud, the Talos chain, the homelab host, the backup account, the
#: gateway's controller and the overlay.
DOMAIN_PROVIDERS = ('oci', 'talos', 'libvirt', 'b2', 'unifi', 'zerotier')


@pytest.mark.asyncio
async def test_the_stack_declares_every_domain_of_the_design(setup: Mocks) -> None:
    # The whole program against the mocks, which is where a wiring mistake
    # surfaces — an argument the provider would reject, or a dependency that
    # needs the endpoint before it exists.
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    # And the inventory property: a domain that declared nothing at all would
    # leave a stack that runs clean and comes up one provider short.
    families = {typ.partition(':')[0] for typ in setup.registered}
    assert set(DOMAIN_PROVIDERS) <= families


@pytest.mark.asyncio
async def test_the_controller_is_dialled_where_the_roster_placed_the_gateway(setup: Mocks) -> None:
    """The controller's address is derived, not recorded beside its key.

    The gateway's overlay address is handed out by this program's own ZeroTier
    roster, so every client of the gateway reads it from the same constant:
    the estate's SSH host, the controller's API endpoint, and the next hop of
    every managed route. A value typed in beside the API key would be a second
    copy of that, free to disagree with the roster that decides it.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    assert setup.inputs[f'{conventions.CLUSTER_NAME}-unifi']['apiUrl'] == f'https://{conventions.ZT_UDM}'
    assert setup.inputs[f'{conventions.CLUSTER_NAME}-frr']['host'] == str(conventions.ZT_UDM)
    # And nothing supplies it: the stack has no key to read it from, so a
    # `record` command that pushed one would be filling a slot nobody reads.
    assert not [key for key in STACK_CONFIG if 'ApiUrl' in key]
    # The steady state is the knob's absence, so nothing has to be unset to
    # reach this.
    assert f'kluster:{physical.GATEWAY_BOOTSTRAP_HOST}' not in STACK_CONFIG


@pytest.mark.asyncio
async def test_the_cluster_zone_is_opened_to_the_home_with_the_iot_vlan_carved_out(setup: Mocks) -> None:
    """The zone matrix as the whole run declares it, not as one component does.

    A zone the controller has just been told about is denied against every
    other zone in both directions, so each direction the design wants open is
    a policy the stack has to declare — and the one direction it does not want
    open, the IoT VLAN into the node subnet, is a drop that has to be ordered
    *ahead* of the zone-wide allow beside it. Both properties are wiring: they
    hold only if the stack reaches this arm of the gateway at all, which is
    what running the program rather than the component proves.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    name = conventions.CLUSTER_NAME
    zone = f'{name}-zone_id'
    internal = 'zone-Internal'

    outward = setup.inputs[f'{name}-cluster-internal']
    assert outward['action'] == 'ALLOW'
    assert outward['source']['zoneId'] == zone
    assert outward['destination']['zoneId'] == internal

    inward = setup.inputs[f'{name}-internal-cluster']
    assert inward['action'] == 'ALLOW'
    assert inward['source']['zoneId'] == internal
    assert inward['destination']['zoneId'] == zone

    # Two drops, one per family, because the source is a literal subnet.
    for suffix, source in (('v4', str(conventions.IOT_VLAN.v4)), ('v6', str(conventions.IOT_VLAN.v6))):
        drop = setup.inputs[f'{name}-iot-cluster-{suffix}']
        assert drop['action'] == 'BLOCK'
        assert drop['source']['ips'] == [source]
        assert drop['destination']['zoneId'] == zone

    # The drops first: the allow behind them is the broad one here, so an
    # allow declared ahead of them would answer for the IoT VLAN as well.
    assert setup.inputs[f'{name}-internal-cluster-order']['beforePredefinedIds'] == [
        f'{name}-iot-cluster-v4_id',
        f'{name}-iot-cluster-v6_id',
        f'{name}-internal-cluster_id',
    ]
    assert setup.inputs[f'{name}-cluster-internal-order']['beforePredefinedIds'] == [f'{name}-cluster-internal_id']


@pytest.mark.asyncio
async def test_the_libvirt_session_is_dialled_where_the_roster_placed_the_host(
    setup: Mocks,
    tmp_path: Path,
) -> None:
    """The libvirt endpoint is derived from the census and the checkout.

    Nothing about this URI can be recorded in committed configuration. The
    address belongs to the overlay census the same run authorizes, and the two
    paths in it exist only on the machine running the program — a workstation
    on one run and a continuous-integration runner on the next — so a URI typed
    into the stack would be wrong for one of them and stale for both.
    """
    configured = cast('dict[str, Any]', json.loads(GATEWAY_CONFIG['kluster:zerotierMembers']))
    address = cast('str', configured[conventions.ZT_MEMBER_HOMELAB]['address'])

    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    uri = cast('str', setup.inputs[f'{conventions.CLUSTER_NAME}-libvirt']['uri'])
    parts = urlsplit(uri)
    assert parts.scheme == 'qemu+ssh'
    assert parts.netloc == f'{homelab.LIBVIRT_USER}@{address}'
    assert parts.path == '/system'

    # The files the provider opens, in the slot this checkout keeps local
    # secrets in — and the identity in them is the configured one.
    slot = tmp_path / workstation.DIRECTORY / homelab.SLOT
    query = parse_qs(parts.query)
    assert query['keyfile'] == [str(slot / homelab.KEYFILE)]
    assert query['knownhosts'] == [str(slot / homelab.KNOWN_HOSTS)]
    assert (slot / homelab.KEYFILE).read_text() == LIBVIRT_KEY
    # The pin is written against the address the URI dials: a `known_hosts`
    # entry keyed on anything else matches nothing the session sees.
    assert (slot / homelab.KNOWN_HOSTS).read_text() == f'{address} {conventions.HOMELAB_HOST_KEY}\n'
    # And no key holds any of it: what is configured is the credential alone.
    assert not [key for key in STACK_CONFIG if 'libvirtUri' in key]


@pytest.mark.asyncio
async def test_the_bootstrap_knob_moves_both_doors_to_the_gateway_at_once(setup: Mocks) -> None:
    """First bring-up dials the device over the LAN, on both channels.

    The overlay address answers only once the estate's ZeroTier container is on
    the device, and the estate is what this run delivers. While the knob is set
    it therefore replaces the dial address for both providers that reach the
    gateway — the desired-state push over SSH and the controller's API — because
    they are the same box behind two ports, and an override that moved one of
    them would leave the run half able to reach it.
    """
    pulumi.runtime.set_all_config(dict(STACK_CONFIG) | {f'kluster:{physical.GATEWAY_BOOTSTRAP_HOST}': BOOTSTRAP_HOST})

    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    assert setup.inputs[f'{conventions.CLUSTER_NAME}-frr']['host'] == BOOTSTRAP_HOST
    assert setup.inputs[f'{conventions.CLUSTER_NAME}-unifi']['apiUrl'] == f'https://{BOOTSTRAP_HOST}'
    # Nothing else about the channels moves — the pin in particular is a bare
    # key with no host name in front of it, so it matches the device at either
    # address (`test_gw_provider`).
    assert setup.inputs[f'{conventions.CLUSTER_NAME}-frr']['port'] == 22


@pytest.mark.asyncio
async def test_first_bring_up_runs_before_the_gateway_has_an_identity_to_authorize(setup: Mocks) -> None:
    """The nested egg: the id is minted by the container this run delivers.

    A ZeroTier node id comes into being when the daemon first runs on a device,
    and the daemon reaches the gateway as part of the estate. So the first apply
    of all is asked to run with no `zerotierMembers` entry for it, which is a
    refusal in the steady state and permitted only while the bootstrap knob is
    set. Nothing else about the overlay changes: the network, the routes and
    every other member are declared as usual.
    """
    members = {
        name: entry
        for name, entry in cast('dict[str, Any]', json.loads(GATEWAY_CONFIG['kluster:zerotierMembers'])).items()
        if name != conventions.ZT_MEMBER_UDM
    }
    pulumi.runtime.set_all_config(
        dict(STACK_CONFIG)
        | {
            f'kluster:{physical.GATEWAY_BOOTSTRAP_HOST}': BOOTSTRAP_HOST,
            'kluster:zerotierMembers': json.dumps(members),
        }
    )

    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    assert f'{conventions.CLUSTER_NAME}-member-{conventions.ZT_MEMBER_UDM}' not in setup.inputs
    assert f'{conventions.CLUSTER_NAME}-member-haos' in setup.inputs
    assert f'{conventions.CLUSTER_NAME}-network' in setup.inputs


@pytest.mark.asyncio
async def test_the_roster_gap_is_refused_once_the_gateway_is_on_the_overlay() -> None:
    """Unsetting the knob is what makes the census complete again.

    The relaxation belongs to the bring-up and to nothing else: with no knob
    set, a missing gateway id is the hole in the census it has always been, and
    it stops the run by naming the entry rather than declaring an overlay the
    gateway is not on.
    """
    members = {
        name: entry
        for name, entry in cast('dict[str, Any]', json.loads(GATEWAY_CONFIG['kluster:zerotierMembers'])).items()
        if name != conventions.ZT_MEMBER_UDM
    }
    pulumi.runtime.set_all_config(dict(STACK_CONFIG) | {'kluster:zerotierMembers': json.dumps(members)})

    with pytest.raises(ValueError, match=f'no configured node id for {conventions.ZT_MEMBER_UDM}'):
        await physical.main()


@pytest.mark.asyncio
async def test_the_pinhole_waits_for_an_address_the_worker_has_not_formed_yet(setup: Mocks) -> None:
    """The second nested egg: the address is SLAAC off a network this run makes.

    The worker's global address is formed from the router advertisement of the
    cluster VLAN, and that VLAN is declared by this same program — so the first
    apply of all is asked for a value only its own outcome produces. `workerGua`
    is therefore optional, and absent it the one rule that names a literal
    address is not declared: the worker's IPv6 is outbound-only, which is the
    stage the design already accepts when the home prefix rotates under a rule
    that has not been re-applied yet.
    """
    pulumi.runtime.set_all_config({key: value for key, value in STACK_CONFIG.items() if key != 'kluster:workerGua'})

    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    assert f'{conventions.CLUSTER_NAME}-peer-v6' not in setup.inputs
    # Nothing else waits with it. The v4 half names the node address the
    # address plan states rather than one a booted machine reports, and the
    # rest of the census never named the worker at all.
    assert f'{conventions.CLUSTER_NAME}-peer-v4' in setup.inputs
    assert f'{conventions.CLUSTER_NAME}-cluster-egress' in setup.inputs
    assert f'{conventions.CLUSTER_NAME}-network' in setup.inputs


@pytest.mark.asyncio
async def test_the_pinhole_admits_the_configured_address_once_it_is_known(setup: Mocks) -> None:
    """And with the address configured, the rule is back and carries it.

    Step three of the bring-up ceremony is writing the key, so what follows it
    has to be the pinhole itself — the configured address on the configured
    port, into the zone the worker moved to.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    destination = setup.inputs[f'{conventions.CLUSTER_NAME}-peer-v6']['destination']
    assert destination['ips'] == [WORKER_GUA]
    # A number over the wire arrives as one, whatever configuration spelled it.
    assert int(destination['port']) == int(GATEWAY_CONFIG['kluster:qbittorrentPeerPort'])


@pytest.mark.asyncio
async def test_a_compartment_that_does_not_exist_yet_names_the_command_that_makes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The one state the mapping can be in that the stack cannot act on: the
    # compartment is named but has never been created, so there is no OCID to
    # declare anything in. What matters is that the refusal names the command
    # that produces one -- a lookup failure here would say nothing at all.
    with_compartment(monkeypatch, conventions.Compartment(consumer=conventions.PHYSICAL, name=COMPARTMENT.name))

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

    await physical.main()

    for output in (dns.OUTPUT_CLUSTER_V4, dns.OUTPUT_CLUSTER_V6, dns.OUTPUT_VIP1_V4):
        assert output in exported, output

    addresses = (
        cast('pulumi.Output[str]', exported[dns.OUTPUT_CLUSTER_V4]),
        cast('pulumi.Output[str]', exported[dns.OUTPUT_CLUSTER_V6]),
    )
    assert [await address.future() for address in addresses] == [LB_ADDRESS, LB_ADDRESS_V6]


@pytest.mark.asyncio
async def test_the_overlay_addresses_are_exported_for_the_records_dns_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What crosses to `dns` is the half of the roster this program is told.

    `dns` publishes a `*.zt` host record per member and takes the member names
    from the roster as code, so the export carries addresses alone — and only
    those ZeroTier Central assigned. The members whose address this repository
    decides are absent by construction: they are conventions both stacks read
    from the same module, and exporting them would be a second copy free to
    disagree. The gateway is one of them, which is why the block `dns`
    declares does not wait on the identity this stack's own run mints.
    """
    from kluster.stacks import dns

    exported: dict[str, object] = {}

    def record(name: str, value: object) -> None:
        exported[name] = value

    monkeypatch.setattr(physical.pulumi, 'export', record)

    await physical.main()

    configured = cast('dict[str, Any]', json.loads(GATEWAY_CONFIG['kluster:zerotierMembers']))
    assert exported[dns.OUTPUT_ZT_ADDRESSES] == {
        name: entry['address'] for name, entry in configured.items() if 'address' in entry
    }
    assert conventions.ZT_MEMBER_UDM not in cast('dict[str, str]', exported[dns.OUTPUT_ZT_ADDRESSES])


@pytest.mark.asyncio
async def test_the_ci_join_credentials_are_exported_under_the_names_the_slot_map_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identities CI joins the overlay with, by the names `derived sync` reads.

    The slot map's half of that contract is a stack output name
    (`slots.StateRead`) and this program's half is the export; nothing but the
    string ties them together, and the map's own tests supply the output by
    hand, so a rename on either side would surface first as a bring-up that
    cannot fill `ZEROTIER_IDENTITY`. Which member each export carries is part
    of the contract rather than cosmetic: an identity live in two jobs at once
    flaps, which is why there is one per identity domain (gateway.md §2.6). The
    marking is checked for the same reason it is on the cluster credentials
    below — a join credential printed into a deployment log is a leaked one.
    """
    from kluster.scripts.credentials import slots

    exported: dict[str, object] = {}

    def record(name: str, value: object) -> None:
        exported[name] = value

    monkeypatch.setattr(physical.pulumi, 'export', record)

    await physical.main()

    contracted = {
        row.source.output
        for row in slots.ROWS.values()
        if isinstance(row.source, slots.StateRead) and row.source.stack == slots.PHYSICAL_STACK
    }
    # Every continuous-integration member the roster carries gets an export:
    # one added without one would join no job, having no secret to be pushed.
    assert set(physical.CI_IDENTITY_OUTPUTS) == set(conventions.ZT_CI_MEMBERS)
    assert contracted >= set(physical.CI_IDENTITY_OUTPUTS.values())
    assert contracted <= set(exported)

    for member, output in physical.CI_IDENTITY_OUTPUTS.items():
        identity = cast('pulumi.Output[str]', exported[output])
        assert await identity.is_secret()
        assert await identity.future() == f'{conventions.CLUSTER_NAME}-identity-{member}-secret'


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

    await physical.main()

    kubeconfig = cast('pulumi.Output[str]', exported['kubeconfig'])
    talosconfig = cast('pulumi.Output[str]', exported['talosconfig'])
    assert await kubeconfig.is_secret()
    assert await talosconfig.is_secret()
    assert await kubeconfig.future() == KUBECONFIG
    assert await talosconfig.future() == TALOSCONFIG


@pytest.mark.asyncio
async def test_the_worker_is_configured_through_the_cluster_endpoint(setup: Mocks) -> None:
    """The worker's apid is reached without a route to its LAN address.

    apid routes by the node a call names, so the worker's configuration apply
    names the cluster-VLAN address the machine answers on and dials the
    balancer, which forwards the machine API port to whichever control plane it
    likes; that control plane proxies the call the rest of the way over the
    mesh. Nothing outside the site therefore needs a path to the worker, which
    is why a continuous-integration run confined to the overlay's four targets
    can still carry a worker configuration change.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    worker = setup.inputs[f'{conventions.CLUSTER_NAME}-{conventions.HOMELAB_NODE}-config']
    assert worker['node'] == str(conventions.HOMELAB_NODE_IPV4)
    assert worker['endpoint'] == LB_ADDRESS
    # And the balancer forwards that port, or the endpoint above is a closed
    # door: the machine API is one of the two management ports it listens on.
    assert 50000 in nodes.MANAGEMENT_PORTS


#: The instance id each node's stub answers with, so an attachment can be
#: asserted to have landed on the node the volume table names.
INSTANCE_IDS = {node: f'ocid1.instance.oc1.phx.{node}' for node in conventions.CLOUD_NODES}


def declare_storage() -> physical.Storage:
    """§1 and §5 on their own, against stubs of the nodes they read.

    An instance is the whole of what this domain needs from the fleet: the
    availability domain a volume may be attached within, and the instance it
    attaches to. Stubbing them keeps the assertions below about the wiring
    rather than about the fleet, which `test_cloud_nodes` already holds.
    """
    nodes = SimpleNamespace(
        instances={
            node: SimpleNamespace(
                availability_domain=pulumi.Output.from_input(AVAILABILITY_DOMAIN),
                id=pulumi.Output.from_input(instance_id),
            )
            for node, instance_id in INSTANCE_IDS.items()
        }
    )
    return physical._declare_storage(  # pyright: ignore[reportPrivateUsage]
        compartment_id=COMPARTMENT.require(),
        nodes=cast('Any', nodes),
    )


def declare_guardrails() -> Guardrails:
    return physical._declare_guardrails(  # pyright: ignore[reportPrivateUsage]
        config=pulumi.Config(),
        compartment_id=COMPARTMENT.require(),
        tenancy_id=TENANCY_ID,
    )


@pytest.mark.asyncio
async def test_every_volume_is_attached_to_the_node_the_table_names() -> None:
    """A block volume attaches only within its own availability domain.

    Both halves come off the instance rather than out of a constant, because
    the domain a node lands in is itself decided at apply time from what the
    region offers — a volume pinned to a remembered domain would fail to
    attach the first time the placement list came back in another order. Which
    instance each one lands on is the table's answer, and for the following
    volume that answer is the node holding the dedicated VIP.
    """
    declared = declare_storage()

    for name, volume in conventions.NODE_VOLUMES.items():
        component = declared.volumes[name]
        assert await component.volume.availability_domain.future() == AVAILABILITY_DOMAIN
        assert await component.volume.size_in_gbs.future() == str(volume.size_gb)
        assert await component.attachment.instance_id.future() == INSTANCE_IDS[volume.attached_node]

    following = declared.volumes['hath-cache']
    assert await following.attachment.instance_id.future() == INSTANCE_IDS[conventions.DEDICATED_VIP_NODE]


@pytest.mark.asyncio
async def test_the_backup_bucket_is_not_hosted_by_the_provider_it_insures() -> None:
    """The placement rule, as the endpoint a consumer is handed.

    Tenancy loss is an enumerated risk, so the bucket the cluster is rebuilt
    from answers on another provider entirely (storage.md §4). Its region is an
    account property rather than stack configuration, so the endpoint is
    derivable from `conventions` alone.
    """
    declared = declare_storage()

    assert declared.backup.endpoint == f'https://s3.{conventions.B2_ACCOUNT.region}.backblazeb2.com'


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

    await physical.main()

    assert exported['backup_bucket'] == conventions.BUCKET_BACKUP
    assert exported['backup_endpoint'] == f'https://s3.{conventions.B2_ACCOUNT.region}.backblazeb2.com'

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
    'kluster:budgetAlertRecipients',
    'kluster:libvirtPrivateKey',
    'kluster:libvirtStorageDir',
    'kluster:haosDomainUuid',
]


@pytest.mark.parametrize('key', SITE_FACTS)
@pytest.mark.asyncio
async def test_a_site_fact_the_configuration_lacks_refuses_by_name(key: str) -> None:
    pulumi.runtime.set_all_config({name: value for name, value in STACK_CONFIG.items() if name != key})

    with pytest.raises(pulumi.ConfigMissingError, match=key.partition(':')[2]):
        await physical.main()


@pytest.mark.asyncio
async def test_the_gateway_arm_reads_the_configuration_its_three_channels_need() -> None:
    """The gateway's three channels, exercised without the rest of the stack.

    `main` reaches it now, but a failure there names the whole program; this
    isolates the arm whose wiring is entirely configuration — every key, and
    which of them is a secret — so a missing one is reported against the
    gateway rather than against a run of everything.
    """
    config = pulumi.Config()
    physical.declare_gateway(config, physical.read_overlay(config))
    await wait_for_rpcs(await_all_outstanding_tasks=False)


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
