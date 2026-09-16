"""The ZeroTier network the gateway routes for: membership, routes, flow rules, managed DNS.

The overlay is how the home site is reached from anywhere that is not the home
site — by a person on a phone, and by a continuous-integration job that has to
configure the gateway itself. It is a network several machines belong to rather
than a fact about any one of them, so it is declared beside the gateway and not
under it (rfc-002 §6). The gateway is one member, and the two components meet
only in `conventions`, where the roster says which address the gateway answers
at.

Four things are declared here, and each answers a different question
(gateway.md §2):

-   **Membership** — who is authorized, at what address, carrying which role
    tag. Membership is the authentication boundary for traffic the gateway's
    own firewall never classifies, so it is not bookkeeping: it is the
    admission decision. *Who* may be on the network is not decided here: the
    roster arrives as a parameter, and it lives in `conventions.overlay.ROSTER`
    because the `dns` stack publishes the `*.zt` host block from the same table.
    This module is what turns that decision into authorized members.
-   **The managed routes** — the whole route table the network carries, and
    it arrives as a parameter, from `conventions.overlay.MANAGED_ROUTES`. A
    route is `{target, via}` on the network and nothing more: `via` names a
    member, and that member forwards only because forwarding is configured on
    the device itself. That is why the gateway's routing configuration is
    `SiteRouting`'s file on the box and only the route table is here — two
    systems being told two different things (gateway.md §2.2). The table is
    written as declared, `via` where the census has one and absent where it
    has none: the home subnets go via the gateway's own member, the one
    machine that was already routing them, and the overlay's own subnet goes
    via nothing, because that route is what the controller assigns every
    member's address under. The network is adopted with its routes declared
    in full, so a route the census lacks is one the update deletes at
    Central.
-   **The flow rules** — what a member may do once admitted. They arrive as a
    parameter, because what confines a run is a fact about how continuous
    integration reaches this site rather than about the network, and this
    component declares no policy (`flow_rules.py`).
-   **The managed DNS** — the one domain the network asks its members to
    resolve at home, and the resolvers that answer for it. It arrives as a
    parameter, from `conventions.overlay.MANAGED_DNS`, because the `dns` stack
    answers under the same domain and the two must agree. It is a field of the
    network like the routes: the controller hands it to every member, and each
    member applies it only under its own `allowDNS` setting, which is off by
    default and which nothing here can set (gateway.md §2.7).

**The roster is the whole of admission.** A member is authorized because it has
an entry, and the entry carries the node id that says which device it is — so
there is nothing to cross-check and no way for the role tag's permissive default
to reach anything undeclared. The gateway is the one member the roster can be
missing, because a ZeroTier identity is minted by the daemon's first run and
that daemon is a container the gateway delivers; while the entry is absent no
member is declared for it, and the ceremony that reads the minted id adds the
entry as a commit (physical/gateway.md §2.5).

**Two continuous-integration identities, not one.** ZeroTier maps a node to one
endpoint at a time, so an identity live in two jobs at once flaps; there is one
identity per stack that joins — the one that configures the physical layer and
the one that writes the resolvers' rewrites — and each is serialized by a
job-level concurrency group named after it (gateway.md §2.6). Their key material
is generated in state rather than pre-authorized by hand, which is what keeps
the network-administration token out of every environment that joins.

**The overlay is IPv4-only.** No member carries a v6 assignment and no v6 route
is managed, which is what makes the confinement rules complete: a continuous-
integration member with a v6 address would have its own neighbour discovery
eaten by its own drop rule, and there is nothing on the overlay for it to reach
over v6 that it cannot reach over v4.
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi
import pulumi_zerotier as zerotier

from kluster import conventions
from putils import Component, own_provider_opts, with_provider

__all__ = ('API_TOKEN', 'MULTICAST_LIMIT', 'Overlay')

#: The largest number of recipients a multicast or broadcast reaches. It has to
#: be at least the size of the roster or local discovery quietly stops finding
#: the last members to answer, which is a roster invariant the suite holds.
MULTICAST_LIMIT = 32

#: Where the network-administration token is read: at the line that builds the
#: provider and nowhere else (rfc-002 §8.1). It reaches no component signature,
#: this one's included.
API_TOKEN = 'zerotierApiToken'


class Overlay(Component):
    """The overlay's configuration and its whole membership.

    `network_id` is a plain value rather than an input because it is what the
    network is adopted by, and an adoption cannot wait on a computation.
    `flow_rules` is the rule program the network carries, composed by the
    caller: what a member may do once admitted is not this component's
    decision (rfc-002 §6).

    `roster` and `managed_routes` are the censuses this component turns into
    resources, and both arrive from the caller: a component receives the census
    it acts on rather than reading one for itself. They live in `conventions`
    rather than in the stack program because the `dns` stack publishes the
    `*.zt` host block from the same roster. `dns` arrives the same way and for
    the same reason: the domain it pushes is the one that block is published
    under, and the servers are the site's resolvers, both decided in
    `conventions` and read by more than this program.
    """

    def __init__(
        self,
        name: str,
        *,
        network_id: str,
        flow_rules: str,
        roster: Sequence[conventions.overlay.RosterEntry],
        managed_routes: Sequence[conventions.overlay.ManagedRoute],
        dns: conventions.overlay.ManagedDns,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # A provider of its own: this token administers the whole ZeroTier
        # account, Central minting nothing smaller, so the resources it may
        # reach are exactly the ones below — and it is read here, at the line
        # that builds the provider, rather than threaded in from above. What
        # keeps it out of state in the clear is `require_secret`, which returns
        # a secret output; a second wrapping here would read as the mechanism
        # without being it.
        #
        # Built before the component registers, because a provider reaches a
        # subtree through the options the component is registered with.
        provider = zerotier.Provider(
            f'{name}-zerotier',
            zerotier_central_token=pulumi.Config().require_secret(API_TOKEN),
            opts=own_provider_opts(opts),
        )
        super().__init__(name, opts=with_provider(opts, provider))
        self.provider = provider

        child = self.child_opts()

        # The network predates this program and is addressed by id, so the
        # first deployment adopts it rather than creating a second one that
        # nobody is a member of. Adopted is declared in full: the engine reads
        # the network into state at Central's values and plans an ordinary
        # update from there to this declaration, so every field below is
        # what Central holds after the first run, and nothing is ignored.
        #
        # Protected, because a replace is a delete: a replaced network is a
        # new id every member has to re-join, and a `destroy` of this stack
        # must never take the overlay with it. While `import_` is declared
        # the engine refuses a replace on its own; `protect` is what still
        # refuses it, and the delete, once that declaration is gone.
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
            # The whole table, as the census states it: `via` where it names
            # a member, and no `via` at all for the overlay's own subnet. The
            # controller hands the whole table to every member as it joins,
            # so this is what the whole network learns, not what any one
            # member asked for.
            routes=[
                zerotier.NetworkRouteArgs(target=str(route.target), via=None if route.via is None else str(route.via))
                for route in managed_routes
            ],
            flow_rules=flow_rules,
            # One element, declared as one object: the provider folds every
            # element of this list into the network's single DNS setting, so a
            # second element would overwrite the first rather than add a
            # domain. Like the routes, this reaches every member; unlike them
            # it is inert until the member's own `allowDNS` is on.
            dns=[zerotier.NetworkDnArgs(domain=dns.domain, servers=[str(server) for server in dns.servers])],
            opts=pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(import_=network_id, protect=True)),
        )

        # Generated identities first: a member cannot be declared before the
        # node it authorizes has an identifier.
        self.identities = {
            entry.name: zerotier.Identity(f'{name}-identity-{entry.name}', opts=child)
            for entry in roster
            if isinstance(entry, conventions.overlay.GeneratedMember)
        }

        # One member per entry, and no entry is skipped: a device with no
        # identity to authorize has no entry either, which is the state the
        # gateway is in until the ceremony records the id its daemon minted.
        self.members = {entry.name: self._declare(name, entry, child) for entry in roster}

        self.register_outputs({})

    def _declare(
        self,
        name: str,
        entry: conventions.overlay.RosterEntry,
        opts: pulumi.ResourceOptions,
    ) -> zerotier.Member:
        node_id: pulumi.Input[str] = (
            self.identities[entry.name].identity_id
            if isinstance(entry, conventions.overlay.GeneratedMember)
            else entry.node_id
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
            tags=[[conventions.overlay.TAG_ROLE_ID, entry.role]],
            opts=opts,
        )
