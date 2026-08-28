"""The gateway's desired state, asserted against Pulumi's mock provider.

Nothing here contacts a device. What is exercised is the part of the estate that
a diff cannot show a reviewer: which file's change makes which container
restart, which file carries a credential, what the boot-time script does when it
finds a device that has nothing on it, and what the routing configuration says
about a peer that misbehaves.

The renderers are plain functions over plain data, so most of the suite reads
their output directly; the component is declared once against mocks, which is
where a wiring mistake would surface.
"""

from __future__ import annotations

import asyncio
from ipaddress import IPv4Address
from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

from kluster import conventions, gateway
from kluster.gateway import estate

NAME = 'kluster'
HOST = str(conventions.ZT_UDM)
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample'
PRIVATE_KEY = '-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n-----END OPENSSH PRIVATE KEY-----\n'
BGP_PASSWORD = 'a-session-password'
ACME_TOKEN = 'a-zone-scoped-token'

DIGEST = 'f' * 64
ADDRESSES = {
    'caddy': IPv4Address('10.0.5.10'),
    'adguard-alice': IPv4Address('10.0.5.11'),
    'adguard-bob': IPv4Address('10.0.5.12'),
}
ROOTFS = {
    name: estate.Rootfs(url=f'https://example.invalid/{name}.tar.zst', sha256=DIGEST) for name in conventions.GW_ESTATE
}

#: Every resource the declaration fixture registered: type, name, inputs.
declared: list[tuple[str, str, dict[str, Any]]] = []


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        declared.append((args.typ, args.name, outputs))
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        return {}, []


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> None:
    """Declare the estate once, through the seam the `physical` stack calls.

    A declaration schedules a registration task on the module's own event loop,
    and only the tasks this module added may be awaited: the others belong to
    loops that other suites already closed.
    """
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='physical', preview=False)

    before = asyncio.all_tasks()
    gateway.declare_estate(
        NAME,
        host=HOST,
        host_key=HOST_KEY,
        private_key=PRIVATE_KEY,
        bgp_neighbour=conventions.HOMELAB_NODE_IPV4,
        bgp_password=BGP_PASSWORD,
        acme_token=ACME_TOKEN,
        rootfs=ROOTFS,
        addresses=ADDRESSES,
    )
    pending = asyncio.all_tasks() - before - {asyncio.current_task()}
    _ = await asyncio.gather(*pending)
    await wait_for_rpcs(await_all_outstanding_tasks=False)


def registered(name: str) -> dict[str, Any]:
    return next(inputs for _, declared_name, inputs in declared if declared_name == name)


def census() -> tuple[estate.Container, ...]:
    return estate.census(rootfs=ROOTFS, addresses=ADDRESSES, acme_token=ACME_TOKEN)


##
## The census
##


def test_the_estate_is_the_four_members_the_design_names() -> None:
    """The estate is a closed set, and the closure is checked both ways.

    A member with no image pinned would be a container that never starts; an
    image pinned for a member nobody declared would be a payload pushed to the
    device for no reason at all, which is how an estate accumulates history.
    """
    assert tuple(sorted(container.name for container in census())) == tuple(sorted(conventions.GW_ESTATE))

    with pytest.raises(ValueError, match='no image pinned for zerotier'):
        estate.census(
            rootfs={name: pin for name, pin in ROOTFS.items() if name != 'zerotier'},
            addresses=ADDRESSES,
            acme_token=ACME_TOKEN,
        )
    with pytest.raises(ValueError, match='thermostat is not a member of the estate'):
        estate.census(
            rootfs={**ROOTFS, 'thermostat': estate.Rootfs(url='https://example.invalid/x', sha256=DIGEST)},
            addresses=ADDRESSES,
            acme_token=ACME_TOKEN,
        )
    with pytest.raises(ValueError, match='no address for adguard-bob'):
        estate.census(
            rootfs=ROOTFS,
            addresses={name: address for name, address in ADDRESSES.items() if name != 'adguard-bob'},
            acme_token=ACME_TOKEN,
        )


