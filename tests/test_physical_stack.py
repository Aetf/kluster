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
from kluster.components.overlay import flow_rules
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

#: A hex SHA-256 digest, as a root filesystem pin carries one. Nothing here
#: checks the bytes behind it; the shape is what the reader is checked against.
DIGEST = 'f' * 64
#: The release the pins below name. Invented, like the digest: what matters is
#: that the convention turns the pair into the URL and digest a push carries.
ROOTFS_RELEASE = 'rootfs-7'

#: What the gateway reads out of stack configuration: two secrets a file's
#: content is rendered from, the controller's key, and one measurement. Every
#: value here is invented; what the test is for is that the keys line up and the
#: values reach the right resource.
GATEWAY_CONFIG = {
    'kluster:gatewayPrivateKey': '-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n',
    'kluster:gatewayBgpPassword': 'a-session-password',
    'kluster:gatewayAcmeToken': 'a-zone-scoped-token',
    'kluster:unifiApiKey': 'a-controller-key',
    'kluster:workerGua': WORKER_GUA,
    'kluster:zerotierApiToken': 'a-central-token',
}

#: The version pins, in the namespace they share (rfc-002 §11.1). They are
#: project-level configuration in the committed tree — one copy for five stacks
#: — and the runtime cannot tell that from a stack's own key, which is exactly
#: why one namespace works.
VERSIONS_CONFIG = {
    'versions:talos': 'v1.11.0',
    **{
        f'versions:rootfs-{conventions.gateway.rootfs_pin(service)}': f'{ROOTFS_RELEASE}:{DIGEST}'
        for service in conventions.gateway.SERVICES
    },
}

#: What the two accounts' providers are built from. Every value is invented;
#: what the suite is for is that each is read at the line that builds its
#: provider and that everything below that line inherits the result.
ACCOUNT_CONFIG = {
    'kluster:ociTenancyOcid': TENANCY_ID,
    'kluster:ociUserOcid': 'ocid1.user.oc1..test',
    'kluster:ociFingerprint': ':'.join(['ab'] * 16),
    'kluster:ociPrivateKey': '-----BEGIN PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----',
    'kluster:b2ApplicationKeyId': 'a-b2-key-id',
    'kluster:b2ApplicationKey': 'a-b2-key',
}


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        #: Every resource type the run registered, so a test can ask which
        #: providers the program actually reached.
        self.registered: set[str] = set()
        #: What each resource was declared with, by name, so a test can ask
        #: what the stack handed a provider rather than only that it made one.
        self.inputs: dict[str, dict[str, Any]] = {}
        #: The type each resource was registered as, and the provider instance
        #: it was registered against, both by name. The engine hands a mock the
        #: reference of the provider that would manage the resource, which is
        #: how a case can ask what a declaration authenticates as rather than
        #: only that it happened.
        self.types: dict[str, str] = {}
        self.providers: dict[str, str] = {}
        #: The same for each function call, by token.
        self.call_providers: dict[str, str] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        self.registered.add(args.typ)
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        self.inputs[args.name] = dict(cast('dict[str, Any]', args.inputs))
        self.types[args.name] = args.typ
        self.providers[args.name] = args.provider or ''
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
        self.call_providers[args.token] = args.provider or ''
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


#: The whole of what the stack reads out of configuration: the version pins,
#: the secrets that configure the two accounts' providers, and the handful of
#: values an operator supplies or a booted machine reports. Nothing here names
#: a provider namespace — with every provider explicit there is nothing left to
#: configure through one (rfc-002 §8.1).
STACK_CONFIG = {
    'kluster:budgetAlertRecipients': json.dumps(BUDGET_RECIPIENTS),
    # The §3 domain: the credential the host is reached with, and nothing else.
    # There is no endpoint among them — it is derived — and no storage
    # directory either: the host's own configuration management has to name the
    # same one, which makes it a convention.
    'kluster:libvirtPrivateKey': LIBVIRT_KEY,
    **VERSIONS_CONFIG,
    **ACCOUNT_CONFIG,
    **GATEWAY_CONFIG,
}


