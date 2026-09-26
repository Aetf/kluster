"""The physical program as a whole.

A smoke test with teeth: it declares the entire stack against mocks, which is
what catches wiring mistakes — a resource argument the provider would reject,
or a dependency ordered so that the endpoint is needed before it exists.

The stack is also an inventory, and as of the homelab worker it is a complete
one: every area the design calls for is written, so `main` runs end to end
here rather than stopping at a named gap. What replaces that gap as a test is
the same worry stated positively — each provider of the design has to appear in
what the run registered, because an area that quietly declared nothing would
leave a stack that comes up looking whole.

Every run here is under the parent backstop `kluster.main` installs before a
real run declares anything, so a resource a component leaves unparented fails
the run here rather than in `pulumi preview`.
"""

import inspect
import json
from collections import Counter
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pulumi
import pytest
import pytest_asyncio
from device_places import DEVICE_TYPE_PREFIX, PLACES
from mock_monitor import Recorder, declaring, run_under_backstop
from oci_conventions import with_compartment, with_tenancy_ocid
from unifi_controller import Controller, zone_id

from kluster import conventions
from kluster.components import homelab
from kluster.components.backup import BackupBucket
from kluster.components.cloud.guardrails import Guardrails
from kluster.components.gateway import Gateway, access, nspawn, persistence
from kluster.components.gateway.container import CaddyService
from kluster.components.gateway.unifi import SiteFirewall
from kluster.components.overlay import Overlay, flow_rules
from kluster.components.talos import TalosCluster
from kluster.components.talos.image import TalosArtifact
from kluster.lib import workstation
from kluster.stacks import physical

LB_ADDRESS = '203.0.113.10'
LB_ADDRESS_V6 = '2001:db8::10'
VIP1_ADDRESS = '203.0.113.20'
VNIC_ID = 'ocid1.vnic.oc1.phx.vip'
AVAILABILITY_DOMAIN = 'ZRbp:PHX-AD-1'
OBJECT_NAMESPACE = 'axmpletenancy'
TENANCY_ID = 'ocid1.tenancy.oc1..test'
BUDGET_RECIPIENTS = ['alerts@example.invalid', 'second@example.invalid']
ZT_NETWORK_ID = '0123456789abcdef'
#: A LAN address for the gateway, as the first-bring-up knob carries one. It is
#: a literal address because the knob takes nothing else (`gateway.md` §2.5),
#: and it is from the documentation range like every other invented address
#: here; what the cases below need of it is that it is not the overlay address.
BOOTSTRAP_HOST = '192.0.2.1'
#: The shape the knob forbids, spelled out for the case that proves the refusal.
BOOTSTRAP_NAME = 'gateway.invalid'
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

#: A manifest digest, as a root filesystem pin carries one. Nothing here checks
#: the bytes behind it; the shape is what the reader is checked against.
DIGEST = f'sha256:{"f" * 64}'
#: The tag the pins below name. Invented, like the digest: what matters is that
#: the convention turns the pair into the reference a push pulls by.
ROOTFS_TAG = '7'

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

#: The version pins a stack program reads, in the namespace they share
#: (framework/pulumi.md §3.2). They are project-level configuration in the
#: committed tree — one copy for five stacks — and the runtime cannot tell that
#: from a stack's own key, which is exactly why one namespace works.
VERSIONS_CONFIG = {
    'versions:talos': 'v1.11.0',
    **{
        f'versions:image-{conventions.gateway.image_pin(service)}': (
            f'{conventions.gateway.image_repository(service.artifact)}:{ROOTFS_TAG}@{DIGEST}'
        )
        for service in conventions.gateway.SERVICES
    },
}

#: What the two accounts' providers are built from. Every value is invented;
#: what the suite is for is that each is read at the line that builds its
#: provider and that everything below that line inherits the result.
ACCOUNT_CONFIG = {
    'kluster:ociUserOcid': 'ocid1.user.oc1..test',
    'kluster:ociFingerprint': ':'.join(['ab'] * 16),
    'kluster:ociPrivateKey': '-----BEGIN PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----',
    'kluster:b2ApplicationKeyId': 'a-b2-key-id',
    'kluster:b2ApplicationKey': 'a-b2-key',
}


