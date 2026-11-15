"""The gateway's routing configuration, asserted against Pulumi's mock provider.

Nothing here contacts a controller. What is exercised is what a diff cannot
show a reviewer: what the rendered configuration says about a peer that
misbehaves, and that the component hands the session's password to the site's
BGP configuration as a secret. What the configuration's resource does with the
controller is `test_unifi_bgp`'s.
"""

from __future__ import annotations

import re
from ipaddress import IPv6Address

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import routing
from kluster.components.gateway.routing import RoutingSession, SiteRouting

NAME = 'routing'
API_URL = 'https://gateway.invalid'
SITE = 'default'
BGP_PASSWORD = 'a-session-password'
#: A link-local next hop, and not the convention's: what is asserted is that
#: the file routes through the one the session carries.
LINK_LOCAL = IPv6Address('fe80::5054:ff:fe12:3456')

#: The site's BGP configuration, as the dynamic provider's token names it.
BGP_CONFIGURATION = 'pulumi-python:dynamic/unifi_bgp:BgpConfiguration'

#: How a secret arrives on the wire: Pulumi's special-signature key, carrying
#: the signature that means "secret", beside the value itself.
SECRET = {'4dabf18193072939515e22adb298388d': '1b47061264138c4ac30d75fd1eb44270'}


@pytest_asyncio.fixture(scope='module', loop_scope='module', autouse=True)
async def monitor() -> Recorder:
    """What the run registered, for the cases that read declarations directly."""
    return await run_with(Recorder(), stack='physical')


@pytest_asyncio.fixture(scope='module', loop_scope='module', autouse=True)
async def site(monitor: Recorder) -> SiteRouting:
    """The component declared once, the way `Gateway` declares it."""
    async with declaring():
        declared = SiteRouting(
            NAME,
            api_url=API_URL,
            site=SITE,
            session=RoutingSession(
                neighbor=conventions.HOMELAB_NODE_IPV4, neighbor_link_local=LINK_LOCAL, password=BGP_PASSWORD
            ),
        )
    return declared


##
## What the daemon is told
##


def test_the_routing_configuration_confines_what_the_peer_may_announce() -> None:
    """Three defenses, and each of them is a line a reviewer can find.

    Without the prefix-list, anything holding the worker's address could
    announce the resolvers' own /32s and take the LAN's name service with it;
    without the cap, it could announce the pool one address at a time until the
    table gave out; without the password, holding the address would be enough
    to be the peer.
    """
    rendered = routing.frr_config(
        neighbor=conventions.HOMELAB_NODE_IPV4, neighbor_link_local=LINK_LOCAL, password=BGP_PASSWORD
    )
    peer = str(conventions.HOMELAB_NODE_IPV4)

    assert f'neighbor {peer} remote-as {conventions.CLUSTER_ASN}' in rendered
    assert f'router bgp {conventions.UDM_ASN}' in rendered
    assert f'neighbor {peer} password {BGP_PASSWORD}' in rendered
    assert f'neighbor {peer} maximum-prefix {routing.MAX_PREFIXES}' in rendered
    assert f'permit {conventions.LAN_POOL.v4} le 32' in rendered
    assert f'permit {conventions.LAN_POOL.v6} le 128' in rendered
    assert rendered.count('deny any') == 2, 'each family admits the pool and refuses the rest'
    # Both families ride the one session, so both have to be activated on it.
    assert rendered.count(f'neighbor {peer} activate') == 2


def _family(rendered: str, family: str) -> list[str]:
    """The lines of one address family's block, stripped of their indentation."""
    lines = rendered.splitlines()
    start = lines.index(f' address-family {family} unicast')
    end = lines.index(' exit-address-family', start)
    return [line.strip() for line in lines[start + 1 : end]]


def _route_maps(rendered: str) -> dict[str, list[str]]:
    """Every route-map the file defines, by name, with the lines its `permit` entries hold.

    A `deny` entry drops what it matches and never runs its `set` lines, so its
    lines are not collected: a map whose only entry denies has none here.
    """
    maps: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in rendered.splitlines():
        if entry := re.fullmatch(r'route-map (\S+) permit \d+', line):
            current = maps.setdefault(entry[1], [])
        elif current is not None and line.startswith(' '):
            current.append(line.strip())
        else:
            current = None
    return maps


def test_the_ipv6_routes_take_the_sessions_link_local_as_their_next_hop() -> None:
    """The IPv6 family, and it alone, applies a route-map the file defines, which sets the link-local.

    The peer sends its IPv6 routes over the IPv4 session with an IPv4-mapped
    next hop, which the kernel refuses to route through; the inbound route-map
    replaces it with the peer's link-local, which the kernel accepts on the
    session's interface. On the IPv4 family it would have nothing to correct.
    """
    rendered = routing.frr_config(
        neighbor=conventions.HOMELAB_NODE_IPV4, neighbor_link_local=LINK_LOCAL, password=BGP_PASSWORD
    )
    inbound = re.compile(rf'neighbor {re.escape(str(conventions.HOMELAB_NODE_IPV4))} route-map (\S+) in')
    applied = {
        family: [found[1] for line in _family(rendered, family) if (found := inbound.fullmatch(line))]
        for family in ('ipv4', 'ipv6')
    }
    maps = _route_maps(rendered)

    assert applied['ipv4'] == []
    (name,) = applied['ipv6']
    assert name in maps, 'the route-map the family applies is one the file defines, and it permits'
    assert f'set ipv6 next-hop local {LINK_LOCAL}' in maps[name]


@pytest.mark.parametrize(
    'address',
    [
        pytest.param('::ffff:192.168.70.10', id='ipv4-mapped'),
        pytest.param('fd1a:665f:8bcb:70::10', id='unique-local'),
        pytest.param('2001:db8::10', id='global'),
    ],
)
def test_a_session_whose_next_hop_is_not_link_local_is_refused(address: str) -> None:
    """Only a link-local next hop is one the daemon installs on the session's own interface.

    The IPv4-mapped address is the one the kernel refuses outright; a routable
    one would be tracked as a next hop of its own, and would rotate or move
    with a prefix the session knows nothing about.
    """
    with pytest.raises(ValueError, match='not a link-local address'):
        _ = RoutingSession(
            neighbor=conventions.HOMELAB_NODE_IPV4, neighbor_link_local=IPv6Address(address), password=BGP_PASSWORD
        )


##
## What the controller holds
##


def test_the_site_holds_the_rendered_configuration_as_a_secret(monitor: Recorder) -> None:
    """The text the controller is given is the rendered file, password and all, and it never prints.

    Holding the peer's address is not enough to become the peer *because* the
    password is in this text, which is the same reason it is rendered into no
    diff.
    """
    (name,) = monitor.by_name(BGP_CONFIGURATION)
    inputs = monitor.inputs_of(name)
    rendered = routing.frr_config(
        neighbor=conventions.HOMELAB_NODE_IPV4, neighbor_link_local=LINK_LOCAL, password=BGP_PASSWORD
    )

    assert inputs['config'] == SECRET | {'value': rendered}
    assert 'config' in monitor.options_of(name).additionalSecretOutputs
    assert (inputs['api_url'], inputs['site']) == (API_URL, SITE)