@pytest_asyncio.fixture(autouse=True)
async def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Mocks:
    with_compartment(monkeypatch, COMPARTMENT)
    # The run materializes the libvirt session's credential into the checkout's
    # `.credentials/`, so every test here is pointed at a directory of its own:
    # a test suite that wrote into the tree it runs from would leave a key
    # behind and, worse, overwrite the operator's.
    monkeypatch.setattr(workstation, 'repo_root', lambda: tmp_path)
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
    the shell the desired state travels over, the controller's API endpoint,
    and the next hop of every managed route. A value typed in beside the API key would be a second
    copy of that, free to disagree with the roster that decides it.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    assert setup.inputs[f'{conventions.CLUSTER_NAME}-firewall-unifi']['apiUrl'] == f'https://{conventions.overlay.UDM}'
    assert setup.inputs[f'{conventions.CLUSTER_NAME}-services-routing']['host'] == str(conventions.overlay.UDM)
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

    name = f'{conventions.CLUSTER_NAME}-firewall'
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
    """The libvirt endpoint is derived from the roster and the checkout.

    Nothing about this URI can be recorded in committed configuration. The
    address belongs to the overlay roster the same run authorizes the host on,
    and the two paths in it exist only on the machine running the program — a
    workstation on one run and a continuous-integration runner on the next — so
    a URI typed into the stack would be wrong for one of them and stale for
    both.
    """
    address = str(conventions.overlay.member(conventions.overlay.MEMBER_HOMELAB).address)

    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    uri = cast('str', setup.inputs[f'{conventions.CLUSTER_NAME}-libvirt']['uri'])
    parts = urlsplit(uri)
    assert parts.scheme == 'qemu+ssh'
    assert parts.netloc == f'{homelab.LIBVIRT_USER}@{address}'
    assert parts.path == '/system'

    # The files the provider opens, named relative to the checkout root — an
    # absolute path here would record the path this machine happened to have
    # in a resource input, and every other machine would then diff against it
    # forever (rfc-002 §8.4).
    slot = f'{workstation.DIRECTORY}/{homelab.SLOT}'
    query = parse_qs(parts.query)
    assert query['keyfile'] == [f'{slot}/{homelab.KEYFILE}']
    assert query['knownhosts'] == [f'{slot}/{homelab.KNOWN_HOSTS}']
    assert (tmp_path / slot / homelab.KEYFILE).read_text() == LIBVIRT_KEY
    # The pin is written against the address the URI dials: a `known_hosts`
    # entry keyed on anything else matches nothing the session sees.
    assert (tmp_path / slot / homelab.KNOWN_HOSTS).read_text() == f'{address} {conventions.HOMELAB_HOST_KEY}\n'
    # And no key holds any of it: what is configured is the credential alone.
    assert not [key for key in STACK_CONFIG if 'libvirtUri' in key]


@pytest.mark.asyncio
async def test_the_overlay_carries_rules_composed_from_the_roster_and_the_resolvers(setup: Mocks) -> None:
    """The policy is composed here, out of the facts the program already holds.

    `Overlay` declares none of it (rfc-002 §6), so this is where the four
    destinations a run may reach are decided — and each of them is read from
    the table that also declares the thing it names. A second statement of any
    of those addresses would be free to disagree with the one the packets are
    matched against: the homelab host is at the address the roster authorizes
    it on, and the resolvers at the site addresses the service census gives
    them, which is what their packets carry after the gateway routes them.
    """
    homelab_address = conventions.overlay.member(conventions.overlay.MEMBER_HOMELAB).address
    ci = conventions.overlay.Role.CI

    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    rendered = cast('str', setup.inputs[f'{conventions.CLUSTER_NAME}-network']['flowRules'])
    assert f'accept tseq role {ci} and ipdest {homelab_address}/32 and dport {flow_rules.SSH_PORT};' in rendered
    assert f'accept tseq role {ci} and ipdest {conventions.overlay.UDM}/32 and dport {flow_rules.SSH_PORT};' in rendered
    for resolver in conventions.gateway.RESOLVERS:
        port = conventions.gateway.ADGUARD_API_PORT
        assert f'accept tseq role {ci} and ipdest {resolver.address}/32 and dport {port};' in rendered