class Installation(Controller):
    """Every account and appliance the program reaches, as far as it reads them back.

    The values are invented; what the suite is for is that each is read at the
    right line and reaches the right resource. The controller's zone lookup is
    the shared stand-in's (`unifi_controller`), on the site the gateway is
    declared against.
    """

    def __init__(self) -> None:
        super().__init__(site=conventions.gateway.UNIFI_SITE)
        #: The arguments each machine configuration was rendered from. An
        #: invoke's arguments survive nowhere else, and two of them are read
        #: back: the cluster endpoint, the one place the Kubernetes API port is
        #: written as a URL, and the patches carrying the firewall's openings.
        self.configurations: list[dict[str, Any]] = []

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        match args.typ:
            case 'oci:Core/vcn:Vcn':
                return {'ipv6cidrBlocks': ['2603:c020:8000:1200::/56']}
            case 'oci:NetworkLoadBalancer/networkLoadBalancer:NetworkLoadBalancer':
                return {
                    'ipAddresses': [
                        {'ipAddress': LB_ADDRESS, 'isPublic': True, 'ipVersion': 'IPV4'},
                        {'ipAddress': LB_ADDRESS_V6, 'isPublic': True, 'ipVersion': 'IPV6'},
                    ]
                }
            case 'talos:machine/secrets:Secrets':
                return {'machineSecrets': {'cluster': {'id': 'test'}}}
            case 'talos:cluster/kubeconfig:Kubeconfig':
                return {'kubeconfigRaw': KUBECONFIG}
            case 'zerotier:index/identity:Identity':
                # Keyed by the resource name, so a test can tell one member's key
                # material from another's: which identity an export carries is the
                # whole of the contract the credential map reads it under.
                return {'identityId': f'{args.name}-node', 'publicKey': 'public', 'privateKey': f'{args.name}-secret'}
            case 'zerotier:index/network:Network':
                return {'networkId': ZT_NETWORK_ID}
            case 'oci:Core/instance:Instance':
                return {'availabilityDomain': AVAILABILITY_DOMAIN}
            case 'oci:Core/publicIp:PublicIp':
                return {'ipAddress': VIP1_ADDRESS}
            case 'b2:index/bucket:Bucket':
                return {'bucketId': 'b2-bucket-id'}
            case 'b2:index/applicationKey:ApplicationKey':
                return {'applicationKeyId': args.name + '-key-id', 'applicationKey': args.name + '-secret'}
            case _:
                return {}

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        match args.token:
            case 'oci:Core/getServices:getServices':
                return {'services': [{'id': 'ocid1.service.os', 'name': 'Object Storage', 'cidrBlock': 'oci-os'}]}
            case 'oci:Core/getVnicAttachments:getVnicAttachments':
                return {'vnicAttachments': [{'vnicId': VNIC_ID}]}
            case 'oci:Core/getVnic:getVnic':
                return {'ipv6addresses': ['2603:c020:8000:1200::a']}
            case 'oci:Identity/getAvailabilityDomains:getAvailabilityDomains':
                return {'availabilityDomains': [{'name': 'ZRbp:PHX-AD-1'}]}
            case 'oci:Identity/getFaultDomains:getFaultDomains':
                return {'faultDomains': [{'name': f'FAULT-DOMAIN-{n}'} for n in (1, 2, 3)]}
            case 'talos:machine/getConfiguration:getConfiguration':
                self.configurations.append(dict(cast('dict[str, Any]', args.args)))
                return {'machineConfiguration': 'machine: {}'}
            case 'talos:client/getConfiguration:getConfiguration':
                return {'talosConfig': TALOSCONFIG}
            case 'talos:cluster/getHealth:getHealth':
                return {'id': 'healthy'}
            case 'oci:ObjectStorage/getNamespace:getNamespace':
                return {'namespace': OBJECT_NAMESPACE}
            case 'talos:imageFactory/getUrls:getUrls':
                # Two artifacts of the same family: the cloud nodes' OCI image
                # and the worker's `nocloud` disk image, which the factory
                # serves compressed.
                platform = str(cast('dict[str, Any]', args.args)['platform'])
                suffix = 'raw.xz' if platform == 'nocloud' else 'qcow2'
                url = f'https://factory.talos.dev/image/test/v1.11.0/{platform}-arch.{suffix}'
                return {'urls': {'diskImage': url}}
            case _:
                return super().answer(args)


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
    # The §3 area, the homelab: the credential the host is reached with, and
    # nothing else.
    # There is no endpoint among them — it is derived — and no storage
    # directory either: the host's own configuration management has to name the
    # same one, which makes it a convention.
    'kluster:libvirtPrivateKey': LIBVIRT_KEY,
    **VERSIONS_CONFIG,
    **ACCOUNT_CONFIG,
    **GATEWAY_CONFIG,
}


@pytest_asyncio.fixture(autouse=True)
async def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Installation:
    with_compartment(monkeypatch, COMPARTMENT)
    with_tenancy_ocid(monkeypatch, TENANCY_ID)
    # The run materializes the libvirt session's credential into the checkout's
    # `.credentials/`, so every test here is pointed at a directory of its own:
    # a test suite that wrote into the tree it runs from would leave a key
    # behind and, worse, overwrite the operator's.
    monkeypatch.setattr(workstation, 'repo_root', lambda: tmp_path)
    pulumi.runtime.set_all_config(dict(STACK_CONFIG))
    return await run_under_backstop(Installation(), stack='physical')


#: The provider of each area the design has, by the prefix its type tokens
#: carry: the cloud, the Talos chain, the homelab host, the backup account, the
#: gateway's controller and the overlay.
AREA_PROVIDERS = ('oci', 'talos', 'libvirt', 'b2', 'unifi', 'zerotier')


@pytest.mark.asyncio
async def test_the_stack_declares_every_area_of_the_design(setup: Installation) -> None:
    # The whole program against the mocks, which is where a wiring mistake
    # surfaces — an argument the provider would reject, or a dependency that
    # needs the endpoint before it exists.
    async with declaring():
        await physical.main()

    # And the inventory property: an area that declared nothing at all would
    # leave a stack that runs clean and comes up one provider short.
    families = {typ.partition(':')[0] for typ in setup.types}
    assert set(AREA_PROVIDERS) <= families


#: The census parameters the rule below holds, as the component that receives
#: one and the parameter it arrives on. Stated rather than discovered, because
#: what makes a table a census is what it says and not how it is typed, and
#: added to by hand when a component gains one. Each entry names a parameter
#: and never its value: the roll itself is `conventions`' or the component's
#: to state, and a copy here would be a mirror (style/testing.md).
CENSUS_PARAMETERS = (
    (SiteFirewall, 'static_hosts'),
    (Gateway, 'static_hosts'),
    (Gateway, 'resolvers'),
    (Gateway, 'keys'),
    (CaddyService, 'vhosts'),
    (CaddyService, 'legacy'),
    (BackupBucket, 'scopes'),
    (Guardrails, 'alert_rules'),
    (Overlay, 'roster'),
    (Overlay, 'managed_routes'),
    (Overlay, 'dns'),
    (TalosCluster, 'control_plane_nodes'),
    (TalosCluster, 'worker_nodes'),
    (TalosCluster, 'bgp_peers'),
    # Handed down by the two artifact subclasses rather than by this program:
    # each states the schematic it is, and the base that renders it takes the
    # roll with no default to fall back on.
    (TalosArtifact, 'extensions'),
)


