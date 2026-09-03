"""The container services on the device, asserted against Pulumi's mock provider.

Nothing here contacts a device. What is exercised is the part a diff cannot show
a reviewer: which file's change makes which machine restart, which file carries a
credential, what a machine's settings say about the box it starts on, where each
piece of a machine lands.

The renderers are plain functions over plain declarations, so most of the suite
reads their output directly; the components are declared once against mocks,
which is where a wiring mistake would surface.
"""

from __future__ import annotations

import re
from ipaddress import IPv4Address
from pathlib import Path

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import container, nspawn, persistence
from kluster.components.gateway.container import Container
from kluster.components.gateway.nspawn import NspawnRuntime
from kluster.components.gateway.persistence import DevicePersistence
from kluster.providers.device_files.provider import Connection, marker_path

NAME = 'kluster'
HOST = str(conventions.overlay.UDM)
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample'
ACME_TOKEN = 'a-zone-scoped-token'

DIGEST = f'sha256:{"f" * 64}'
TAG = '7'
SERVICES = tuple(service.name for service in conventions.gateway.SERVICES)
#: Where the census places the bridged services, restated rather than read back
#: from it: these are the addresses the LAN's leases already point at, so one
#: that moves should have to move here too.
ADDRESSES = {
    'caddy': IPv4Address('10.0.5.180'),
    'adguard-alice': IPv4Address('10.0.5.3'),
    'adguard-bob': IPv4Address('10.0.5.4'),
}


def pin(service: str) -> container.Rootfs:
    return container.Rootfs(repository=f'registry.invalid/estate/{service}', tag=TAG, digest=DIGEST)


def caddy(
    legacy: tuple[conventions.gateway.LegacyVhost, ...] = conventions.gateway.LEGACY_VHOSTS,
) -> container.CaddyService:
    """The proxy as the stack declares it, with the census a case is about."""
    return container.CaddyService(
        service=conventions.gateway.CADDY,
        pin=pin('caddy'),
        acme_token=ACME_TOKEN,
        vhosts=conventions.gateway.RESOLVERS,
        legacy=legacy,
    )


def declarations() -> tuple[container.ServiceDeclaration, ...]:
    """The four services, in the order the gateway takes them."""
    return (
        caddy(),
        *(
            container.ResolverService(service=resolver, pin=pin(resolver.name))
            for resolver in conventions.gateway.RESOLVERS
        ),
        container.OverlayDaemon(service=conventions.gateway.OVERLAY, pin=pin('zerotier')),
    )


def declared_for(name: str) -> container.ServiceDeclaration:
    return next(declaration for declaration in declarations() if declaration.service.name == name)


@pytest_asyncio.fixture(scope='module', autouse=True)
async def monitor() -> Recorder:
    """What the run registered, for the cases that read declarations directly."""
    return await run_with(Recorder(), stack='physical')


@pytest_asyncio.fixture(scope='module', autouse=True)
async def containers(monitor: Recorder) -> tuple[Container, ...]:
    """The four services on a runtime, the way `Gateway` declares them."""
    connection = Connection(host=HOST, host_key=HOST_KEY, username=conventions.gateway.SSH_USER)
    async with declaring():
        mechanism = DevicePersistence(
            f'{NAME}-persistence', connection=connection, packages=NspawnRuntime.REQUIRED_PACKAGES
        )
        runtime = NspawnRuntime(
            f'{NAME}-nspawn',
            mechanism=mechanism,
            machines=tuple(container.machine(declaration) for declaration in declarations()),
        )
        built = tuple(
            Container(
                f'{NAME}-{declaration.service.name}',
                declaration=declaration,
                runtime=runtime,
                connection=connection,
            )
            for declaration in declarations()
        )
    return built


##
## The census, as declarations
##


def test_a_resolver_cannot_be_declared_against_a_service_with_no_address() -> None:
    """The binding is a reference the type checker follows, not a name lookup.

    A resolver takes a bridged census entry, so the address its settings inject
    is the census's own; the overlay daemon takes the host-networked one, so it
    has no address to inject and no bridge to attach to. The mistakes this used
    to check for at runtime — a pin with no service, a service with no pin, a
    resolver bound to something that has no address — are now unwritable.
    """
    resolver = declared_for('adguard-alice')
    assert isinstance(resolver, container.ResolverService)
    assert resolver.service.address == ADDRESSES['adguard-alice']

    overlay = declared_for('zerotier')
    assert isinstance(overlay, container.OverlayDaemon)
    assert overlay.bridge is None
    assert overlay.devices == (container.TUN_DEVICE,)
    assert overlay.state == container.OVERLAY_STATE