@pytest.mark.asyncio
async def test_the_bootstrap_knob_moves_both_doors_to_the_gateway_at_once(setup: Mocks) -> None:
    """First bring-up dials the device over the LAN, on both channels.

    The overlay address answers only once the overlay daemon's container is on
    the device, and that container is what this run delivers. Where the device
    answers is the whole of what the knob decides, and it decides it for both
    providers that reach the gateway — the desired-state push over SSH and the
    controller's API — because they are the same box behind two ports, and an
    override that moved one of them would leave the run half able to reach it.
    """
    pulumi.runtime.set_all_config(dict(STACK_CONFIG) | {f'kluster:{physical.GATEWAY_BOOTSTRAP_HOST}': BOOTSTRAP_HOST})

    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    assert setup.inputs[f'{conventions.CLUSTER_NAME}-services-routing']['host'] == BOOTSTRAP_HOST
    assert setup.inputs[f'{conventions.CLUSTER_NAME}-firewall-unifi']['apiUrl'] == f'https://{BOOTSTRAP_HOST}'
    # Nothing else about the channels moves — the pin in particular is a bare
    # key with no host name in front of it, so it matches the device at either
    # address (`test_device_files`).
    assert setup.inputs[f'{conventions.CLUSTER_NAME}-services-routing']['port'] == 22


@pytest.mark.asyncio
async def test_the_pin_a_preview_shows_is_the_constant_the_repository_holds(setup: Mocks) -> None:
    """A pin nobody can read is a pin nobody reviews (rfc-002 §11).

    The key the device must present is a public key and a decision of this
    repository, so it is a constant rather than a configuration secret — which
    is what puts it in the preview a reviewer reads, in the clear, instead of
    behind the redaction a secret-typed value carries wherever it goes.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    declared = setup.inputs[f'{conventions.CLUSTER_NAME}-services-routing']['host_key']
    assert declared == conventions.gateway.HOST_KEY
    assert not isinstance(declared, dict), 'the pin reached the engine marked secret'


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

    assert f'{conventions.CLUSTER_NAME}-firewall-peer-v6' not in setup.inputs
    # Nothing else waits with it. The v4 half names the node address the
    # address plan states rather than one a booted machine reports, and the
    # rest of the census never named the worker at all.
    assert f'{conventions.CLUSTER_NAME}-firewall-peer-v4' in setup.inputs
    assert f'{conventions.CLUSTER_NAME}-firewall-cluster-egress' in setup.inputs
    assert f'{conventions.CLUSTER_NAME}-firewall-network' in setup.inputs


@pytest.mark.asyncio
async def test_the_pinhole_admits_the_configured_address_once_it_is_known(setup: Mocks) -> None:
    """And with the address configured, the rule is back and carries it.

    Step three of the bring-up ceremony is writing the key, so what follows it
    has to be the pinhole itself — the configured address, on the port the
    census holds, into the zone the worker moved to. The port is not configured beside
    it: two firewall rules name it and they have to agree, so it sits with the
    public port census in `conventions` (rfc-002 §11).
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    destination = setup.inputs[f'{conventions.CLUSTER_NAME}-firewall-peer-v6']['destination']
    assert destination['ips'] == [WORKER_GUA]
    assert int(destination['port']) == conventions.QBITTORRENT_PEER_PORT


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
    assert set(physical.CI_IDENTITY_OUTPUTS) == set(conventions.overlay.CI_MEMBERS)
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


#: The instance id the mock answers a node's declaration with, which is how an
#: attachment can be asserted to have landed on the node the volume table names.
INSTANCE_IDS = {node: f'{conventions.CLUSTER_NAME}-{node}_id' for node in conventions.CLOUD_NODES}


