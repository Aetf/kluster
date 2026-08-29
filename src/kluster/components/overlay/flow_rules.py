"""The overlay's rule program: what a member may do once it is admitted.

Two of the overlay's members are continuous-integration identities, and the
rules confine each of them to exactly the four destinations its work needs, so
that a leaked join credential does not buy general access to the LAN. That
confinement is not a fact about ZeroTier — it is a fact about how a run reaches
this site — which is why the rules are composed by a function the caller hands
the destinations to rather than inside `Overlay` (rfc-002 §6).

**The rules are written in positive matches only.** The engine evaluates every
packet independently at both ends and keeps no connection state, so each
allowed flow is declared twice — once outbound, once as its own return leg —
and a negated matcher is avoided because negation over an unknown tag or an
absent address family inverts missing information rather than the intended
condition (ZeroTierOne #2200). The stock base filter is the one exception; it
predates the quirk and is left as the engine ships it.

The whole program text lives here, the parts that belong to ZeroTier itself
included: the tag declaration, the stock base filter, the final accept.
Splitting one program in one language across two modules would cost more than
it buys.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import final

from kluster import conventions
from kluster.lib import templates

__all__ = ('SSH_PORT', 'UNIFI_API_PORT', 'flow_rules', 'roles')

#: The package `importlib.resources` resolves this module's `templates/`
#: directory against, so the rules program travels with the code that renders
#: it (rfc-002 §9.1).
_PACKAGE = 'kluster.components.overlay'

#: The gateway's own management ports, as reached over the overlay: the shell
#: the desired-state push writes through, and the controller API the firewall
#: resources call. Both terminate on the gateway itself.
SSH_PORT = 22
UNIFI_API_PORT = 443


def roles() -> Mapping[str, int]:
    """The role enumeration as the rules language spells it."""
    return {role.name.lower(): role.value for role in conventions.overlay.Role}


@final
@dataclass(frozen=True)
class _Target:
    """One destination continuous integration may reach, and why it may."""

    destination: str
    port: int
    why: str


@final
@dataclass(frozen=True)
class _FlowRulesParams:
    """What `flow-rules.zt.j2` reads."""

    cluster: str
    tag_role_id: int
    role_personal: int
    role_ci: int
    roles: Mapping[str, int]
    targets: tuple[_Target, ...]


def flow_rules(
    *,
    gateway_overlay_address: IPv4Address,
    homelab_overlay_address: IPv4Address,
    resolver_site_addresses: Sequence[IPv4Address],
) -> str:
    """The network's rules: a base filter, the confinement, and a fallthrough.

    The two kinds of address in the signature are the point of it. The gateway
    and the homelab host are members, so a run reaches each at its overlay
    address; the resolvers are not, so they are named by the site addresses
    their packets carry (§6.1).

    The final `accept` is what leaves personal devices with the reachability
    they would have sitting on the LAN, local discovery included: every rule
    above it matches a tagged continuous-integration endpoint and nothing else.
    """
    return templates.render(
        _PACKAGE,
        'templates/flow-rules.zt.j2',
        _FlowRulesParams(
            cluster=conventions.CLUSTER_NAME,
            tag_role_id=conventions.overlay.TAG_ROLE_ID,
            role_personal=conventions.overlay.Role.PERSONAL,
            role_ci=conventions.overlay.Role.CI,
            roles=roles(),
            targets=(
                _Target(
                    f'{gateway_overlay_address}/32',
                    SSH_PORT,
                    'the gateway, for the desired-state push',
                ),
                _Target(
                    f'{gateway_overlay_address}/32',
                    UNIFI_API_PORT,
                    'the controller API on the gateway, for the firewall resources',
                ),
                *(
                    _Target(
                        f'{address}/32',
                        conventions.gateway.ADGUARD_API_PORT,
                        'a resolver, for the split-horizon rewrites',
                    )
                    for address in resolver_site_addresses
                ),
                _Target(
                    f'{homelab_overlay_address}/32',
                    SSH_PORT,
                    'the homelab host, for the libvirt session',
                ),
            ),
        ),
    )