def test_no_census_parameter_carries_a_default() -> None:
    """A census lives beside the component that receives it, never inside it.

    A default is the loophole that lets it live in both places at once: the
    signature reads as though the caller decides, while a caller that passes
    nothing gets the roll the component chose. So the parameter is required,
    and a component with nothing to declare is handed an empty roll explicitly.
    """
    for component, parameter in CENSUS_PARAMETERS:
        default = inspect.signature(component.__init__).parameters[parameter].default
        assert default is inspect.Parameter.empty, (
            f'{component.__name__}.{parameter} defaults to {default!r}: a census parameter has no default '
            f'(style/pulumi.md), because a default is a roll the component owns behind a signature that says '
            f'it does not'
        )


@pytest.mark.asyncio
async def test_the_controller_is_dialed_where_the_roster_placed_the_gateway(setup: Installation) -> None:
    """The controller's address is derived, not recorded beside its key.

    The gateway's overlay address is handed out by this program's own ZeroTier
    roster, so every client of the gateway reads it from the same constant:
    the shell the desired state travels over, the controller's API endpoint,
    and the next hop of every managed route. A value typed in beside the API key would be a second
    copy of that, free to disagree with the roster that decides it.
    """
    async with declaring():
        await physical.main()

    assert (
        setup.inputs_of(f'{conventions.CLUSTER_NAME}-firewall-unifi')['apiUrl'] == f'https://{conventions.overlay.UDM}'
    )
    assert setup.inputs_of(f'{conventions.CLUSTER_NAME}-routing-config')['host'] == str(conventions.overlay.UDM)
    # And nothing supplies it: the stack has no key to read it from, so a
    # `record` command that pushed one would be filling a slot nobody reads.
    assert not [key for key in STACK_CONFIG if 'ApiUrl' in key]
    # The steady state is the knob's absence, so nothing has to be unset to
    # reach this.
    assert f'kluster:{physical.GATEWAY_BOOTSTRAP_HOST}' not in STACK_CONFIG


@pytest.mark.asyncio
async def test_the_cluster_zone_is_opened_to_the_home_with_the_iot_vlan_carved_out(setup: Installation) -> None:
    """The zone matrix as the whole run declares it, not as one component does.

    A zone the controller has just been told about is denied against every
    other zone in both directions, so each direction the design wants open is
    a policy the stack has to declare — and the one direction it does not want
    open, the IoT VLAN into the node subnet, is a drop that has to be ordered
    *ahead* of the zone-wide allow beside it. Both properties are wiring: they
    hold only if the stack reaches this arm of the gateway at all, which is
    what running the program rather than the component proves.
    """
    async with declaring():
        await physical.main()

    name = f'{conventions.CLUSTER_NAME}-firewall'
    zone = f'{name}-zone_id'
    internal = zone_id('Internal')

    outward = setup.inputs_of(f'{name}-cluster-internal')
    assert outward['action'] == 'ALLOW'
    assert outward['source']['zoneId'] == zone
    assert outward['destination']['zoneId'] == internal

    inward = setup.inputs_of(f'{name}-internal-cluster')
    assert inward['action'] == 'ALLOW'
    assert inward['source']['zoneId'] == internal
    assert inward['destination']['zoneId'] == zone

    # Two drops, one per family, because the source is a literal subnet.
    for suffix, source in (('v4', str(conventions.IOT_VLAN.v4)), ('v6', str(conventions.IOT_VLAN.v6))):
        drop = setup.inputs_of(f'{name}-iot-cluster-{suffix}')
        assert drop['action'] == 'BLOCK'
        assert drop['source']['ips'] == [source]
        assert drop['destination']['zoneId'] == zone

    # The drops first: the allow behind them is the broad one here, so an
    # allow declared ahead of them would answer for the IoT VLAN as well.
    assert setup.inputs_of(f'{name}-internal-cluster-order')['beforePredefinedIds'] == [
        f'{name}-iot-cluster-v4_id',
        f'{name}-iot-cluster-v6_id',
        f'{name}-internal-cluster_id',
    ]
    assert setup.inputs_of(f'{name}-cluster-internal-order')['beforePredefinedIds'] == [f'{name}-cluster-internal_id']


@pytest.mark.asyncio
async def test_the_site_resolver_is_given_no_static_host(setup: Installation) -> None:
    """The device name plane is DHCP-derived, so the roll of literal names is empty.

    Empty and passed anyway: the component has no roll of its own to fall back
    to, so a run declares a controller DNS record only for an entry this
    program states. A name belongs in it when it must resolve on the LAN with
    no lease behind it and no service plane to carry it.
    """
    assert physical.GATEWAY_STATIC_HOSTS == {}

    async with declaring():
        await physical.main()

    assert [typ for typ in setup.types if 'dnsRecord' in typ] == []


