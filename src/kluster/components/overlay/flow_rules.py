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

**Wherever a run is at either end, a member speaks only for the addresses
assigned to it.** A run carries a credential the gateway accepts, so a member
that could claim the gateway's overlay address to a run — answer its ARP, or
reply from that address — would receive what the run sends there, the
controller's API key among it (`components/gateway/unifi.py`'s
`ALLOW_INSECURE`). The engine's `chr ipauth` is that check: it holds when the
packet's sender address — an IPv4 source, or an ARP sender — is covered by the
certificate of ownership the network controller issues each member for the
addresses assigned to it, and at the receiving end it is judged against the
sender's certificate rather than the sender's word. A missing certificate
drops where a missing tag does not: a run drops the gateway's ARP until the
gateway's certificate has arrived. The gateway pushes it, with its tags, ahead
of its first frame to the run, so the gap is bounded and the run's next ARP
retry recovers. Routed traffic is the reason the check is
scoped rather than network-wide: a reply the gateway forwards from a LAN host
carries an address assigned to no member, so the check goes wherever the sender
speaks from an address of its own — ARP, a run's outbound legs, and the return
legs from the gateway and the homelab host — and never on a routed reply.
Traffic with no run at either end keeps the fallthrough's LAN parity, spoofing
included, as a LAN has it — at ZeroTier 1.14 or later on every member, where a
sender that does not yet know its receiver's tag skips a drop keyed on that tag
rather than applying it. A broadcast ARP is filtered at its sender with no
receiver at all, so an older sender would drop every broadcast ARP whose sender
address is not its own.

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
_TEMPLATE_PACKAGE = 'kluster.components.overlay'

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
    #: Whether the destination is a member's own overlay address, so that its
    #: reply can be required to come from the member the address is assigned
    #: to. A routed destination is answered through the gateway from an address
    #: assigned to no member, and is not.
    member: bool


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
    they would have sitting on the LAN, local discovery included: every drop
    above it names a tagged continuous-integration endpoint, and the one accept
    that names none passes only ARP whose sender holds its address; other ARP
    reaches `accept ethertype arp`.
    """
    return templates.render(
        _TEMPLATE_PACKAGE,
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
                    member=True,
                ),
                _Target(
                    f'{gateway_overlay_address}/32',
                    UNIFI_API_PORT,
                    'the controller API on the gateway, for the firewall resources',
                    member=True,
                ),
                *(
                    _Target(
                        f'{address}/32',
                        conventions.gateway.ADGUARD_API_PORT,
                        'a resolver, for the split-horizon rewrites',
                        member=False,
                    )
                    for address in resolver_site_addresses
                ),
                _Target(
                    f'{homelab_overlay_address}/32',
                    SSH_PORT,
                    'the homelab host, for the libvirt session',
                    member=True,
                ),
            ),
        ),
    )