@pytest.mark.asyncio
async def test_every_volume_is_attached_to_the_node_the_table_names(setup: Mocks) -> None:
    """A block volume attaches only within its own availability domain.

    Both halves come off the instance rather than out of a constant, because
    the domain a node lands in is itself decided at apply time from what the
    region offers - a volume pinned to a remembered domain would fail to
    attach the first time the placement list came back in another order. Which
    instance each one lands on is the table's answer, and for the following
    volume that answer is the node holding the dedicated VIP.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    for name, volume in conventions.NODE_VOLUMES.items():
        declared = setup.inputs[f'{conventions.CLUSTER_NAME}-{name}-volume']
        attachment = setup.inputs[f'{conventions.CLUSTER_NAME}-{name}-attachment']
        assert declared['availabilityDomain'] == AVAILABILITY_DOMAIN
        assert int(declared['sizeInGbs']) == volume.size_gb
        assert attachment['instanceId'] == INSTANCE_IDS[volume.attached_node]

    following = setup.inputs[f'{conventions.CLUSTER_NAME}-hath-cache-attachment']
    assert following['instanceId'] == INSTANCE_IDS[conventions.DEDICATED_VIP_NODE]


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
async def test_the_quota_names_the_compartment_this_program_decided(setup: Mocks) -> None:
    """A quota statement has no OCID form and names its compartment by name.

    Which is why that name is a convention rather than something read back from
    the tenancy, and why the stack hands the component both halves: the budget
    beside it targets the same compartment by OCID. The statements' own
    content - deny before allow, every family capped - is `test_guardrails`.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    statements = cast('list[str]', setup.inputs[f'{conventions.CLUSTER_NAME}-quota']['statements'])
    assert statements
    assert all(text.endswith(f'in compartment {COMPARTMENT.name}') for text in statements)


