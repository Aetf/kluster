"""The ZeroTier network the gateway routes for: membership, routes, flow rules.

The overlay is how the home site is reached from anywhere that is not the home
site — by a person on a phone, and by a continuous-integration job that has to
configure the gateway itself. Three things are declared here, and each answers a
different question (gateway.md §2):

-   **Membership** — who is authorized, at what address, carrying which role
    tag. Membership is the authentication boundary for traffic the gateway's
    own firewall never classifies, so it is not bookkeeping: it is the
    admission decision. *Who* may be on the network is not decided here — the
    roster is `conventions.ZT_ROSTER`, because the `dns` stack publishes the
    `*.zt` host block from the same table — and this module is what turns that
    decision into authorized members.
-   **The managed routes** — which of the home's subnets the overlay carries,
    all of them through the gateway's own member, which is the one machine that
    was already routing them.
-   **The flow rules** — what a member may do once admitted. Two of the members
    are continuous-integration identities, and the rules confine each of them
    to exactly the four destinations its work needs, so that a leaked join
    credential does not buy general access to the LAN.

**The roster is the whole of admission.** A member is authorized because it has
an entry, and the entry carries the node id that says which device it is — so
there is nothing to cross-check and no way for the role tag's permissive default
to reach anything undeclared. The gateway is the one member the roster can be
missing, because a ZeroTier identity is minted by the daemon's first run and
that daemon is a container this program delivers; while the entry is absent no
member is declared for it, and the ceremony that reads the minted id adds the
entry as a commit (physical/gateway.md §2.5).

**Two continuous-integration identities, not one.** ZeroTier maps a node to one
endpoint at a time, so an identity live in two jobs at once flaps; there is one
identity per stack that joins — the one that configures the physical layer and
the one that writes the resolvers' rewrites — and each is serialized by a
job-level concurrency group named after it (gateway.md §2.6). Their key material
is generated in state rather than pre-authorized by hand, which is what keeps
the network-administration token out of every environment that joins.

**The rules are written in positive matches only.** The engine evaluates every
packet independently at both ends and keeps no connection state, so each allowed
flow is declared twice — once outbound, once as its own return leg — and a
negated matcher is avoided because negation over an unknown tag or an absent
address family inverts missing information rather than the intended condition
(ZeroTierOne #2200). The stock base filter is the one exception; it predates the
quirk and is left as the engine ships it.

**The overlay is IPv4-only.** No member carries a v6 assignment and no v6 route
is managed, which is what makes the confinement rules complete: a continuous-
integration member with a v6 address would have its own neighbour discovery
eaten by its own drop rule, and there is nothing on the overlay for it to reach
over v6 that it cannot reach over v4.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import final

import pulumi
import pulumi_zerotier as zerotier

from kluster import conventions
from kluster.lib import templates
from putils import Component, own_provider_opts, with_provider

__all__ = (
    'API_TOKEN',
    'MULTICAST_LIMIT',
    'SSH_PORT',
    'UNIFI_API_PORT',
    'Network',
    'flow_rules',
    'roles',
)

#: The package `importlib.resources` resolves this module's `templates/`
#: directory against, so the rules program travels with the code that renders
#: it (rfc-002 §9.1).
_PACKAGE = 'kluster.components.overlay'

#: The gateway's own management ports, as reached over the overlay: the shell
#: the desired-state push writes through, and the controller API the firewall
#: resources call. Both terminate on the gateway itself.
SSH_PORT = 22
UNIFI_API_PORT = 443

#: The largest number of recipients a multicast or broadcast reaches. It has to
#: be at least the size of the roster or local discovery quietly stops finding
#: the last members to answer, which is a roster invariant the suite holds.
MULTICAST_LIMIT = 32

#: Where the network-administration token is read: at the line that builds the
#: provider and nowhere else (rfc-002 §8.1). It reaches no component signature,
#: this one's included.
API_TOKEN = 'zerotierApiToken'


def roles() -> Mapping[str, int]:
    """The role enumeration as the rules language spells it."""
    return {
        'personal': conventions.ZT_ROLE_PERSONAL,
        'infra': conventions.ZT_ROLE_INFRA,
        'ci': conventions.ZT_ROLE_CI,
    }


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


def flow_rules(*, udm: IPv4Address, homelab: IPv4Address, adguard: Sequence[IPv4Address]) -> str:
    """The network's rules: a base filter, the confinement, and a fallthrough.

    `homelab` is the homelab host's own overlay address rather than a LAN one:
    the libvirt session a run opens reaches it member to member, needing no
    managed route. The resolvers, by contrast, are named by their LAN addresses,
    because traffic to them is routed by the gateway and a routed packet still
    carries the destination it had before the forward.

    The final `accept` is what leaves personal devices with the reachability
    they would have sitting on the LAN, local discovery included: every rule
    above it matches a tagged continuous-integration endpoint and nothing else.
    """
    return templates.render(
        _PACKAGE,
        'templates/flow-rules.zt.j2',
        _FlowRulesParams(
            cluster=conventions.CLUSTER_NAME,
            tag_role_id=conventions.ZT_TAG_ROLE_ID,
            role_personal=conventions.ZT_ROLE_PERSONAL,
            role_ci=conventions.ZT_ROLE_CI,
            roles=roles(),
            targets=(
                _Target(f'{udm}/32', SSH_PORT, 'the gateway, for the desired-state push'),
                _Target(
                    f'{udm}/32',
                    UNIFI_API_PORT,
                    'the controller API on the gateway, for the firewall resources',
                ),
                *(
                    _Target(
                        f'{address}/32',
                        conventions.ADGUARD_API_PORT,
                        'a resolver, for the split-horizon rewrites',
                    )
                    for address in adguard
                ),
                _Target(f'{homelab}/32', SSH_PORT, 'the homelab host, for the libvirt session'),
            ),
        ),
    )


class Network(Component):
    """The overlay's configuration and its whole membership."""

    def __init__(
        self,
        name: str,
        *,
        network_id: str,
        adguard: Sequence[IPv4Address],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # A provider of its own: this token administers the whole ZeroTier
        # account, Central minting nothing smaller, so the resources it may
        # reach are exactly the ones below — and it is read here, at the line
        # that builds the provider, rather than threaded in from above. It is
        # marked secret here rather than left to the bridged SDK, which --
        # unlike the controller's -- does not classify its own credential, and
        # an unclassified provider setting is written to state in plain text.
        #
        # Built before the component registers, because a provider reaches a
        # subtree through the options the component is registered with.
        provider = zerotier.Provider(
            f'{name}-zerotier',
            zerotier_central_token=pulumi.Output.secret(pulumi.Config().require_secret(API_TOKEN)),
            opts=own_provider_opts(opts),
        )
        super().__init__(name, opts=with_provider(opts, provider))
        self.provider = provider
        homelab = conventions.zt_member(conventions.ZT_MEMBER_HOMELAB).address

        child = self.child_opts()

        # The network predates this program and is addressed by id, so the
        # first deployment adopts it rather than creating a second one that
        # nobody is a member of.
        self.network = zerotier.Network(
            f'{name}-network',
            name=conventions.CLUSTER_NAME,
            description=f'Home overlay for {conventions.CLUSTER_NAME}; managed by the physical stack.',
            private=True,
            enable_broadcast=True,
            multicast_limit=MULTICAST_LIMIT,
            # Every address on this network is one the roster assigned. Both
            # derived IPv6 schemes are off, which is what keeps the overlay
            # single-family and the confinement rules complete.
            assign_ipv4s=[zerotier.NetworkAssignIpv4Args(zerotier=True)],
            assign_ipv6s=[zerotier.NetworkAssignIpv6Args(zerotier=False, rfc4193=False, sixplane=False)],
            routes=[
                zerotier.NetworkRouteArgs(target=str(route), via=str(conventions.ZT_UDM))
                for route in conventions.ZT_MANAGED_ROUTES
            ],
            flow_rules=flow_rules(udm=conventions.ZT_UDM, homelab=homelab, adguard=adguard),
            opts=pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(import_=network_id)),
        )

        # Generated identities first: a member cannot be declared before the
        # node it authorizes has an identifier.
        self.identities = {
            entry.name: zerotier.Identity(f'{name}-identity-{entry.name}', opts=child)
            for entry in conventions.ZT_ROSTER
            if isinstance(entry, conventions.GeneratedMember)
        }

        # One member per entry, and no entry is skipped: a device with no
        # identity to authorize has no entry either, which is the state the
        # gateway is in until the ceremony records the id its daemon minted.
        self.members = {entry.name: self._declare(name, entry, child) for entry in conventions.ZT_ROSTER}

        self.register_outputs({})

    def _declare(
        self,
        name: str,
        entry: conventions.RosterEntry,
        opts: pulumi.ResourceOptions,
    ) -> zerotier.Member:
        node_id: pulumi.Input[str] = (
            self.identities[entry.name].identity_id if isinstance(entry, conventions.GeneratedMember) else entry.node_id
        )
        return zerotier.Member(
            f'{name}-member-{entry.name}',
            network_id=self.network.network_id,
            member_id=node_id,
            name=entry.name,
            description=entry.note or f'{entry.name}, on the {conventions.CLUSTER_NAME} overlay',
            authorized=True,
            hidden=False,
            # The address is the roster's, not the pool's. A member that drew
            # from the pool would move, and the rules and the records that name
            # it would not move with it.
            no_auto_assign_ips=True,
            ip_assignments=[str(entry.address)],
            tags=[[conventions.ZT_TAG_ROLE_ID, entry.role]],
            opts=opts,
        )