def test_only_the_overlay_daemon_runs_in_the_hosts_network_namespace() -> None:
    """Host networking is what makes the gateway able to route the overlay.

    The interface has to land in the namespace the routing table lives in. The
    other three are bridged onto the container VLAN with addresses of their
    own, because the resolvers are what the LAN's leases point at.
    """
    for name in ('caddy', 'adguard-alice', 'adguard-bob'):
        declaration = declared_for(name)
        assert declaration.bridge == container.CONTAINER_BRIDGE
        assert isinstance(declaration.service, conventions.gateway.BridgedService)
        assert declaration.service.address == ADDRESSES[name]

    assert declared_for('zerotier').bridge is None


##
## Rendering
##


def test_a_bridged_service_is_placed_and_the_overlay_daemon_is_not() -> None:
    """The settings file is where host networking is said by cancelling a default.

    The template unit gives every machine a virtual ethernet pair, so a machine
    that must be in the host's own namespace has to say so — and a bridge
    accidentally added to it would quietly break every route the design puts
    through it.
    """
    bridged = container.nspawn_file(declared_for('adguard-alice'))
    overlay = container.nspawn_file(declared_for('zerotier'))

    assert f'Bridge={container.CONTAINER_BRIDGE}' in bridged
    assert 'VirtualEthernet=off' not in bridged
    assert 'Bridge=' not in overlay
    assert 'VirtualEthernet=off' in overlay
    assert f'Bind={container.TUN_DEVICE}' in overlay
    assert f'Bind={nspawn.state_path("zerotier")}:{container.OVERLAY_STATE}' in overlay


def test_the_host_networked_machine_keeps_the_privileges_the_host_namespace_needs() -> None:
    """A namespaced capability does not reach the host's network namespace.

    The template unit runs every machine in a user namespace, and the daemon
    that has to create an interface the *host* routes through cannot do it from
    inside one — it would join the overlay and route nothing. The bridged
    services need no such exception, and do not get one.
    """
    overlay = container.nspawn_file(declared_for('zerotier'))

    assert 'PrivateUsers=no' in overlay
    for name in ('caddy', 'adguard-alice', 'adguard-bob'):
        assert 'PrivateUsers' not in container.nspawn_file(declared_for(name)), name


def test_a_machine_names_no_unit_because_it_has_none_of_its_own() -> None:
    """systemd's template unit runs every machine, and it is systemd's.

    What a machine says about itself it says in its settings, which is the file
    `machinectl` and `systemd-nspawn@.service` already read; a unit written
    here would be a second such place and the one nothing consults.
    """
    for declaration in declarations():
        settings = container.nspawn_file(declaration)

        assert '[Unit]' not in settings, declaration.service.name
        assert 'ExecStart' not in settings, declaration.service.name
        assert declaration.unit_name == f'systemd-nspawn@{declaration.service.name}.service'


@pytest.mark.parametrize('service', ['adguard-alice', 'adguard-bob', 'caddy'])
def test_a_service_is_addressed_through_the_environment_its_image_reads(service: str) -> None:
    """The images run s6, and each configures its own interface from PID 1's
    environment, which the settings file fills — not out of a drop-in for a
    network manager the image does not carry. A file delivered to such a path is
    one nobody opens.

    Every bridged service, caddy included: its image carries the same
    `net-setup` the resolvers do, the proxy is ordered after that oneshot, and
    the oneshot exits non-zero when the addressing is absent. So a machine that
    did not deliver it would leave the reverse proxy down rather than leave it
    on a lease.
    """
    settings = container.nspawn_file(declared_for(service))

    for name, value in container.net_setup_environment(ADDRESSES[service]).items():
        assert f'Environment={name}={value}' in settings

    targets = [mounted.target for declaration in declarations() for mounted in declaration.mounted_files]
    assert not [target for target in targets if target.startswith('/etc/systemd/')], (
        'nothing in these images reads a systemd configuration path'
    )