@pytest.mark.asyncio
async def test_the_budget_alerts_reach_the_addresses_configuration_names(setup: Mocks) -> None:
    """The only signal this stack raises that does not go through the cluster.

    The addresses are the one thing about the guardrails an operator supplies,
    so what this holds is the path from the configuration key to the rule.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    alerts = [inputs for name, inputs in setup.inputs.items() if name.startswith(f'{conventions.CLUSTER_NAME}-budget-')]
    assert alerts
    for alert in alerts:
        assert alert['recipients'] == ','.join(BUDGET_RECIPIENTS)


@pytest.mark.asyncio
async def test_a_recipient_list_that_is_not_a_list_of_addresses_is_refused() -> None:
    """Named at the boundary, so the operator is told which key to fix."""
    pulumi.runtime.set_all_config(
        dict(STACK_CONFIG) | {'kluster:budgetAlertRecipients': json.dumps('one@example.invalid')}
    )

    with pytest.raises(TypeError, match='budgetAlertRecipients must be a list'):
        await physical.main()


def test_the_signing_configuration_is_read_from_the_keys_the_mint_writes() -> None:
    """Four values, written by one command and read at one line.

    None of them is configuration of this program's own: they are the signing
    configuration the credential mint installs, and the stack program reads
    them where it builds the cloud provider. Asserting against the minter's
    constants is what keeps the reader from drifting onto keys nothing fills.
    """
    from kluster.scripts.credentials import derived

    assert physical.OCI_TENANCY_OCID == derived.OCI_TENANCY_KEY
    assert physical.OCI_USER_OCID == derived.OCI_USER_KEY
    assert physical.OCI_FINGERPRINT == derived.OCI_FINGERPRINT_KEY
    assert physical.OCI_PRIVATE_KEY == derived.OCI_PRIVATE_KEY_KEY

    from kluster.components.backup import APPLICATION_KEY, APPLICATION_KEY_ID

    assert APPLICATION_KEY_ID == derived.B2_KEY_ID_KEY
    assert APPLICATION_KEY == derived.B2_KEY_KEY


def test_no_provider_namespace_is_read_at_all() -> None:
    """Every key this stack reads belongs to this repository (rfc-002 §8.1, §10.3).

    A provider namespace is configuration acting at a distance: the same
    program run somewhere else declares against a different account, and
    nothing in the program says so. With every provider built explicitly there
    is nothing left for one to carry, so the committed file holds none.

    Two namespaces, not one: `versions:` is this repository's own, holding the
    pins every stack shares (§11.1).
    """
    namespaces = {key.partition(':')[0] for key in STACK_CONFIG}
    assert namespaces == {'kluster', 'versions'}


#: Every site fact the stack takes as configuration, and every secret its two
#: providers are built from. A first `up` is run against a half-filled
#: configuration more often than not, so what matters is that each missing
#: value stops the run by naming itself rather than failing later inside a
#: provider call.
SITE_FACTS = [
    'kluster:budgetAlertRecipients',
    'kluster:libvirtPrivateKey',
    *ACCOUNT_CONFIG,
    *VERSIONS_CONFIG,
]


@pytest.mark.parametrize('key', SITE_FACTS)
@pytest.mark.asyncio
async def test_a_site_fact_the_configuration_lacks_refuses_by_name(key: str) -> None:
    pulumi.runtime.set_all_config({name: value for name, value in STACK_CONFIG.items() if name != key})

    # A version pin is refused by its accessor rather than by Pulumi, because
    # the accessor is what knows the kind and can name the whole key.
    expected = KeyError if key.startswith('versions:') else pulumi.ConfigMissingError
    with pytest.raises(expected, match=key):
        await physical.main()


@pytest.mark.asyncio
async def test_the_program_never_reads_the_devices_own_credential() -> None:
    """`gatewayPrivateKey` configures a provider, so the provider reads it.

    For a dynamic provider that line is `configure`, in the plugin's process
    (rfc-002 §7.4) — so the key is absent from every read this program performs,
    and a run whose configuration lacks it gets as far as declaring the gateway.
    What the device-files provider does with a configuration that lacks it is
    the provider's own test.
    """
    pulumi.runtime.set_all_config(
        {name: value for name, value in STACK_CONFIG.items() if name != 'kluster:gatewayPrivateKey'}
    )

    physical._gateway(pulumi.Config())  # pyright: ignore[reportPrivateUsage]
    await wait_for_rpcs(await_all_outstanding_tasks=False)


@pytest.mark.asyncio
async def test_the_gateway_arm_reads_the_configuration_its_channels_need() -> None:
    """The gateway, exercised without the rest of the stack.

    `main` reaches it now, but a failure there names the whole program; this
    isolates the arm whose wiring is entirely configuration — every key, and
    which of them is a secret — so a missing one is reported against the
    gateway rather than against a run of everything.
    """
    physical._gateway(pulumi.Config())  # pyright: ignore[reportPrivateUsage]
    await wait_for_rpcs(await_all_outstanding_tasks=False)


def test_a_root_filesystem_pin_becomes_the_url_and_digest_a_push_carries() -> None:
    """The pin is a release and a digest; the rest of the URL is a convention.

    So an operator maintains four scalars and not four URLs, and moving
    publication is an edit to one rule (rfc-002 §11.1). The two resolvers have
    a key each even though one build serves both, which is what lets a new
    resolver be proven on one instance before the other.
    """
    caddy = conventions.gateway.CADDY
    pin = physical._rootfs(caddy)  # pyright: ignore[reportPrivateUsage]

    assert pin.sha256 == DIGEST
    assert pin.url == f'{conventions.gateway.ROOTFS_RELEASES}/{ROOTFS_RELEASE}/caddy-arm64.tar.zst'

    alice, bob = conventions.gateway.RESOLVERS
    assert conventions.gateway.rootfs_pin(alice) != conventions.gateway.rootfs_pin(bob)
    assert physical._rootfs(alice).url == physical._rootfs(bob).url  # pyright: ignore[reportPrivateUsage]


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


def _unwrapped(value: Any) -> Any:
    """One provider input, as the engine received it.

    A secret input reaches the monitor as Pulumi's own marked mapping rather
    than as the value, so a case that wants the value has to look inside it --
    and the marking is itself the property worth seeing.
    """
    assert isinstance(value, dict), f'{value!r} is not a marked secret'
    return cast('dict[str, Any]', value)['value']


#: Which provider each of the stack's own resource families must be signed by,
#: by the prefix of the type token the family carries. The device-file
#: resources are the one family with no entry: their provider carries nothing
#: and travels as an object rather than through resource options (rfc-002
#: §8.3), and the Talos chain is the other -- it authenticates to no account,
#: so it keeps the package's own default provider.
SIGNED_BY = {
    'oci:': f'{conventions.CLUSTER_NAME}-oci',
    'b2:': f'{conventions.CLUSTER_NAME}-b2',
    'unifi:': f'{conventions.CLUSTER_NAME}-firewall-unifi',
    'zerotier:': f'{conventions.CLUSTER_NAME}-zerotier',
    'libvirt:': f'{conventions.CLUSTER_NAME}-libvirt',
}


@pytest.mark.asyncio
async def test_every_resource_is_signed_by_the_provider_its_owner_built(setup: Mocks) -> None:
    """The whole point of the slice, as one assertion over the whole program.

    Every resource in the stack authenticates through a provider some component
    built explicitly, and no resource names one: each inherits it from its
    parent, transitively, because a provider set on a component is the default
    for its subtree. A resource that lost its parent would inherit the stack's
    providers instead -- which, with default providers disabled, is nothing at
    all.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    checked = 0
    for name, typ in setup.types.items():
        # A provider resource's own type token is `pulumi:providers:<package>`,
        # so it never matches a package prefix and never checks itself.
        for prefix, provider in SIGNED_BY.items():
            if not typ.startswith(prefix):
                continue
            assert provider in setup.providers[name], f'{name} ({typ}) is not signed by {provider}'
            checked += 1
    # A run that declared nothing would pass the loop above vacuously.
    assert checked >= len(SIGNED_BY)