@pytest.mark.asyncio
async def test_the_libvirt_session_is_dialed_where_the_roster_placed_the_host(
    setup: Installation,
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

    async with declaring():
        await physical.main()

    uri = cast('str', setup.inputs_of(f'{conventions.CLUSTER_NAME}-libvirt')['uri'])
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
async def test_the_overlay_carries_rules_composed_from_the_roster_and_the_resolvers(setup: Installation) -> None:
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

    async with declaring():
        await physical.main()

    rendered = cast('str', setup.inputs_of(f'{conventions.CLUSTER_NAME}-network')['flowRules'])
    assert f'accept tseq role {ci} and ipdest {homelab_address}/32 and dport {flow_rules.SSH_PORT};' in rendered
    assert f'accept tseq role {ci} and ipdest {conventions.overlay.UDM}/32 and dport {flow_rules.SSH_PORT};' in rendered
    for resolver in conventions.gateway.RESOLVERS:
        port = conventions.gateway.ADGUARD_API_PORT
        assert f'accept tseq role {ci} and ipdest {resolver.address}/32 and dport {port};' in rendered


@pytest.mark.asyncio
async def test_the_overlay_network_is_adopted_by_the_conventions_id_and_is_the_runs_only_adoption(
    setup: Installation,
) -> None:
    """One import in the whole program, and it is the network, by the id `conventions` states.

    The network is the one resource this stack adopts rather than creates: it
    predates the program, and every member is an upsert on its node id that
    needs no adoption. An `importId` on anything else would be a resource the
    first run reads from a provider instead of creating, which is a shape no
    other declaration here has and one the ceremony (physical/gateway.md
    §2.5) does not account for. The id is the convention's, not a literal
    here: the stack hands on what `conventions` says the network is.
    """
    async with declaring():
        await physical.main()

    adopted = [request for request in setup.requested if request.importId]

    assert [request.type for request in adopted] == ['zerotier:index/network:Network']
    (network,) = adopted
    assert network.name == f'{conventions.CLUSTER_NAME}-network'
    assert network.importId == conventions.overlay.NETWORK_ID


@pytest.mark.asyncio
async def test_the_overlay_pushes_the_block_domain_and_the_resolvers_it_admits_a_run_to(setup: Installation) -> None:
    """The managed DNS reaches the network from `conventions`, not composed here.

    The domain is the convention's -- the overlay block's name, which
    `test_conventions` holds it to -- and the servers are the resolver census
    the flow rules above admit a run to, at the same container-VLAN
    addresses, because the resolvers have no others. A value composed in the
    program would be a second spelling of either, free to disagree with the
    block or the census.
    """
    async with declaring():
        await physical.main()

    assert setup.inputs_of(f'{conventions.CLUSTER_NAME}-network')['dns'] == [
        {
            'domain': conventions.overlay.MANAGED_DNS.domain,
            'servers': [str(resolver.address) for resolver in conventions.gateway.RESOLVERS],
        }
    ]


@pytest.mark.asyncio
async def test_the_bootstrap_knob_moves_both_doors_to_the_gateway_at_once(setup: Installation) -> None:
    """First bring-up dials the device over the LAN, on both channels.

    The overlay address answers only once the overlay daemon's container is on
    the device, and that container is what this run delivers. Where the device
    answers is the whole of what the knob decides, and it decides it for both
    providers that reach the gateway — the desired-state push over SSH and the
    controller's API — because they are the same box behind two ports, and an
    override that moved one of them would leave the run half able to reach it.
    """
    pulumi.runtime.set_all_config(dict(STACK_CONFIG) | {f'kluster:{physical.GATEWAY_BOOTSTRAP_HOST}': BOOTSTRAP_HOST})

    async with declaring():
        await physical.main()

    assert setup.inputs_of(f'{conventions.CLUSTER_NAME}-routing-config')['host'] == BOOTSTRAP_HOST
    assert setup.inputs_of(f'{conventions.CLUSTER_NAME}-firewall-unifi')['apiUrl'] == f'https://{BOOTSTRAP_HOST}'
    # Nothing else about the channels moves — the pin in particular is a bare
    # key with no host name in front of it, so it matches the device at either
    # address (`test_device_files`).
    assert setup.inputs_of(f'{conventions.CLUSTER_NAME}-routing-config')['port'] == 22


@pytest.mark.asyncio
async def test_a_bootstrap_host_that_is_a_name_is_refused_before_the_window_opens() -> None:
    """The one apply this knob exists for has no resolver behind it.

    The cutover window stops both home resolvers for its whole length, so a
    name is unresolvable at the only moment the key is read. Nothing later
    would catch it kindly: the providers would dial the name inside the window,
    after the device's live state has already moved. So the run refuses at the
    read, and says why rather than only that the value is wrong.
    """
    pulumi.runtime.set_all_config(dict(STACK_CONFIG) | {f'kluster:{physical.GATEWAY_BOOTSTRAP_HOST}': BOOTSTRAP_NAME})

    with pytest.raises(ValueError, match='not a literal IP address'):
        await physical.main()


@pytest.mark.asyncio
async def test_the_pin_a_preview_shows_is_the_constant_the_repository_holds(setup: Installation) -> None:
    """A pin nobody can read is a pin nobody reviews (rfc-002 §11).

    The key the device must present is a public key and a decision of this
    repository, so it is a constant rather than a configuration secret — which
    is what puts it in the preview a reviewer reads, in the clear, instead of
    behind the redaction a secret-typed value carries wherever it goes.
    """
    async with declaring():
        await physical.main()

    declared = setup.inputs_of(f'{conventions.CLUSTER_NAME}-routing-config')['host_key']
    assert declared == conventions.gateway.HOST_KEY
    assert not isinstance(declared, dict), 'the pin reached the engine marked secret'


@pytest.mark.asyncio
async def test_the_device_is_told_to_keep_accepting_the_key_this_stack_dials_with(setup: Installation) -> None:
    """The door this program comes through is one it declares, or an update closes it.

    `/root` is off `/data`, so the key that authorizes every push is exactly as
    perishable as the rest of the customization. The stack passes the public
    half of its own credential, and nothing else: the operator's keys are on
    the device already and the converger takes none of them away.
    """
    async with declaring():
        await physical.main()

    name = f'{conventions.CLUSTER_NAME}-{conventions.PHYSICAL}'
    declared = setup.inputs_of(f'{conventions.CLUSTER_NAME}-access-key-{name}')

    assert declared['content'].strip() == conventions.gateway.CLIENT_KEY
    assert declared['path'] == access.key_path(name)


@pytest.mark.asyncio
async def test_the_device_is_given_the_packages_its_container_runtime_needs(setup: Installation) -> None:
    """The gateway's persistence layer is declared, and with the set as data.

    What a firmware update wipes is reinstalled by a script in the device's boot
    chain, and which packages that script installs is the union of what the
    layers above the mechanism require — passed in rather than written into the
    script, so a requirement is stated by the component that has it.
    """
    async with declaring():
        await physical.main()

    script = setup.inputs_of(f'{conventions.CLUSTER_NAME}-persistence-on-boot-{persistence.PACKAGES_SCRIPT}')

    assert script['path'] == f'{conventions.gateway.ON_BOOT_D}/{persistence.PACKAGES_SCRIPT}'
    assert script['host'] == str(conventions.overlay.UDM)
    for package in nspawn.NspawnRuntime.REQUIRED_PACKAGES:
        assert package in script['content'], package


@pytest.mark.asyncio
async def test_every_child_carries_its_components_name(setup: Installation) -> None:
    """The rule, held on the whole program rather than on the census happening not to collide.

    The gateway is where it bites: the persistence mechanism declares a file on
    behalf of whichever component asked, and the URN places that file under the
    asker, so it is the asker's name the file carries (style/pulumi.md). A
    file named for the mechanism instead registers cleanly today -- one
    `SiteRouting`, one `AuthorizedKeys`, one `NspawnRuntime` -- and collides the
    day a second instance of one of them asks for a file of the same kind.
    """
    async with declaring():
        await physical.main()

    assert setup.children_not_named_for_their_component() == {}


@pytest.mark.asyncio
async def test_no_two_resources_claim_one_place_on_the_device(setup: Installation) -> None:
    """One path on the device, one resource -- an invariant over the inputs, not over the names.

    Two components asking the mechanism for one `bin/` name, or one declaring a
    mounted file where another declares its converger, are two URNs at one
    path: the engine accepts the run, each `create` writes the file, and either
    `delete` takes it from under the other. Nothing in a name catches that
    (style/pulumi.md), so the program is read back by the place each device
    resource claims. The map is held complete first, since a device type it
    does not name is a type the census cannot see.
    """
    async with declaring():
        await physical.main()

    device_types = {typ for typ in setup.types if typ.startswith(DEVICE_TYPE_PREFIX)}
    assert device_types <= set(PLACES), device_types - set(PLACES)
    assert setup.places_claimed_more_than_once(PLACES) == {}


@pytest.mark.asyncio
async def test_the_pinhole_waits_for_an_address_the_worker_has_not_formed_yet(setup: Installation) -> None:
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

    async with declaring():
        await physical.main()

    declared = setup.names_declared
    # Nothing else waits with it. The v4 half names the node address the
    # address plan states rather than one a booted machine reports, and the
    # rest of the census never named the worker at all.
    assert f'{conventions.CLUSTER_NAME}-firewall-peer-v6' not in declared
    assert f'{conventions.CLUSTER_NAME}-firewall-peer-v4' in declared
    assert f'{conventions.CLUSTER_NAME}-firewall-cluster-egress' in declared
    assert f'{conventions.CLUSTER_NAME}-firewall-network' in declared


@pytest.mark.asyncio
async def test_the_pinhole_admits_the_configured_address_once_it_is_known(setup: Installation) -> None:
    """And with the address configured, the rule is back and carries it.

    Step three of the bring-up ceremony is writing the key, so what follows it
    has to be the pinhole itself — the configured address, on the port the
    census holds, into the zone the worker sits in. The port is not configured beside
    it: two firewall rules name it and they have to agree, so it sits with the
    public port census in `conventions` (rfc-002 §11).
    """
    async with declaring():
        await physical.main()

    destination = setup.inputs_of(f'{conventions.CLUSTER_NAME}-firewall-peer-v6')['destination']
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


class ExportedPhysical(Recorder):
    """A `physical` whose StackReference hands out exactly what the program exported.

    Each output's value is its own name, so a record built from one says which
    export it was read from, and a read of a name the program never exported
    lands as `None` rather than as an invented address.
    """

    def __init__(self, exported: set[str]) -> None:
        super().__init__()
        self.exported = exported

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'pulumi:pulumi:StackReference':
            return {'outputs': {name: name for name in self.exported}}
        return {}


@pytest.mark.asyncio
async def test_every_output_dns_reads_across_the_reference_is_one_this_program_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam between the two programs, held by running both.

    Both spell the names from `conventions.PHYSICAL_OUTPUTS`, so what is left
    to disagree is the programs themselves: an export this one dropped, or a
    field `dns` reads that nothing here writes under. `dns` writes what it
    reads into its anchors, and an output the reference does not carry arrives
    there as the string `None` — so the anchors are declared against a
    reference carrying this program's exports and nothing else, and each is
    checked to carry one of them.
    """
    from kluster.stacks import dns

    exported: dict[str, object] = {}

    def record(name: str, value: object) -> None:
        exported[name] = value

    monkeypatch.setattr(physical.pulumi, 'export', record)
    async with declaring():
        await physical.main()

    pulumi.runtime.set_all_config({f'kluster:{dns.CLOUDFLARE_API_TOKEN}': 'a-zones-token'})
    reader = await run_under_backstop(ExportedPhysical(set(exported)), stack='dns')
    async with declaring():
        await dns.main()

    anchors = [
        reader.inputs_of(f'{conventions.ZONE_PRIMARY}-{anchor}-{family}')
        for anchor, family in (
            (conventions.ANCHOR_CLUSTER, 'a'),
            (conventions.ANCHOR_CLUSTER, 'aaaa'),
            (conventions.ANCHOR_VIP1, 'a'),
        )
    ]
    for anchor in anchors:
        assert anchor['content'] in exported, anchor
    # And which export each family carries: the A records carry the IPv4
    # outputs and the AAAA the IPv6 one, or a dual-stack anchor is two records
    # of one family.
    exports = [cast('pulumi.Output[str]', exported[cast('str', anchor['content'])]).future() for anchor in anchors]
    assert [await export for export in exports] == [LB_ADDRESS, LB_ADDRESS_V6, VIP1_ADDRESS]


@pytest.mark.asyncio
async def test_the_program_exports_every_name_the_structure_carries_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PhysicalOutputs` is the contract and the export its implementation, both ways.

    A field nothing exports is what its reader receives as `None`; an export
    under a name the structure does not carry is one no reader can ask for.
    Either passes the type checker, so the run is what holds them together.
    """
    exported: dict[str, object] = {}

    def record(name: str, value: object) -> None:
        exported[name] = value

    monkeypatch.setattr(physical.pulumi, 'export', record)

    await physical.main()

    assert set(exported) == set(conventions.PHYSICAL_OUTPUTS.names())


@pytest.mark.asyncio
async def test_the_ci_join_credentials_are_exported_under_the_names_the_slot_map_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identities CI joins the overlay with, by the names `derived sync` reads.

    The slot map's half of that contract is a stack output name
    (`slots.StateRead`) and this program's half is the export. Both spell it
    from `conventions.PHYSICAL_OUTPUTS`, so what this holds is the map rather
    than the spelling: that it declares a read of every identity export, and
    that every read it declares of this stack names a field of the structure
    -- the case above holds fields to exports, so a row can then only name
    something the program exports. The map's own tests supply the output by
    hand, so a read of an export this program dropped would otherwise surface
    first as a bring-up that cannot fill `ZEROTIER_IDENTITY`. Which member each export carries is part
    of the contract rather than cosmetic: an identity live in two jobs at once
    flaps, which is why there is one per joining stack (gateway.md §2.6). The
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
    # The map reads every identity export there is -- one it did not would
    # join no job, having no secret pushed -- and everything the map reads out
    # of this stack's state, identities or not, is a name the structure
    # carries.
    assert contracted >= set(conventions.PHYSICAL_OUTPUTS.ci_identity.values())
    assert contracted <= set(conventions.PHYSICAL_OUTPUTS.names())

    for member, output in conventions.PHYSICAL_OUTPUTS.ci_identity.items():
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

    kubeconfig = cast('pulumi.Output[str]', exported[conventions.PHYSICAL_OUTPUTS.kubeconfig])
    talosconfig = cast('pulumi.Output[str]', exported[conventions.PHYSICAL_OUTPUTS.talosconfig])
    assert await kubeconfig.is_secret()
    assert await talosconfig.is_secret()
    assert await kubeconfig.future() == KUBECONFIG
    assert await talosconfig.future() == TALOSCONFIG


@pytest.mark.asyncio
async def test_the_worker_is_configured_through_the_cluster_endpoint(setup: Installation) -> None:
    """The worker's apid is reached without a route to its LAN address.

    apid routes by the node a call names, so the worker's configuration apply
    names the cluster-VLAN address the machine answers on and dials the
    balancer, which forwards the machine API port to whichever control plane it
    likes; that control plane proxies the call the rest of the way over the
    mesh. Nothing outside the site therefore needs a path to the worker, which
    is why a continuous-integration run confined to the overlay's four targets
    can still carry a worker configuration change.
    """
    async with declaring():
        await physical.main()

    worker = setup.inputs_of(f'{conventions.CLUSTER_NAME}-{conventions.HOMELAB_NODE}-config')
    assert worker['node'] == str(conventions.HOMELAB_NODE_IPV4)
    assert worker['endpoint'] == LB_ADDRESS
    # And the balancer forwards the machine API, or the endpoint above is a
    # closed door: the port is one of the two it declared a listener on.
    assert conventions.MANAGEMENT_PORTS.talos in listener_ports(setup)


def listener_ports(setup: Installation) -> set[int]:
    """The ports the balancer declared a listener on, out of the run."""
    return {int(inputs['port']) for inputs in setup.by_name(LISTENER).values()}


@pytest.mark.asyncio
async def test_the_cluster_endpoint_names_a_port_the_balancer_forwards_and_the_nodes_open(
    setup: Installation,
) -> None:
    """One structure, three declarations: the endpoint, the listener, the opening.

    The endpoint every machine configuration names is the balancer's address on
    the Kubernetes API port. That port is worth nothing unless the balancer
    forwards it and the nodes accept it, and the three are declared in three
    places -- this program, the balancer component, the Talos firewall patch --
    so what is held is that the run's own declarations agree, read off the
    run: the URL's port is among the listeners, and among the firewall's
    openings. The scheme is the earned literal: the machine configuration
    takes an HTTPS URL, and a bare address here is a cluster that never
    forms.
    """
    async with declaring():
        await physical.main()

    assert setup.configurations, 'no machine configuration was rendered'
    for configuration in setup.configurations:
        endpoint = str(configuration['clusterEndpoint'])
        parts = urlsplit(endpoint)
        assert parts.scheme == 'https', endpoint
        assert parts.hostname == LB_ADDRESS, endpoint
        assert parts.port in listener_ports(setup), endpoint
        assert parts.port in firewall_openings(configuration), endpoint


def firewall_openings(configuration: dict[str, Any]) -> set[int]:
    """Every port one machine's ingress firewall opens, out of its rendered patches."""
    documents = [json.loads(str(patch)) for patch in cast('list[Any]', configuration['configPatches'])]
    return {
        int(port)
        for document in documents
        if document.get('kind') == 'NetworkRuleConfig'
        for port in document['portSelector']['ports']
    }


LISTENER = 'oci:NetworkLoadBalancer/listener:Listener'


#: The instance id the mock answers a node's declaration with, which is how an
#: attachment can be asserted to have landed on the node the volume table names.
INSTANCE_IDS = {node: f'{conventions.CLUSTER_NAME}-{node}_id' for node in conventions.CLOUD_NODES}


@pytest.mark.asyncio
async def test_every_volume_is_attached_to_the_node_the_table_names(setup: Installation) -> None:
    """A block volume attaches only within its own availability domain.

    Both halves come off the instance rather than out of a constant, because
    the domain a node lands in is itself decided at apply time from what the
    region offers - a volume pinned to a remembered domain would fail to
    attach the first time the placement list came back in another order. Which
    instance each one lands on is the table's answer, and for the following
    volume that answer is the node holding the dedicated VIP.
    """
    async with declaring():
        await physical.main()

    for name, volume in conventions.NODE_VOLUMES.items():
        declared = setup.inputs_of(f'{conventions.CLUSTER_NAME}-{name}-volume')
        attachment = setup.inputs_of(f'{conventions.CLUSTER_NAME}-{name}-attachment')
        assert declared['availabilityDomain'] == AVAILABILITY_DOMAIN
        assert int(declared['sizeInGbs']) == volume.size_gb
        assert attachment['instanceId'] == INSTANCE_IDS[volume.attached_node]

    following = setup.inputs_of(f'{conventions.CLUSTER_NAME}-hath-cache-attachment')
    assert following['instanceId'] == INSTANCE_IDS[conventions.DEDICATED_VIP_NODE]


@pytest.mark.asyncio
async def test_the_volumes_and_the_backup_floor_get_the_fleets_values(setup: Installation) -> None:
    """The tier and the retention floor are this program's to pass, so this program is where they are held."""
    async with declaring():
        await physical.main()

    assert conventions.NODE_VOLUMES
    for volume in conventions.NODE_VOLUMES:
        inputs = setup.inputs_of(f'{conventions.CLUSTER_NAME}-{volume}-volume', 'oci:Core/volume:Volume')
        assert inputs['vpusPerGb'] == str(conventions.NODE_VOLUME_VPUS)
    (rule,) = setup.inputs_of(f'{conventions.CLUSTER_NAME}-backup', 'b2:index/bucket:Bucket')['lifecycleRules']
    assert rule['daysFromHidingToDeleting'] == conventions.BACKUP_VERSION_RETENTION_DAYS


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

    assert exported[conventions.PHYSICAL_OUTPUTS.backup_bucket] == conventions.BUCKET_BACKUP
    assert (
        exported[conventions.PHYSICAL_OUTPUTS.backup_endpoint]
        == f'https://s3.{conventions.B2_ACCOUNT.region}.backblazeb2.com'
    )

    # The one consumer that exists whether or not any application does.
    keys = cast('dict[str, dict[str, pulumi.Output[str]]]', exported[conventions.PHYSICAL_OUTPUTS.backup_keys])
    assert set(keys) == {'etcd'}
    assert await keys['etcd']['id'].future() == 'kluster-backup-etcd-key-id'


@pytest.mark.asyncio
async def test_the_quota_names_the_compartment_this_program_decided(setup: Installation) -> None:
    """A quota statement has no OCID form and names its compartment by name.

    Which is why that name is a convention rather than something read back from
    the tenancy, and why the stack hands the component both halves: the budget
    beside it targets the same compartment by OCID. The statements' own
    content - deny before allow, every family capped - is `test_guardrails`.
    """
    async with declaring():
        await physical.main()

    statements = cast('list[str]', setup.inputs_of(f'{conventions.CLUSTER_NAME}-quota')['statements'])
    assert statements
    assert all(text.endswith(f'in compartment {COMPARTMENT.name}') for text in statements)


@pytest.mark.asyncio
async def test_the_budget_alerts_reach_the_addresses_configuration_names(setup: Installation) -> None:
    """The only signal this stack raises that does not go through the cluster.

    The addresses are the one thing about the guardrails an operator supplies,
    so what this holds is the path from the configuration key to the rule.
    """
    async with declaring():
        await physical.main()

    alerts = [
        declaration.inputs
        for declaration in setup.declared
        if declaration.name.startswith(f'{conventions.CLUSTER_NAME}-budget-')
    ]
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
    """Three values, written by one command and read at one line.

    None of them is configuration of this program's own: they are the signing
    configuration the credential mint installs, and the stack program reads
    them where it builds the cloud provider. Asserting against the minter's
    constants is what keeps the reader from drifting onto keys nothing fills.
    """
    from kluster.scripts.credentials import derived

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

    Two namespaces, not one: `versions:` is this repository's own, holding
    every pin a stack program reads (framework/pulumi.md §3.2).
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
    (framework/pulumi.md §5.2) — so the key is absent from every read this
    program performs, and a run whose configuration lacks it gets as far as
    declaring the gateway.
    What the device-files provider does with a configuration that lacks it is
    the provider's own test.
    """
    pulumi.runtime.set_all_config(
        {name: value for name, value in STACK_CONFIG.items() if name != 'kluster:gatewayPrivateKey'}
    )

    async with declaring():
        physical._gateway(pulumi.Config())  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_the_gateway_arm_reads_the_configuration_its_channels_need() -> None:
    """The gateway, exercised without the rest of the stack.

    `main` reaches it now, but a failure there names the whole program; this
    isolates the arm whose wiring is entirely configuration — every key, and
    which of them is a secret — so a missing one is reported against the
    gateway rather than against a run of everything.
    """
    async with declaring():
        physical._gateway(pulumi.Config())  # pyright: ignore[reportPrivateUsage]


def test_a_root_filesystem_pin_is_the_whole_reference_a_push_pulls_by() -> None:
    """The pin carries repository, tag and digest, because that is what an image is.

    The two resolvers have a key each even though one build serves both, which
    is what lets a new resolver be proven on one instance before the other
    (rfc-002 §11.1).
    """
    caddy = conventions.gateway.CADDY
    pin = physical._rootfs(caddy)  # pyright: ignore[reportPrivateUsage]

    assert pin.digest == DIGEST
    assert pin.tag == ROOTFS_TAG
    assert pin.repository == f'{conventions.gateway.IMAGE_NAMESPACE}/{caddy.artifact}'

    alice, bob = conventions.gateway.RESOLVERS
    assert conventions.gateway.image_pin(alice) != conventions.gateway.image_pin(bob)
    assert physical._rootfs(alice).repository == physical._rootfs(bob).repository  # pyright: ignore[reportPrivateUsage]


def test_a_pin_naming_a_repository_that_does_not_publish_the_build_is_refused() -> None:
    """The census says which build a service runs, and one repository publishes it.

    Without this check two references could put the resolvers on two different
    images — a state the design says is impossible and nothing else would
    catch, since each pin is separately well-formed.
    """
    alice, _ = conventions.gateway.RESOLVERS
    key = f'versions:image-{conventions.gateway.image_pin(alice)}'
    pulumi.runtime.set_all_config(dict(STACK_CONFIG) | {key: f'ghcr.io/somebody/else:{ROOTFS_TAG}@{DIGEST}'})

    with pytest.raises(ValueError, match=key):
        _ = physical._rootfs(alice)  # pyright: ignore[reportPrivateUsage]


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
async def test_every_resource_is_signed_by_the_provider_its_owner_built(setup: Installation) -> None:
    """The whole point of the slice, as one assertion over the whole program.

    Every resource in the stack authenticates through a provider some component
    built explicitly, and no resource names one: each inherits it from its
    parent, transitively, because a provider set on a component is the default
    for its subtree. A resource that lost its parent would inherit the stack's
    providers instead -- which, with default providers disabled, is nothing at
    all.
    """
    async with declaring():
        await physical.main()

    checked = Counter[str]()
    for declaration in setup.declared:
        # A provider resource's own type token is `pulumi:providers:<package>`,
        # so it never matches a package prefix and never checks itself.
        for prefix, provider in SIGNED_BY.items():
            if not declaration.typ.startswith(prefix):
                continue
            assert provider in declaration.provider, f'{declaration.name} is not signed by {provider}'
            checked[prefix] += 1

    # Per family, because a total is met by whichever family has the most
    # resources: a stack that declared nothing at all under one package still
    # clears a total on its OCI resources alone, and the family that vanished
    # is the one nothing then says anything about.
    assert set(checked) == set(SIGNED_BY), checked


@pytest.mark.asyncio
async def test_the_cloud_provider_is_the_stack_programs_and_is_shared(setup: Installation) -> None:
    """One account, six components, one provider -- built where they meet.

    A provider built inside any one of them would be reached into by the other
    five, which is the test rfc-002 §8.1 gives for what the stack program owns.
    Neither its region nor its tenancy is configuration: both are permanent
    properties of the account and live in `conventions`, so the line that
    builds it reads exactly the three secrets -- which is also the whole of
    what the committed file has to carry for this account.
    """
    async with declaring():
        await physical.main()

    built = setup.inputs_of(f'{conventions.CLUSTER_NAME}-oci')
    assert built['region'] == conventions.OCI_TENANCY.region
    # The account's own identifiers arrive in the clear because they identify
    # rather than authenticate.
    assert built['tenancyOcid'] == TENANCY_ID
    # The three secrets arrive wrapped: the engine sees a marked value, which
    # is what keeps a signing key -- and the identifier that says whose it is
    # -- out of a preview and out of a log. `_unwrapped` asserts the marking,
    # so a value that lost it fails here rather than reaching a diff in the
    # clear.
    assert _unwrapped(built['userOcid']) == ACCOUNT_CONFIG['kluster:ociUserOcid']
    assert _unwrapped(built['fingerprint']) == ACCOUNT_CONFIG['kluster:ociFingerprint']
    assert _unwrapped(built['privateKey']) == ACCOUNT_CONFIG['kluster:ociPrivateKey']

    signed = {d.provider for d in setup.declared if d.typ.startswith('oci:')}
    assert len(signed) == 1, 'the cloud account has more than one provider'


@pytest.mark.asyncio
async def test_the_placement_lookups_name_the_provider_they_sign_with(setup: Installation) -> None:
    """A stack program's own invoke has no parent to inherit from.

    Both regional lookups are made outside any component, so nothing carries a
    provider to them: they name it. With default providers disabled an invoke
    that forgot would fail rather than sign as nobody.
    """
    async with declaring():
        await physical.main()

    for token in (
        'oci:Identity/getAvailabilityDomains:getAvailabilityDomains',
        'oci:Identity/getFaultDomains:getFaultDomains',
    ):
        assert f'{conventions.CLUSTER_NAME}-oci' in setup.call_providers[token], f'{token} signed as nobody'