def test_every_machine_runs_an_s6_image_on_the_terms_that_image_sets() -> None:
    """Four statements, each of which an s6 guest would otherwise fail.

    It boots the init the image ships rather than a systemd the image has not
    got. It treats a gentler signal as advisory and leaves supervisors holding
    the control group, so it is killed. It configures its own interface, which
    needs a capability nspawn does not keep by default. And nothing synthesizes
    a resolver file over the one the image decided on.
    """
    for declaration in declarations():
        settings = container.nspawn_file(declaration)

        assert 'Boot=on' in settings
        assert f'KillSignal={container.KILL_SIGNAL}' in settings
        assert f'Environment=S6_KILL_GRACETIME={container.S6_KILL_GRACETIME}' in settings
        assert f'Capability={container.CAPABILITY}' in settings
        assert 'ResolvConf=off' in settings


def test_caddy_is_told_where_to_read_its_configuration_and_where_to_keep_what_it_buys() -> None:
    """Both are environment variables, so the declaration names them or it guesses.

    The server is started with `--config $XDG_CONFIG_HOME/caddy/Caddyfile`, so
    a file delivered anywhere else is not the file it reads; and it places the
    certificates it earns under `XDG_DATA_HOME`, which has to be the directory
    bind-mounted from the device or a rootfs bump would throw them away.
    """
    caddy = declared_for('caddy')
    settings = container.nspawn_file(caddy)
    configuration = next(mounted for mounted in caddy.mounted_files if mounted.name == 'Caddyfile')

    assert f'Environment=XDG_CONFIG_HOME={container.CADDY_CONFIG_HOME}' in settings
    assert configuration.target == f'{container.CADDY_CONFIG_HOME}/caddy/Caddyfile'
    assert f'Environment=XDG_DATA_HOME={container.CADDY_STATE}' in settings
    assert caddy.state == container.CADDY_STATE
    assert f'Bind={nspawn.state_path("caddy")}:{container.CADDY_STATE}' in settings


def test_the_proxy_resolves_through_the_gateways_own_resolver_and_only_that_one() -> None:
    """It has to answer two questions no single public resolver answers.

    The proxy's upstreams are internal names, and its certificate issuance
    calls the registrar's API: the resolver on this same device answers the
    first and forwards the second, and it is not the pair the proxy fronts, so
    the proxy still does not depend on what it serves. Exactly one entry,
    because the image's resolver library asks every listed server in parallel
    and takes the first answer — a public resolver beside this one would win
    the race with NXDOMAIN for the internal names.
    """
    caddy = declared_for('caddy')
    resolver = next(mounted for mounted in caddy.mounted_files if mounted.name == 'resolv.conf')
    rendered = container.resolv_conf()
    gateway_address = conventions.CONTAINER_VLAN.require_gateway()

    assert resolver.target == container.CADDY_RESOLV_CONF == '/etc/resolv.conf'
    assert resolver.content == rendered
    assert [line for line in rendered.splitlines() if not line.startswith('#')] == [f'nameserver {gateway_address}']
    # The same address the image is handed as its default route, so the two
    # cannot disagree about which box is on the other side.
    assert container.net_setup_environment(ADDRESSES['caddy'])[container.ENV_IPV4_GATEWAY] == str(gateway_address)
    # Not the resolvers, which carry their own upstreams, and not the overlay
    # daemon, which is host-networked and resolves as the device does.
    for name in ('adguard-alice', 'adguard-bob', 'zerotier'):
        targets = [mounted.target for mounted in declared_for(name).mounted_files]
        assert container.CADDY_RESOLV_CONF not in targets, name


def test_a_machine_boots_a_tree_it_does_not_name() -> None:
    """The root filesystem is where systemd resolves a machine name, and no more.

    A settings file that named a directory would be a second opinion about
    which tree the machine boots, free to disagree with the link the converger
    maintains.
    """
    settings = container.nspawn_file(declared_for('caddy'))

    assert nspawn.rootfs_path('caddy') not in settings
    assert 'Image=' not in settings
    assert 'Directory=' not in settings