def test_only_the_overlay_member_runs_in_the_hosts_network_namespace() -> None:
    """Host networking is what makes the gateway able to route the overlay.

    The interface has to land in the namespace the routing table lives in. The
    other three are bridged onto the container VLAN with addresses of their
    own, because the resolvers are what the LAN's leases point at.
    """
    by_name = {container.name: container for container in census()}

    assert by_name['zerotier'].host_network
    assert by_name['zerotier'].devices == (estate.TUN_DEVICE,)
    assert by_name['zerotier'].state == estate.ZEROTIER_STATE
    for name in ('caddy', 'adguard-alice', 'adguard-bob'):
        assert not by_name[name].host_network
        assert by_name[name].address == ADDRESSES[name]


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
    rendered = estate.frr_config(neighbour=conventions.HOMELAB_NODE_IPV4, password=BGP_PASSWORD)
    peer = str(conventions.HOMELAB_NODE_IPV4)

    assert f'neighbor {peer} remote-as {conventions.CLUSTER_ASN}' in rendered
    assert f'router bgp {conventions.UDM_ASN}' in rendered
    assert f'neighbor {peer} password {BGP_PASSWORD}' in rendered
    assert f'neighbor {peer} maximum-prefix {estate.MAX_PREFIXES}' in rendered
    assert f'permit {conventions.LAN_POOL_V4} le 32' in rendered
    assert f'permit {conventions.LAN_POOL_V6} le 128' in rendered
    assert rendered.count('deny any') == 2, 'each family admits the pool and refuses the rest'
    # Both families ride the one session, so both have to be activated on it.
    assert rendered.count(f'neighbor {peer} activate') == 2


def test_a_bridged_container_is_placed_and_the_overlay_member_is_not() -> None:
    """The unit is where host networking is the absence of an argument.

    `systemd-nspawn` shares the host's namespace unless a network is asked
    for, so the overlay member is described by having no address rather than by
    a flag — and a bridge argument accidentally added to it would quietly break
    every route the design puts through it.
    """
    by_name = {container.name: container for container in census()}
    bridged = estate.unit_file(by_name['adguard-alice'])
    overlay = estate.unit_file(by_name['zerotier'])

    assert f'--network-bridge={estate.CONTAINER_BRIDGE}' in bridged
    assert '--network-bridge' not in overlay
    assert f'--bind={estate.TUN_DEVICE}' in overlay
    # A bind is not access: the unit's own device policy has to admit it too.
    assert f'DeviceAllow={estate.TUN_DEVICE} rw' in overlay
    # And naming a device is what narrows that policy to a list, so the console
    # the container needs anyway has to be on it — a pseudo-terminal, which the
    # set every service gets does not include.
    assert 'DeviceAllow=char-pts rw' in overlay
    assert 'DeviceAllow' not in bridged, 'a member that names no device keeps the open policy'
    assert f'--bind={estate.state_path("zerotier")}:{estate.ZEROTIER_STATE}' in overlay
    assert f'--directory={estate.root_path("zerotier")}' in overlay


