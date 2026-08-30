"""The device's desired state, asserted against Pulumi's mock provider.

Nothing here contacts a device. What is exercised is the part a diff cannot show
a reviewer: which file's change makes which service restart, which file carries a
credential, what a unit requires of the machine it starts on, what the boot-time
script does when it finds a device that has nothing on it, and what the routing
configuration says about a peer that misbehaves.

The renderers are plain functions over plain declarations, so most of the suite
reads their output directly; the component tree is declared once against mocks,
which is where a wiring mistake would surface.
"""

from __future__ import annotations

import re
from ipaddress import IPv4Address

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import container, services
from kluster.providers.device_files.provider import Connection

NAME = 'kluster'
HOST = str(conventions.overlay.UDM)
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample'
BGP_PASSWORD = 'a-session-password'
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


def declarations() -> tuple[container.ServiceDeclaration, ...]:
    """The four services, in the order `DeviceServices` takes them."""
    return (
        container.CaddyService(service=conventions.gateway.CADDY, pin=pin('caddy'), acme_token=ACME_TOKEN),
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
async def stack(monitor: Recorder) -> services.DeviceServices:
    """The services declared once, the way `Gateway` declares them."""
    caddy, alice, bob, overlay = declarations()
    assert isinstance(caddy, container.CaddyService)
    assert isinstance(alice, container.ResolverService)
    assert isinstance(bob, container.ResolverService)
    assert isinstance(overlay, container.OverlayDaemon)

    async with declaring():
        device = services.DeviceServices(
            NAME,
            connection=Connection(host=HOST, host_key=HOST_KEY, username=conventions.gateway.SSH_USER),
            caddy=caddy,
            resolvers=(alice, bob),
            overlay_daemon=overlay,
            routing=services.RoutingSession(neighbour=conventions.HOMELAB_NODE_IPV4, password=BGP_PASSWORD),
        )
    return device


##
## The census, as declarations
##


def test_a_resolver_cannot_be_declared_against_a_service_with_no_address() -> None:
    """The binding is a reference the type checker follows, not a name lookup.

    A resolver takes a bridged census entry, so the address its unit injects is
    the census's own; the overlay daemon takes the host-networked one, so it has
    no address to inject and no bridge to attach to. The mistakes this used to
    check for at runtime — a pin with no service, a service with no pin, a
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


def test_the_routing_configuration_confines_what_the_peer_may_announce() -> None:
    """Three defences, and each of them is a line a reviewer can find.

    Without the prefix-list, anything holding the worker's address could
    announce the resolvers' own /32s and take the LAN's name service with it;
    without the cap, it could announce the pool one address at a time until the
    table gave out; without the password, holding the address would be enough
    to be the peer.
    """
    rendered = services.frr_config(neighbour=conventions.HOMELAB_NODE_IPV4, password=BGP_PASSWORD)
    peer = str(conventions.HOMELAB_NODE_IPV4)

    assert f'neighbor {peer} remote-as {conventions.CLUSTER_ASN}' in rendered
    assert f'router bgp {conventions.UDM_ASN}' in rendered
    assert f'neighbor {peer} password {BGP_PASSWORD}' in rendered
    assert f'neighbor {peer} maximum-prefix {services.MAX_PREFIXES}' in rendered
    assert f'permit {conventions.LAN_POOL.v4} le 32' in rendered
    assert f'permit {conventions.LAN_POOL.v6} le 128' in rendered
    assert rendered.count('deny any') == 2, 'each family admits the pool and refuses the rest'
    # Both families ride the one session, so both have to be activated on it.
    assert rendered.count(f'neighbor {peer} activate') == 2


def test_a_bridged_service_is_placed_and_the_overlay_daemon_is_not() -> None:
    """The unit is where host networking is the absence of an argument.

    `systemd-nspawn` shares the host's namespace unless a network is asked
    for, so the overlay daemon is described by having no address rather than by
    a flag — and a bridge argument accidentally added to it would quietly break
    every route the design puts through it.
    """
    bridged = container.unit_file(declared_for('adguard-alice'))
    overlay = container.unit_file(declared_for('zerotier'))

    assert f'--network-bridge={container.CONTAINER_BRIDGE}' in bridged
    assert '--network-bridge' not in overlay
    assert f'--bind={container.TUN_DEVICE}' in overlay
    # A bind is not access: the unit's own device policy has to admit it too.
    assert f'DeviceAllow={container.TUN_DEVICE} rw' in overlay
    # And naming a device is what narrows that policy to a list, so the console
    # the container needs anyway has to be on it — a pseudo-terminal, which the
    # set every service gets does not include.
    assert 'DeviceAllow=char-pts rw' in overlay
    assert 'DeviceAllow' not in bridged, 'a service that names no device keeps the open policy'
    assert f'--bind={container.state_path("zerotier")}:{container.OVERLAY_STATE}' in overlay
    assert f'--directory={container.root_path("zerotier")}' in overlay


def test_a_bridged_unit_binds_to_the_bridge_it_needs() -> None:
    """It cannot be started against a bridge that does not exist yet.

    That is the race `Restart=always` used to absorb: the service came up,
    failed to attach, and was restarted until the bridge happened to appear.
    """
    bridged = container.unit_file(declared_for('caddy'))
    bridge_unit = container.bridge_device_unit(container.CONTAINER_BRIDGE)

    assert bridge_unit == 'sys-subsystem-net-devices-br5.device'
    assert f'BindsTo={bridge_unit}' in bridged
    assert f'After={bridge_unit}' in bridged
    assert 'AssertPathExists' not in bridged


def test_the_overlay_daemon_asserts_its_device_rather_than_binding_to_it() -> None:
    """Nothing tags `/dev/net/tun` in `udev`, so its device unit never activates.

    A dependency on that unit would turn a working service into a permanently
    failed one; without the assertion the daemon instead logs that it cannot
    open the device and sleeps, leaving a unit that is active and doing
    nothing.
    """
    overlay = container.unit_file(declared_for('zerotier'))
    bridge_unit = container.bridge_device_unit(container.CONTAINER_BRIDGE)

    assert f'AssertPathExists={container.TUN_DEVICE}' in overlay
    assert f'BindsTo={bridge_unit}' not in overlay
    assert 'sys-subsystem-net-devices' not in overlay


def test_every_unit_waits_for_the_network_to_be_up() -> None:
    for declaration in declarations():
        unit = container.unit_file(declaration)

        assert 'After=network-online.target' in unit, declaration.service.name
        assert 'Wants=network-online.target' in unit, declaration.service.name


def test_no_unit_names_another() -> None:
    """The recovery script chooses no order, and neither do the units.

    Caddy proxies to the resolvers at request time and the overlay daemon
    carries the session rather than the others' traffic, so there is no
    start-up ordering between them to state.
    """
    for declaration in declarations():
        unit = container.unit_file(declaration)

        for other in declarations():
            assert other.unit_name not in unit, f'{declaration.service.name} names {other.service.name}'


@pytest.mark.parametrize('service', ['adguard-alice', 'adguard-bob', 'caddy'])
def test_a_service_is_addressed_through_the_environment_its_image_reads(service: str) -> None:
    """The images run s6, and each configures its own interface from PID 1's
    environment, which the unit fills with `--setenv` — not out of a drop-in for
    a network manager the image does not carry. A file delivered to such a path
    is one nobody opens.

    Every bridged service, caddy included: its image carries the same
    `net-setup` the resolvers do, the proxy is ordered after that oneshot, and
    the oneshot exits non-zero when the addressing is absent. So a unit that
    did not deliver it would leave the reverse proxy down rather than leave it
    on a lease.
    """
    unit = container.unit_file(declared_for(service))

    for name, value in container.net_setup_environment(ADDRESSES[service]).items():
        assert f'--setenv={name}={value}' in unit
    # The unit's own `Environment=` is the environment of the process on the
    # device, which is not the one the image reads.
    assert 'Environment=' not in unit

    targets = [mounted.target for declaration in declarations() for mounted in declaration.mounted_files]
    assert not [target for target in targets if target.startswith('/etc/systemd/')], (
        'nothing in these images reads a systemd configuration path'
    )


def test_every_unit_runs_an_s6_image_on_the_terms_that_image_sets() -> None:
    """Four statements, each of which an s6 guest would otherwise fail.

    It never reports readiness, so nspawn answers for it and the unit is ready
    once the container's PID 1 exists. It treats a gentler signal as advisory
    and leaves supervisors holding the control group, so it is killed. It
    configures its own interface, which needs a capability nspawn does not keep
    by default. And it ships an `/etc/resolv.conf` that is an answer to a
    question — the overlay daemon resolves through public servers because it
    has to come up while this device's own resolvers are down — so nothing
    overwrites it.
    """
    for declaration in declarations():
        unit = container.unit_file(declaration)

        assert '--notify-ready=no' in unit
        assert 'Type=notify' in unit
        assert f'--kill-signal={container.KILL_SIGNAL}' in unit
        assert f'--setenv=S6_KILL_GRACETIME={container.S6_KILL_GRACETIME}' in unit
        assert '--capability=CAP_NET_ADMIN' in unit
        assert '--resolv-conf=off' in unit


def test_caddy_is_told_where_to_read_its_configuration_and_where_to_keep_what_it_buys() -> None:
    """Both are environment variables, so the declaration names them or it guesses.

    The server is started with `--config $XDG_CONFIG_HOME/caddy/Caddyfile`, so
    a file delivered anywhere else is not the file it reads; and it places the
    certificates it earns under `XDG_DATA_HOME`, which has to be the directory
    bind-mounted from the device or a rootfs bump would throw them away.
    """
    caddy = declared_for('caddy')
    unit = container.unit_file(caddy)
    configuration = next(mounted for mounted in caddy.mounted_files if mounted.name == 'Caddyfile')

    assert f'--setenv=XDG_CONFIG_HOME={container.CADDY_CONFIG_HOME}' in unit
    assert configuration.target == f'{container.CADDY_CONFIG_HOME}/caddy/Caddyfile'
    assert f'--setenv=XDG_DATA_HOME={container.CADDY_STATE}' in unit
    assert caddy.state == container.CADDY_STATE
    assert f'--bind={container.state_path("caddy")}:{container.CADDY_STATE}' in unit


def test_a_unit_boots_the_unpacked_tree_and_not_the_tarball_that_carried_it() -> None:
    """The pins are root filesystem archives, and `--image=` cannot boot one.

    So the artifact lands as a tarball and the push unpacks it into a tree the
    unit names with `--directory=`. The two paths are distinct on purpose: the
    tarball is what the digest pins, the tree is derived state the push
    replaces, and a unit pointed at the archive would not start at all.
    """
    unit = container.unit_file(declared_for('caddy'))

    assert f'--directory={container.root_path("caddy")}' in unit
    assert '--image=' not in unit
    assert container.image_path('caddy').endswith('.tar')
    assert container.root_path('caddy') != container.image_path('caddy')


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
    rendered = container.caddyfile()

    assert conventions.gateway.VHOST_CONTROLLER in rendered
    assert f'dns cloudflare {{file.{container.CADDY_TOKEN_PATH}}}' in rendered
    for resolver in conventions.gateway.RESOLVERS:
        assert resolver.vhost is not None
        assert resolver.vhost in rendered
        assert f'http://{ADDRESSES[resolver.name]}:{conventions.gateway.ADGUARD_API_PORT}' in rendered
    # The console presents its own certificate to the proxy and the name that
    # matters is the one the client asked for, which Caddy forwards unchanged.
    assert 'tls_insecure_skip_verify' in rendered


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
    rendered = container.caddyfile()

    zone = conventions.ZONE_PRIMARY
    assert rendered.startswith(f'*.{zone} {{\n')
    # One block, so one certificate: a second site block is a second request.
    assert rendered.count(f'{zone} {{') == 1
    assert f'\n{zone} {{' not in rendered

    # Every served name is a matcher inside it, and everything else is refused
    # rather than answered by whichever block happened to be first.
    for vhost in (conventions.gateway.VHOST_CONTROLLER, *(r.vhost for r in conventions.gateway.RESOLVERS)):
        assert f'host {vhost}\n' in rendered
    assert 'abort' in rendered


##
## The recovery script
##


def test_the_service_carrying_the_session_is_converged_last() -> None:
    """Restarting the overlay daemon drops the session that asked for it.

    Everything else therefore converges first, so an apply that dies on that
    last restart has already done the rest of its work and the retry finds it
    done. The order is the signature's: the daemon is its own parameter, not an
    entry in a list something has to sort.
    """
    script = services.recovery_script(declarations())
    declared_units = next(line for line in script.splitlines() if line.startswith('DECLARED='))

    assert declared_units.rstrip('"').endswith(declared_for('zerotier').unit_name)
    assert all(declared_for(name).unit_name in declared_units for name in SERVICES)


def test_a_service_is_restarted_only_when_something_it_reads_changed() -> None:
    """Otherwise every deployment would restart the overlay daemon.

    The content stamp is a checksum over the service's stamped set — the unit,
    the digest marker beside its root filesystem tree, and every file bound into
    the container — and the script compares before acting. The tree is
    represented by its marker rather than by itself: walking a root filesystem
    to learn it has not changed would cost more than the restart it avoids.
    """
    caddy = declared_for('caddy')
    script = services.recovery_script(declarations())

    assert caddy.stamped_set == (
        f'{container.UNIT_DIR}/{caddy.unit_name}',
        f'{container.root_path("caddy")}.digest',
        container.mounted_path('caddy', caddy.mounted_files[0]),
        container.mounted_path('caddy', caddy.mounted_files[1]),
    )
    for path in caddy.stamped_set:
        assert path in script
    assert 'systemctl is-active --quiet' in script
    assert 'cksum' in script


def stamped_arms(script: str) -> dict[str, set[str]]:
    """What the rendered script checksums, per unit, read back out of it.

    The stamped set reaches the device as one shell `case` arm per unit, and
    that text is the only thing the device acts on — so the case is read as the
    device reads it rather than through the property that produced it.
    """
    return {
        unit: set(paths.split())
        for unit, paths in re.findall(r'^\s*(\S+\.service)\) stamped="([^"]*)" ;;$', script, re.MULTILINE)
    }


def test_the_stamped_sets_are_the_children_and_nothing_else(stack: services.DeviceServices, monitor: Recorder) -> None:
    """A stamp cannot name a file no resource declares, or miss one that does.

    The script is rendered from the same declarations the containers are built
    from, so what the device checksums for a service is exactly the files that
    service's component declares — no extra path a hand-written case arm could
    add, and none dropped. Set equality both ways is the whole claim: a path in
    the script that belongs to no child is a restart nothing can trigger, and a
    child's file missing from the script is a change the device never notices.
    """
    arms = stamped_arms(services.recovery_script(declarations()))

    assert arms == {child.unit_name: set(child.stamped_set) for child in stack.containers}
    # And every declared path is a file some child of this component owns.
    declared_paths = {
        declaration.inputs['path']
        for declaration in monitor.of_type('pulumi-python:dynamic/device:File')
        if 'path' in declaration.inputs
    }
    marker_paths = {f'{container.root_path(child_name)}.digest' for child_name in SERVICES}
    assert set().union(*arms.values()) <= declared_paths | marker_paths


def test_a_new_root_filesystem_is_noticed_through_the_marker_beside_the_tree() -> None:
    """The tarball's own marker is written after this script has already run.

    The artifact resource writes it last, as the claim that the whole push
    succeeded, so a stamp built from it would compare equal on the very run
    that installed the new tree and the service would keep running the old one
    until some unrelated file changed. The marker beside the tree is written
    before the hook, which is why it is the one the stamp reads.
    """
    script = services.recovery_script(declarations())

    assert f'{container.image_path("caddy")}.digest' not in script
    assert f'{container.root_path("caddy")}.digest' in script


def test_the_script_converges_a_device_that_has_nothing_on_it() -> None:
    """This is the firmware-update case, and the first-deployment case.

    The script is written before the files it describes, so a service whose
    unit or root filesystem tree has not landed yet is skipped rather than
    fatal — and a unit no longer declared is stopped and removed, which is what
    keeps the device from accumulating every service it ever ran.
    """
    script = services.recovery_script(declarations())

    assert '[ -e "$UNITS/$unit" ] || continue' in script
    # A tree, not a file: a service is ready when there is something to boot.
    assert f'[ -d "{container.ROOT_DIR}/${{machine%.service}}" ] || continue' in script
    assert f'"$SYSTEMD/{container.UNIT_PREFIX}"*.service' in script
    assert 'systemctl disable --now' in script
    # The routing daemon reads outside /data, so the script puts it back there
    # too: after an update that is the only thing that will.
    assert services.FRR_LIVE_CONFIG in script


def test_a_resolvers_own_configuration_is_installed_once_and_then_left_alone() -> None:
    """The instance rewrites the file the moment the `dns` stack adds a rewrite.

    So the initial state is delivered under a name the instance does not read,
    and the script copies it into the working directory only when nothing is
    there — which is the state of a service the device has never run, and never
    the state of one that has been running. It is not in the stamped set
    either: a change to it can never be a reason to restart an instance that
    has already made the file its own.
    """
    alice = declared_for('adguard-alice')
    initial = alice.initial_state
    assert initial is not None
    assert initial.name == container.ADGUARD_INITIAL_STATE != 'AdGuardHome.yaml'
    assert declared_for('caddy').initial_state is None, 'caddy owns nothing it is given'

    delivered = container.config_path('adguard-alice', initial.name)
    assert delivered not in alice.stamped_set

    script = services.recovery_script(declarations())
    assert delivered in script
    assert f'{container.STATE_DIR}/adguard-alice/AdGuardHome.yaml' in script
    assert 'if [ -e "$destination" ]; then\n        return 0' in script


def test_a_resolver_is_bound_at_the_working_directory_its_image_is_started_with() -> None:
    """The image decides this, not the declaration: the census follows the layout.

    The resolver runs from an installation the root filesystem carries and
    keeps its configuration, query log and statistics in a working directory
    outside it. Binding the service's state anywhere else would give the
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

    unit = container.unit_file(alice)
    assert f'--bind={container.state_path("adguard-alice")}:{container.ADGUARD_STATE}' in unit


##
## The declaration
##


def test_every_file_runs_the_recovery_script_as_its_hook(monitor: Recorder) -> None:
    """The recovery path and the deployment path are the same path.

    A separate apply command would be a second way of doing the same thing, and
    the one that only runs after a firmware update is the one that would rot
    unnoticed. The routing configuration is the single exception: it answers to
    a daemon rather than to the script, so it applies itself.
    """
    files = [
        declaration
        for declaration in monitor.declared
        if declaration.name.startswith(f'{NAME}-')
        and declaration.name not in {f'{NAME}-recovery', f'{NAME}-routing'}
        and 'hook' in declaration.inputs
    ]
    assert files, 'the component declared something'
    for declaration in files:
        assert declaration.inputs['hook'] == services.RECOVERY_HOOK, declaration.name

    recovery = monitor.inputs_of(f'{NAME}-recovery')
    assert recovery['hook'] == services.RECOVERY_HOOK
    assert recovery['mode'] == services.SCRIPT_MODE
    assert recovery['path'] == services.RECOVERY_SCRIPT
    assert monitor.inputs_of(f'{NAME}-routing')['hook'] == services.FRR_APPLY


def test_a_root_filesystem_travels_as_a_pin_and_never_as_bytes(monitor: Recorder) -> None:
    """State carries the digest; the runner carries the payload, briefly.

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


def test_each_root_filesystem_owns_the_tree_its_unit_boots(monitor: Recorder) -> None:
    """A pin that moves replaces the tree, not just the tarball beside it.

    An artifact that unpacked somewhere its unit does not read would leave a
    downloaded tarball nobody boots, and the container would go on running the
    filesystem it already had.
    """
    for service in SERVICES:
        inputs = monitor.inputs_of(f'{NAME}-{service}-image')

        assert inputs['target'] == container.image_path(service)
        assert inputs['extract'] == container.root_path(service)


@pytest.mark.asyncio
async def test_the_device_secrets_are_declared_secret(monitor: Recorder) -> None:
    """Two files carry credentials, and neither may render in a preview.

    The routing configuration holds the session password and caddy's token file
    is the credential itself. Everything else is configuration one wants to
    read in a diff, which is why content is not secret by default.
    """
    token = monitor.inputs_of(f'{NAME}-caddy-file-cloudflare.token')
    assert token['content'] == ACME_TOKEN
    assert token['mode'] == container.SECRET_MODE

    routing = monitor.inputs_of(f'{NAME}-routing')
    assert f'password {BGP_PASSWORD}' in routing['content']
    assert routing['path'] == services.FRR_CONFIG