def test_a_resolver_is_placed_statically_and_points_at_more_than_one_upstream() -> None:
    """A resolver that waited for a lease would be waiting on itself.

    Its address is what the leases hand out as the name server, so it is
    configured rather than learned; and its own upstreams are two providers,
    because the LAN's name service must not stop with any one of them.
    """
    address = ADDRESSES['adguard-alice']
    environment = container.net_setup_environment(address)

    assert environment[container.ENV_IPV4_CIDR] == f'{address}/{conventions.CONTAINER_VLAN.v4.prefixlen}'
    assert environment[container.ENV_IPV4_GATEWAY] == str(conventions.CONTAINER_VLAN.require_gateway())
    # The v6 half is an interface identifier, not an address: the prefix keeps
    # arriving in advertisements, so the resolver survives it changing.
    assert environment[container.ENV_IPV6_TOKEN] == f'::{address}'

    rendered = container.adguard_initial_state(address)
    assert f'{address}:{conventions.gateway.ADGUARD_API_PORT}' in rendered
    for upstream in container.ADGUARD_UPSTREAMS:
        assert upstream in rendered
    assert len(container.ADGUARD_UPSTREAMS) > 1


def test_the_gateway_issues_its_own_certificates_from_its_own_credential() -> None:
    """Its TLS has to keep renewing while the cluster is down.

    So the vhosts answer a DNS-01 challenge with a token that lives on the
    device and nowhere else — never the cluster issuer's, which is the point of
    there being two.
    """
    rendered = container.caddyfile(caddy())

    assert conventions.gateway.VHOST_CONTROLLER in rendered
    assert f'dns cloudflare {{file.{container.CADDY_TOKEN_PATH}}}' in rendered
    for resolver in conventions.gateway.RESOLVERS:
        assert resolver.vhost is not None
        assert resolver.vhost in rendered
        assert f'http://{ADDRESSES[resolver.name]}:{conventions.gateway.ADGUARD_API_PORT}' in rendered
    # The console presents its own certificate to the proxy and the name that
    # matters is the one the client asked for, which Caddy forwards unchanged.
    assert 'tls_insecure_skip_verify' in rendered


def test_the_file_opens_with_the_defaults_the_whole_proxy_runs_on() -> None:
    """Three global options, and each of them is a decision rather than a default.

    The contact is what loads the ACME account the cutover carries across, which
    is why it is a constant and not a literal here. `debug` is what records which
    servers an issuance asked, and no other level has it. `admin off` retires an
    endpoint nothing in this program reconfigures the proxy through.
    """
    rendered = container.caddyfile(caddy())

    assert rendered.startswith(f'{{\n\temail {conventions.gateway.ACME_CONTACT}\n\tdebug\n\tadmin off\n}}\n')


def test_the_console_is_dialled_at_the_device_and_never_at_the_proxy_itself() -> None:
    """A loopback address in the proxy's own network namespace is the proxy.

    So the console is dialled at the device's leg on the container VLAN — the
    address the proxy's default route and its `resolv.conf` already name — and
    the block states the `Host` the console requires: it answers a WebSocket
    upgrade whose `Origin` does not match it with a 500.
    """
    rendered = container.caddyfile(caddy())

    device = conventions.CONTAINER_VLAN.require_gateway()
    assert f'\t\treverse_proxy https://{device} {{\n\t\t\theader_up Host {{host}}\n' in rendered
    assert '127.0.0.1' not in rendered


def test_no_challenge_is_checked_against_a_resolver_the_file_names() -> None:
    """Neither zone's `tls` block names one, and that is what keeps caches out.

    Given a resolver, the client asks that resolver's cache for the challenge
    record — which holds the answer it gave before the record was written for
    the zone's negative TTL. Given none, it discovers the zone's own name
    servers and asks them, and the only thing the proxy's resolver answers is
    that discovery.
    """
    assert 'resolvers' not in container.caddyfile(caddy())


def test_the_certificate_asked_for_is_the_wildcard_and_never_the_apex() -> None:
    """One site block for `*.<zone>`, and the three names matched inside it.

    Two reasons, and both are about what a request publishes (rfc-002 §9.3).
    Per-name issuance would republish in Certificate Transparency exactly the
    census that resolving nowhere was meant to hide. And the apex belongs to
    the cluster's issuer, whose certificate carries the apex and the wildcard
    together: asking for the same identifier set would put two issuers that
    must survive each other's outage into one weekly duplicate-certificate
    window.
    """
    rendered = container.caddyfile(caddy())

    zone = conventions.ZONE_PRIMARY
    assert f'\n*.{zone} {{\n' in rendered
    # One block for the zone, so one certificate for it: a second site block
    # under the same zone would be a second request for the same names.
    assert rendered.count(f'{zone} {{') == 1
    assert f'\n{zone} {{' not in rendered

    # Every served name is a matcher inside it, and everything else is refused
    # rather than answered by whichever block happened to be first.
    for vhost in (conventions.gateway.VHOST_CONTROLLER, *(r.vhost for r in conventions.gateway.RESOLVERS)):
        assert f'host {vhost}\n' in rendered
    assert 'abort' in rendered