def test_a_member_is_addressed_through_the_environment_its_image_reads() -> None:
    """The images run s6, and a resolver configures its interface itself.

    It reads the addressing out of PID 1's environment, which the unit fills
    with `--setenv` — not out of a drop-in for a network manager the image does
    not carry. A file delivered to such a path is one nobody opens, so the
    resolvers would come up with no address at all and the LAN would lose the
    name service that every lease points at.
    """
    by_name = {container.name: container for container in census()}
    unit = estate.unit_file(by_name['adguard-alice'])

    for name, value in estate.net_setup_environment(ADDRESSES['adguard-alice']).items():
        assert f'--setenv={name}={value}' in unit
    # The unit's own `Environment=` is the environment of the process on the
    # device, which is not the one the image reads.
    assert 'Environment=' not in unit

    targets = [dropin.target for container in census() for dropin in container.files]
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
    question — the overlay member resolves through public servers because it
    has to come up while this estate's own resolvers are down — so nothing
    overwrites it.
    """
    for container in census():
        unit = estate.unit_file(container)

        assert '--notify-ready=no' in unit
        assert 'Type=notify' in unit
        assert f'--kill-signal={estate.KILL_SIGNAL}' in unit
        assert f'--setenv=S6_KILL_GRACETIME={estate.S6_KILL_GRACETIME}' in unit
        assert '--capability=CAP_NET_ADMIN' in unit
        assert '--resolv-conf=off' in unit


def test_caddy_is_told_where_to_read_its_configuration_and_where_to_keep_what_it_buys() -> None:
    """Both are environment variables, so the estate names them or it guesses.

    The server is started with `--config $XDG_CONFIG_HOME/caddy/Caddyfile`, so
    a file delivered anywhere else is not the file it reads; and it places the
    certificates it earns under `XDG_DATA_HOME`, which has to be the directory
    bind-mounted from the device or a rootfs bump would throw them away.
    """
    caddy = next(container for container in census() if container.name == 'caddy')
    unit = estate.unit_file(caddy)
    configuration = next(dropin for dropin in caddy.files if dropin.name == 'Caddyfile')

    assert f'--setenv=XDG_CONFIG_HOME={estate.CADDY_CONFIG_HOME}' in unit
    assert configuration.target == f'{estate.CADDY_CONFIG_HOME}/caddy/Caddyfile'
    assert f'--setenv=XDG_DATA_HOME={estate.CADDY_STATE}' in unit
    assert caddy.state == estate.CADDY_STATE
    assert f'--bind={estate.state_path("caddy")}:{estate.CADDY_STATE}' in unit
    # Its address is the one thing the unit cannot deliver: the image asks for
    # a lease, and the lease is where its resolver comes from too.
    assert estate.ENV_IPV4_CIDR not in unit


def test_a_unit_boots_the_unpacked_tree_and_not_the_tarball_that_carried_it() -> None:
    """The pins are root filesystem archives, and `--image=` cannot boot one.

    So the artifact lands as a tarball and the push unpacks it into a tree the
    unit names with `--directory=`. The two paths are distinct on purpose: the
    tarball is what the digest pins, the tree is derived state the push
    replaces, and a unit pointed at the archive would not start at all.
    """
    unit = estate.unit_file(census()[0])

    assert f'--directory={estate.root_path("caddy")}' in unit
    assert '--image=' not in unit
    assert estate.image_path('caddy').endswith('.tar')
    assert estate.root_path('caddy') != estate.image_path('caddy')


def test_a_resolver_is_placed_statically_and_points_at_more_than_one_upstream() -> None:
    """A resolver that waited for a lease would be waiting on itself.

    Its address is what the leases hand out as the name server, so it is
    configured rather than learned; and its own upstreams are two providers,
    because the LAN's name service must not stop with any one of them.
    """
    address = ADDRESSES['adguard-alice']
    environment = estate.net_setup_environment(address)

    assert environment[estate.ENV_IPV4_CIDR] == f'{address}/{conventions.VLAN_CONTAINER.prefixlen}'
    assert environment[estate.ENV_IPV4_GATEWAY] == str(next(conventions.VLAN_CONTAINER.hosts()))
    # The v6 half is an interface identifier, not an address: the prefix keeps
    # arriving in advertisements, so the resolver survives it changing.
    assert environment[estate.ENV_IPV6_TOKEN] == f'::{address}'

    seed = estate.adguard_seed(ADDRESSES['adguard-alice'])
    assert f'{ADDRESSES["adguard-alice"]}:{estate.ADGUARD_API_PORT}' in seed
    for upstream in estate.ADGUARD_UPSTREAMS:
        assert upstream in seed
    assert len(estate.ADGUARD_UPSTREAMS) > 1


def test_the_gateway_issues_its_own_certificates_from_its_own_credential() -> None:
    """Its TLS has to keep renewing while the cluster is down.

    So the vhosts answer a DNS-01 challenge with a token that lives on the
    device and nowhere else — never the cluster issuer's, which is the point of
    there being two.
    """
    rendered = estate.caddyfile(adguard={name: ADDRESSES[name] for name in estate.VHOST_ADGUARD})

    assert estate.VHOST_CONTROLLER in rendered
    assert rendered.count(f'dns cloudflare {{file.{estate.CADDY_TOKEN_PATH}}}') == 3
    for instance, name in estate.VHOST_ADGUARD.items():
        assert name in rendered
        assert f'http://{ADDRESSES[instance]}:{estate.ADGUARD_API_PORT}' in rendered
    # The console presents its own certificate to the proxy and the name that
    # matters is the one the client asked for, which Caddy forwards unchanged.
    assert 'tls_insecure_skip_verify' in rendered


##
## The recovery script
##


def test_the_member_carrying_the_session_is_converged_last() -> None:
    """Restarting the overlay member drops the session that asked for it.

    Everything else therefore converges first, so an apply that dies on that
    last restart has already done the rest of its work and the retry finds it
    done.
    """
    script = estate.on_boot_script(census())
    declared_units = next(line for line in script.splitlines() if line.startswith('DECLARED='))

    assert declared_units.rstrip('"').endswith(estate.unit_name('zerotier'))
    assert all(estate.unit_name(name) in declared_units for name in conventions.GW_ESTATE)


def test_a_container_is_restarted_only_when_something_it_reads_changed() -> None:
    """Otherwise every deployment would restart the overlay member.

    The script stamps each unit with a checksum over the files that define it —
    the unit, the digest marker beside its root filesystem tree, and every file
    bound into the container — and compares before acting. The tree is
    represented by its marker rather than by itself: walking a root filesystem
    to learn it has not changed would cost more than the restart it avoids.
    """
    script = estate.on_boot_script(census())

    assert f'{estate.root_path("caddy")}.digest' in script
    assert f'{estate.UNIT_DIR}/{estate.unit_name("caddy")}' in script
    assert estate.dropin_path('caddy', census()[0].files[1]) in script
    assert 'systemctl is-active --quiet' in script
    assert 'cksum' in script


def test_a_new_root_filesystem_is_noticed_through_the_marker_beside_the_tree() -> None:
    """The tarball's own marker is written after this script has already run.

    The artifact resource writes it last, as the claim that the whole push
    succeeded, so a stamp built from it would compare equal on the very run
    that installed the new tree and the member would keep running the old one
    until some unrelated file changed. The marker beside the tree is written
    before the hook, which is why it is the one the stamp reads.
    """
    script = estate.on_boot_script(census())

    assert f'{estate.image_path("caddy")}.digest' not in script
    assert f'{estate.root_path("caddy")}.digest' in script


def test_the_script_converges_a_device_that_has_nothing_on_it() -> None:
    """This is the firmware-update case, and the first-deployment case.

    The script is written before the files it describes, so a member whose unit
    or root filesystem tree has not landed yet is skipped rather than fatal —
    and a unit the estate no longer declares is stopped and removed, which is
    what keeps the device from accumulating every estate it ever had.
    """
    script = estate.on_boot_script(census())

    assert '[ -e "$UNITS/$unit" ] || continue' in script
    # A tree, not a file: a member is ready when there is something to boot.
    assert f'[ -d "{estate.ROOT_DIR}/${{machine%.service}}" ] || continue' in script
    assert f'"$SYSTEMD/{estate.UNIT_PREFIX}"*.service' in script
    assert 'systemctl disable --now' in script
    # The routing daemon reads outside /data, so the script puts it back there
    # too: after an update that is the only thing that will.
    assert estate.FRR_LIVE_CONFIG in script


def test_a_resolvers_own_configuration_is_seeded_and_then_left_alone() -> None:
    """The instance rewrites the file the moment the `dns` stack adds a rewrite.

    So the estate delivers a seed under a name the instance does not read, and
    the script copies it into the working directory only when nothing is there
    — which is the state of a container whose image was just replaced, and
    never the state of one that has been running.
    """
    by_name = {container.name: container for container in census()}
    seed = by_name['adguard-alice'].seed
    assert seed is not None
    assert seed.source == estate.ADGUARD_SEED != 'AdGuardHome.yaml'
    assert by_name['caddy'].seed is None, 'caddy owns nothing it is given'

    script = estate.on_boot_script(census())
    assert f'{estate.STATE_DIR}/adguard-alice/AdGuardHome.yaml' in script
    assert 'if [ -e "$destination" ]; then\n        return 0' in script


def test_a_resolver_is_bound_at_the_working_directory_its_image_is_started_with() -> None:
    """The image decides this, not the estate: the census follows the layout.

    The resolver runs from an installation the root filesystem carries and
    keeps its configuration, query log and statistics in a working directory
    outside it. Binding the member's state anywhere else would give the
    instance an empty directory to fill and leave the seed sitting where
    nothing reads it — the failure would be a resolver that came up with
    default settings rather than an error anyone sees.
    """
    by_name = {container.name: container for container in census()}
    alice = by_name['adguard-alice']
    seed = alice.seed
    assert seed is not None

    assert alice.state == estate.ADGUARD_STATE
    assert estate.ADGUARD_STATE != estate.ADGUARD_INSTALL
    # The live configuration is the working directory's, and the seed is
    # delivered read-only beside the installation so that placing one can never
    # overwrite the other.
    dropin = next(entry for entry in alice.files if entry.name == estate.ADGUARD_SEED)
    assert dropin.target == f'{estate.ADGUARD_INSTALL}/{estate.ADGUARD_SEED}'
    assert not dropin.target.startswith(f'{estate.ADGUARD_STATE}/')

    unit = estate.unit_file(alice)
    assert f'--bind={estate.state_path("adguard-alice")}:{estate.ADGUARD_STATE}' in unit


##
## The declaration
##


def test_every_file_of_the_estate_runs_the_recovery_script_as_its_hook() -> None:
    """The recovery path and the deployment path are the same path.

    A separate apply command would be a second way of doing the same thing, and
    the one that only runs after a firmware update is the one that would rot
    unnoticed. The routing configuration is the single exception: it answers to
    a daemon rather than to the estate, so it applies itself.
    """
    estate_files = [
        (name, inputs)
        for _, name, inputs in declared
        if name.startswith(f'{NAME}-') and name != f'{NAME}-on-boot' and name != f'{NAME}-frr'
    ]
    assert estate_files, 'the estate declared something'
    for name, inputs in estate_files:
        assert inputs['hook'] == estate.ON_BOOT_HOOK, name

    assert registered(f'{NAME}-on-boot')['hook'] == estate.ON_BOOT_HOOK
    assert registered(f'{NAME}-on-boot')['mode'] == estate.SCRIPT_MODE
    assert registered(f'{NAME}-frr')['hook'] == estate.FRR_APPLY


def test_a_root_filesystem_travels_as_a_pin_and_never_as_bytes() -> None:
    """State carries the digest; the runner carries the payload, briefly.

    An image in state would make every preview download megabytes to compare
    them against megabytes, and would put a container's whole filesystem into
    the deployment history.
    """
    images = [
        (name, inputs)
        for typ, name, inputs in declared
        if typ == 'pulumi-python:dynamic/gateway:Artifact'  # noqa: S105 -- a resource type, not a credential
    ]
    assert sorted(name for name, _ in images) == sorted(f'{NAME}-image-{member}' for member in conventions.GW_ESTATE)
    for name, inputs in images:
        assert inputs['sha256'] == DIGEST, name
        assert inputs['url'].endswith('.tar.zst'), name
        assert 'content' not in inputs, name

    # Every member's artifact owns the tree its unit boots, so a pin that moves
    # replaces the root filesystem rather than leaving a tarball nobody unpacks.
    for member in conventions.GW_ESTATE:
        inputs = registered(f'{NAME}-image-{member}')
        assert inputs['target'] == estate.image_path(member)
        assert inputs['extract'] == estate.root_path(member)


@pytest.mark.asyncio
async def test_the_device_secrets_are_declared_secret() -> None:
    """Two files carry credentials, and neither may render in a preview.

    The routing configuration holds the session password and caddy's token file
    is the credential itself. Everything else is configuration one wants to
    read in a diff, which is why content is not secret by default.
    """
    token = registered(f'{NAME}-file-caddy-cloudflare.token')
    assert token['content'] == ACME_TOKEN
    assert token['mode'] == estate.SECRET_MODE

    frr = registered(f'{NAME}-frr')
    assert f'password {BGP_PASSWORD}' in frr['content']
    assert frr['path'] == estate.FRR_CONFIG


def test_the_pins_and_the_addresses_are_read_from_configuration() -> None:
    """Both are site facts: what the build produced, and where the LAN expects.

    They are checked as they cross into the program, so a truncated digest is a
    named configuration error rather than a push that reaches the device and is
    refused there.
    """
    url = 'https://example.invalid/caddy.tar.zst'
    pins = estate.parse_rootfs({'caddy': {'url': url, 'sha256': DIGEST.upper()}})
    assert pins['caddy'] == estate.Rootfs(url=url, sha256=DIGEST)

    assert estate.parse_addresses({'caddy': '10.0.5.10'}) == {'caddy': IPv4Address('10.0.5.10')}

    with pytest.raises(ValueError, match='is not a hex sha256 digest'):
        estate.parse_rootfs({'caddy': {'url': url, 'sha256': 'abc'}})
    with pytest.raises(ValueError, match='carries no url'):
        estate.parse_rootfs({'caddy': {'sha256': DIGEST}})
    with pytest.raises(TypeError, match='must be a mapping'):
        estate.parse_rootfs(['caddy'])