@pytest.mark.asyncio
async def test_the_cloud_provider_is_the_stack_programs_and_is_shared(setup: Mocks) -> None:
    """One account, six components, one provider -- built where they meet.

    A provider built inside any one of them would be reached into by the other
    five, which is the test rfc-002 §8.1 gives for what the stack program owns.
    Its region is not configuration: it is a permanent property of the account
    and lives in `conventions`, so the line that builds it reads exactly the
    four secrets -- which is also the whole of what the committed file has to
    carry for this account.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    built = setup.inputs[f'{conventions.CLUSTER_NAME}-oci']
    assert built['region'] == conventions.OCI_TENANCY.region
    # All four arrive wrapped: the engine sees a marked value, which is what
    # keeps a signing key -- and the three identifiers that say whose it is --
    # out of a preview and out of a log. `_unwrapped` asserts the marking, so
    # a value that lost it fails here rather than reaching a diff in the clear.
    assert _unwrapped(built['tenancyOcid']) == TENANCY_ID
    assert _unwrapped(built['userOcid']) == ACCOUNT_CONFIG['kluster:ociUserOcid']
    assert _unwrapped(built['fingerprint']) == ACCOUNT_CONFIG['kluster:ociFingerprint']
    assert _unwrapped(built['privateKey']) == ACCOUNT_CONFIG['kluster:ociPrivateKey']

    signed = {name for name, typ in setup.types.items() if typ.startswith('oci:')}
    assert len({setup.providers[name] for name in signed}) == 1, 'the cloud account has more than one provider'


@pytest.mark.asyncio
async def test_the_placement_lookups_name_the_provider_they_sign_with(setup: Mocks) -> None:
    """A stack program's own invoke has no parent to inherit from.

    Both regional lookups are made outside any component, so nothing carries a
    provider to them: they name it. With default providers disabled an invoke
    that forgot would fail rather than sign as nobody.
    """
    await physical.main()
    await wait_for_rpcs(await_all_outstanding_tasks=False)

    for token in (
        'oci:Identity/getAvailabilityDomains:getAvailabilityDomains',
        'oci:Identity/getFaultDomains:getFaultDomains',
    ):
        assert f'{conventions.CLUSTER_NAME}-oci' in setup.call_providers[token], f'{token} signed as nobody'