##
## The legacy vhosts, until each application migrates
##


#: The device's live configuration, checked in beside this module: what the
#: legacy half of the render has to keep serving.
LIVE_CADDYFILE = Path(__file__).parent / 'data' / 'gw-config-caddyfile'

#: One `@name host <host>` matcher and the `handle` block it guards, which is
#: how both files spell a vhost. The body ends at the first closing brace back
#: at the block's own indentation.
VHOST_BLOCK = re.compile(
    r'^\t@(?P<matcher>\S+) host (?P<host>\S+)\n\thandle @(?P=matcher) \{\n(?P<body>.*?)\n\t\}$',
    re.MULTILINE | re.DOTALL,
)


def served(caddyfile: str) -> dict[str, tuple[str, ...]]:
    """Each vhost in a Caddyfile, as what its block tells Caddy.

    Keyed by the name clients ask for, so the two files are compared on the
    thing they have in common rather than on how they are laid out. What a
    block says is its directives with the spelling taken out: comments and
    indentation dropped, and the two defaults the live file leans on written
    the way the render writes them — an upstream with no scheme is plain HTTP
    on port 80, and `tls` inside a transport is what the `https://` scheme
    already turned on.
    """
    return {match['host']: directives(match['body']) for match in VHOST_BLOCK.finditer(caddyfile)}


def directives(body: str) -> tuple[str, ...]:
    lines = (' '.join(line.split()) for line in body.splitlines())
    return tuple(explicit(line) for line in lines if line and not line.startswith('#') and line != 'tls')


def explicit(directive: str) -> str:
    """One directive with the upstream's scheme and port spelled out."""
    proxy, _, upstream = directive.partition('reverse_proxy ')
    if proxy or not upstream:
        return directive
    dial, brace, trailer = upstream.partition(' {')
    if '://' not in dial:
        dial = f'http://{dial}' if ':' in dial else f'http://{dial}:80'
    return f'reverse_proxy {dial}{brace}{trailer}'


def test_every_name_the_device_serves_today_is_still_served() -> None:
    """The cutover replaces the live file whole, weeks before the first application moves.

    So each name the device answers for under the retiring zone is a row in the
    census, and each row renders — a name missing from here is a name that goes
    dark on the day the device is taken over rather than on the day its
    application migrates.
    """
    rendered = served(container.caddyfile(caddy()))
    live = served(LIVE_CADDYFILE.read_text(encoding='utf-8'))

    legacy = [vhost.host for vhost in conventions.gateway.LEGACY_VHOSTS]
    assert set(legacy) == {host for host in live if host.endswith(conventions.gateway.ZONE_LEGACY)}
    for host in legacy:
        assert host in rendered


def test_each_legacy_vhost_proxies_where_the_device_proxies_it() -> None:
    """Transcription, not redesign: the census carries what the live file carries.

    Every directive of every legacy block — the upstream, the header the UniFi
    console needs, the transport that skips verification on an appliance's own
    certificate — comes across as it is. What changes at the cutover is which
    program renders the file, not what the file says.
    """
    rendered = served(container.caddyfile(caddy()))
    live = served(LIVE_CADDYFILE.read_text(encoding='utf-8'))

    for vhost in conventions.gateway.LEGACY_VHOSTS:
        assert rendered[vhost.host] == live[vhost.host], vhost.host


def test_the_legacy_block_is_its_own_certificate_and_refuses_the_rest() -> None:
    """A second zone is a second wildcard, and the block is shaped like the first.

    Its own `tls` block, because a second zone is a second certificate asked for
    on the same credential. Its own `handle` fallback, so a name under the
    retiring zone that nothing here serves is refused rather than answered by
    the block that happened to match.
    """
    rendered = container.caddyfile(caddy())

    zone = conventions.gateway.ZONE_LEGACY
    tls = f'\ttls {{\n\t\tdns cloudflare {{file.{container.CADDY_TOKEN_PATH}}}\n\t}}\n'
    assert f'\n*.{zone} {{\n{tls}' in rendered
    assert rendered.count('abort') == 2


def test_an_empty_census_is_a_file_with_no_legacy_block_in_it() -> None:
    """Deleting the last row is what retires the zone, and nothing else has to happen.

    A block rendered unconditionally would go on asking for a wildcard for a
    zone nothing is served under, and go on needing that zone in the scope of
    the token the device answers challenges with. So the census is the switch:
    with no rows there is no site block and no redirect.
    """
    rendered = container.caddyfile(caddy(legacy=()))

    assert conventions.gateway.ZONE_LEGACY not in rendered
    assert rendered.count('abort') == 1
    # And what the device's own services are served under is untouched.
    assert f'\n*.{conventions.ZONE_PRIMARY} {{\n' in rendered
    assert conventions.gateway.VHOST_CONTROLLER in rendered


def test_the_names_typed_by_hand_redirect_to_the_name_that_has_a_certificate() -> None:
    """The bare label is a site of its own, and the device serves five of them.

    A redirect rather than a second matcher on the vhost: the wildcard
    certificate does not cover a one-label name, so the only thing the proxy
    can do over plain HTTP is send the client to the name it does cover.
    """
    rendered = container.caddyfile(caddy())
    live = LIVE_CADDYFILE.read_text(encoding='utf-8')

    assert sum(vhost.bare_name for vhost in conventions.gateway.LEGACY_VHOSTS) == 5
    for vhost in conventions.gateway.LEGACY_VHOSTS:
        block = f'http://{vhost.label} {{\n\tredir https://{vhost.host}{{uri}} permanent\n}}\n'
        assert (block in rendered) == vhost.bare_name
        assert (block in live) == vhost.bare_name


##
## What the converger is told about a machine
##


def test_a_machine_is_restarted_only_when_something_it_reads_changed() -> None:
    """Otherwise every deployment would restart the machine carrying the session.

    The content stamp is a checksum over the machine's stamped set — its
    settings, the digest marker beside its root filesystem tree, and every file
    bound into the container — and the converger compares before acting. The
    tree is represented by its marker rather than by itself: walking a root
    filesystem to learn it has not changed would cost more than the restart it
    avoids.
    """
    caddy = declared_for('caddy')
    script = nspawn.machines_script([container.machine(declaration) for declaration in declarations()])

    assert caddy.stamped_set == (
        nspawn.nspawn_path('caddy'),
        marker_path(nspawn.rootfs_path('caddy')),
        *(container.mounted_path('caddy', mounted) for mounted in caddy.mounted_files),
    )
    for path in caddy.stamped_set:
        assert path in script


def stamped_arms(script: str) -> dict[str, set[str]]:
    """What the rendered converger checksums, per machine, read back out of it.

    The stamped set reaches the device as one shell `case` arm per machine, and
    that text is the only thing the device acts on — so the case is read as the
    device reads it rather than through the property that produced it.
    """
    return {
        machine: set(paths.split())
        for machine, paths in re.findall(r'^\s*([\w.-]+)\) stamped="([^"]*)" ;;$', script, re.MULTILINE)
    }


def test_the_stamped_sets_are_the_children_and_nothing_else(
    containers: tuple[Container, ...], monitor: Recorder
) -> None:
    """A stamp cannot name a file no resource declares, or miss one that does.

    The converger is rendered from the same declarations the containers are
    built from, so what the device checksums for a machine is exactly the files
    that machine's component declares — no extra path a hand-written case arm
    could add, and none dropped. Set equality both ways is the whole claim: a
    path in the script that belongs to no child is a restart nothing can
    trigger, and a child's file missing from the script is a change the device
    never notices.
    """
    arms = stamped_arms(nspawn.machines_script([container.machine(declaration) for declaration in declarations()]))

    assert arms == {name: set(child.stamped_set) for name, child in zip(SERVICES, containers, strict=True)}
    # And every declared path is a file some child of this component owns.
    declared_paths = {
        declaration.inputs['path']
        for declaration in monitor.of_type('pulumi-python:dynamic/device:File')
        if 'path' in declaration.inputs
    }
    marker_paths = {marker_path(nspawn.rootfs_path(name)) for name in SERVICES}
    assert set().union(*arms.values()) <= declared_paths | marker_paths


def test_a_resolvers_own_configuration_is_installed_once_and_then_left_alone() -> None:
    """The instance rewrites the file the moment the `dns` stack adds a rewrite.

    So the initial state is delivered under a name the instance does not read,
    and the converger copies it into the working directory only while that
    directory is empty — which is the state of a machine the device has never
    run, and never the state of one that has been running. It is not in the
    stamped set either: a change to it can never be a reason to restart an
    instance that has already made the file its own.
    """
    alice = declared_for('adguard-alice')
    initial = alice.initial_state
    assert initial is not None
    assert initial.name == container.ADGUARD_INITIAL_STATE != 'AdGuardHome.yaml'
    assert declared_for('caddy').initial_state is None, 'caddy owns nothing it is given'

    delivered = nspawn.machine_file('adguard-alice', initial.name)
    assert delivered not in alice.stamped_set

    placement = container.machine(alice).initial_state
    assert placement is not None
    assert placement.source == delivered
    assert placement.destination == f'{nspawn.state_path("adguard-alice")}/AdGuardHome.yaml'


def test_a_resolver_is_bound_at_the_working_directory_its_image_is_started_with() -> None:
    """The image decides this, not the declaration: the census follows the layout.

    The resolver runs from an installation the root filesystem carries and
    keeps its configuration, query log and statistics in a working directory
    outside it. Binding the machine's state anywhere else would give the
    instance an empty directory to fill and leave the initial state sitting
    where nothing reads it — the failure would be a resolver that came up with
    default settings rather than an error anyone sees.
    """
    alice = declared_for('adguard-alice')

    assert alice.state == container.ADGUARD_STATE
    assert container.ADGUARD_STATE != container.ADGUARD_INSTALL
    # The live configuration is the working directory's, and the initial state
    # is delivered beside it rather than into it, so that placing one can never
    # overwrite the other.
    assert alice.mounted_files == ()

    settings = container.nspawn_file(alice)
    assert f'Bind={nspawn.state_path("adguard-alice")}:{container.ADGUARD_STATE}' in settings


##
## The declaration
##


def test_every_piece_of_a_machine_lands_in_that_machines_directory(monitor: Recorder) -> None:
    """A machine can be inspected, moved or deleted whole, which is the point.

    Its tree, its settings, the files it mounts and the state it is seeded with
    are one directory, so nothing about a service is left behind somewhere else
    when the service goes.
    """
    for service in SERVICES:
        directory = f'{nspawn.machine_path(service)}/'
        for name in (f'{NAME}-{service}-nspawn', f'{NAME}-{service}-image'):
            inputs = monitor.inputs_of(name)
            path = inputs.get('path') or inputs['root']
            assert str(path).startswith(directory), name

    assert monitor.inputs_of(f'{NAME}-caddy-nspawn')['path'] == nspawn.nspawn_path('caddy')
    assert monitor.inputs_of(f'{NAME}-caddy-image')['root'] == nspawn.rootfs_path('caddy')
    assert monitor.inputs_of(f'{NAME}-adguard-alice-initial-state')['path'] == nspawn.machine_file(
        'adguard-alice', container.ADGUARD_INITIAL_STATE
    )


def test_every_file_of_a_machine_converges_that_machine_and_holds_it_to_starting(monitor: Recorder) -> None:
    """The recovery path and the deployment path are the same path.

    A separate apply command would be a second way of doing the same thing, and
    the one that only runs after a firmware update is the one that would rot
    unnoticed. Each file's hook is for its own path, because the same command
    runs after the delete and a machine on its way out must not be rolled back
    for failing to be active.
    """
    files = [
        declaration
        for declaration in monitor.declared
        if declaration.name.startswith(f'{NAME}-caddy-') and 'hook' in declaration.inputs
    ]

    assert sorted(declaration.name for declaration in files) == [
        f'{NAME}-caddy-file-Caddyfile',
        f'{NAME}-caddy-file-cloudflare.token',
        f'{NAME}-caddy-file-resolv.conf',
        f'{NAME}-caddy-image',
        f'{NAME}-caddy-nspawn',
    ]
    for declaration in files:
        path = declaration.inputs.get('path') or declaration.inputs['root']
        rollback = declaration.name == f'{NAME}-caddy-image'
        assert declaration.inputs['hook'] == nspawn.machine_hook('caddy', str(path), rollback=rollback), (
            declaration.name
        )


def test_only_the_root_filesystem_may_roll_the_machine_back(monitor: Recorder) -> None:
    """The tree is the only piece of a machine the device keeps a copy of.

    A configuration file's hook that swapped it would replace a tree unrelated
    to the failure and leave the bad configuration in place, so the next push
    would deliver it again and swap again.
    """
    rolls_back = [
        declaration.name
        for declaration in monitor.declared
        if nspawn.ROLLBACK_PROGRAM in str(declaration.inputs.get('hook', ''))
    ]

    assert sorted(rolls_back) == sorted(f'{NAME}-{service}-image' for service in SERVICES)


@pytest.mark.asyncio
async def test_a_machines_files_wait_for_the_runtime_that_converges_them(monitor: Recorder) -> None:
    """A hook that runs a script the device has not been given fails its apply.

    Pulumi does not push a component's own dependencies down to its children,
    so every file of a machine states this rather than the component stating it
    once. The package script is in the set because the two programs the device
    pulls and unpacks a tree with are not on the box until it has run.
    """
    settings = monitor.depends_on(f'{NAME}-caddy-nspawn')

    for name in (
        f'{NAME}-persistence-on-boot-{nspawn.MACHINES_SCRIPT}',
        f'{NAME}-persistence-on-boot-{persistence.PACKAGES_SCRIPT}',
        f'{NAME}-persistence-bin-{nspawn.ROLLBACK_PROGRAM}',
    ):
        assert any(urn.endswith(f'::{name}') for urn in settings), name


@pytest.mark.asyncio
async def test_the_tree_lands_last_so_the_machine_starts_once_with_everything(
    containers: tuple[Container, ...], monitor: Recorder
) -> None:
    """The delivery that starts a machine is the one that gives it a tree.

    The converger skips a machine with no root filesystem, so on the push that
    creates one every configuration file arrives at a machine that cannot start
    yet. Ordering the tree behind them is what makes the start happen once,
    with everything the container reads already on the device — and what keeps
    those files' own hooks from holding a machine to a unit nothing has
    started.
    """
    caddy = containers[0]
    image = monitor.depends_on(f'{NAME}-caddy-image')

    assert str(await caddy.settings.urn.future()) in image
    for mounted in caddy.mounted_files.values():
        assert str(await mounted.urn.future()) in image
    # And the settings wait for the files they name as binds.
    settings = monitor.depends_on(f'{NAME}-caddy-nspawn')
    for mounted in caddy.mounted_files.values():
        assert str(await mounted.urn.future()) in settings

    alice = next(child for child in containers if child.stamped_set[0] == nspawn.nspawn_path('adguard-alice'))
    assert alice.initial_state is not None
    assert str(await alice.initial_state.urn.future()) in monitor.depends_on(f'{NAME}-adguard-alice-image')


def test_a_root_filesystem_travels_as_a_pin_and_never_as_bytes(monitor: Recorder) -> None:
    """State carries the digest; the device fetches the payload for itself.

    An image in state would make every preview download megabytes to compare
    them against megabytes, and would put a container's whole filesystem into
    the deployment history.
    """
    images = monitor.of_type('pulumi-python:dynamic/device:Artifact')  # noqa: S105 -- a type, not a credential

    assert sorted(image.name for image in images) == sorted(f'{NAME}-{service}-image' for service in SERVICES)
    for image in images:
        assert image.inputs['digest'] == DIGEST, image.name
        assert image.inputs['tag'] == TAG, image.name
        assert image.inputs['repository'].startswith('registry.invalid/'), image.name
        assert 'content' not in image.inputs, image.name


def test_each_root_filesystem_owns_the_tree_its_machine_boots(monitor: Recorder) -> None:
    """A pin that moves replaces the tree the machine boots, and no other.

    An artifact that unpacked somewhere the converger does not link would leave
    the container running the filesystem it already had, with nothing to say so.
    """
    for service in SERVICES:
        inputs = monitor.inputs_of(f'{NAME}-{service}-image')

        assert inputs['root'] == nspawn.rootfs_path(service)


def test_the_device_secret_is_declared_secret(monitor: Recorder) -> None:
    """The token file is the credential itself, so it may not render in a preview.

    Everything else the machine holds is configuration one wants to read in a
    diff, which is why content is not secret by default.
    """
    token = monitor.inputs_of(f'{NAME}-caddy-file-cloudflare.token')

    assert token['content'] == ACME_TOKEN
    assert token['mode'] == container.SECRET_MODE
    assert monitor.inputs_of(f'{NAME}-caddy-file-Caddyfile')['mode'] == container.CONFIG_MODE
