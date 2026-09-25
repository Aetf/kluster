"""The censuses' invariants, their derivations, and the seams that can catch them.

Every census's invariants and seam tests are here, whatever program reads the
census (style/pulumi.md). No case restates a census: a literal copied from a
value typed at its own declaration can only ever fail for whoever edits that
declaration, and goes green again the moment the copy is moved to match
(style/testing.md).

An **invariant** is a relation between the *entries* of a table -- a node that
exists, a mount claimed once, an address inside the subnet it belongs to. A
dataclass makes a pair that must agree impossible to write apart inside one
row; across rows nothing can. They are checked here because the tables are
static code: nothing can break one at runtime that this suite did not already
catch.

A **derivation** is checked against literals, because a rule that computes a
value can be rewritten with no new value typed anywhere: `KNOWN_PREFIXES` holds
the /64s the site's addressing rule has to keep producing.

A **seam** is a reader the census is not the source of -- a workflow file no
import reaches, another program's own spelling of the same decision. Those can
disagree with the census, which is what makes them able to catch it.
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import json
import re
import tokenize
import tomllib
from collections.abc import Iterable, Iterator
from ipaddress import IPv6Network
from pathlib import Path
from typing import NamedTuple, cast

import pytest
import yaml
from fences import prose
from section_numbers import sections

from kluster import conventions
from kluster.conventions import backup
from kluster.components.dns.base import overlay_records
from kluster.scripts.credentials import pulumi_config

# --------------------------------------------------------------------------
# The site, zone, gateway and cloud censuses.
# --------------------------------------------------------------------------

#: The site's /64s as they were written out by hand before the addressing rule
#: derived them. A derivation that stopped matching these would silently
#: renumber the site's firewall rules and resolver rewrites.
KNOWN_PREFIXES = {
    'server': IPv6Network('fd1a:665f:8bcb:80::/64'),
    'iot': IPv6Network('fd1a:665f:8bcb:90::/64'),
    'container': IPv6Network('fd1a:665f:8bcb:5::/64'),
    'cluster': IPv6Network('fd1a:665f:8bcb:70::/64'),
}


def test_a_networks_v6_prefix_is_numbered_after_the_third_octet_of_its_v4() -> None:
    """The site's addressing rule, applied rather than restated.

    Each /64 carries the third octet of its IPv4 subnet as the digits of its
    last group. Deriving it is what makes "a v4 subnet moved and its v6 left
    behind" — rules that match half a network — impossible to declare.
    """
    # Both directions. Iterating the census and indexing the table only ever
    # visits the networks the census still has, so *adding* one fails loudly on
    # a missing key while *deleting* one is simply never reached -- the site
    # loses a network and nothing anywhere notices.
    assert {network.name for network in conventions.SITE_NETWORKS} == set(KNOWN_PREFIXES)

    for network in conventions.SITE_NETWORKS:
        assert network.v6 == KNOWN_PREFIXES[network.name]
        assert network.v6.subnet_of(conventions.SITE_ULA)


def test_the_lan_pool_is_numbered_by_the_same_rule_as_the_networks() -> None:
    """The pool is not a network the gateway serves, but it is at the site.

    Its /64 follows the cluster VLAN's the way its IPv4 subnet follows that
    VLAN's, so the two read as neighbors without ever being one network.
    """
    assert conventions.LAN_POOL.v6 == IPv6Network('fd1a:665f:8bcb:71::/64')
    assert conventions.LAN_POOL.v6.subnet_of(conventions.SITE_ULA)


def test_every_fixed_vip_is_an_address_out_of_the_pool_that_holds_it() -> None:
    """A VIP outside its pool is an address the load balancer can never hand out."""
    for vip in (conventions.LAN_POOL.default_vip, conventions.LAN_POOL.media_vip):
        assert vip.v4 in conventions.LAN_POOL.v4
        assert vip.v6 in conventions.LAN_POOL.v6


def test_a_fixed_vips_two_families_carry_the_same_host_number() -> None:
    """`.1` and `::1` are one address wearing two families.

    The v6 half is written out in full, prefix included, where the pool's own
    /64 is derived — so this is the one place the hand-spelled prefix and the
    host number it carries can drift from the v4 sibling they are meant to
    name. A rewrite pointing at `::2` for what the firewall admits at `.1` is
    a split-horizon answer that reaches the wrong service.
    """
    for vip in (conventions.LAN_POOL.default_vip, conventions.LAN_POOL.media_vip):
        v4_host = int(vip.v4) - int(conventions.LAN_POOL.v4.network_address)
        v6_host = int(vip.v6) - int(conventions.LAN_POOL.v6.network_address)
        assert v4_host == v6_host


# The gateway census is held against the artifacts that can disagree with it,
# and restated nowhere. `test_device_services` checks the rendered units and
# files against `tests/data/live-caddyfile`, a transcript of what the
# device serves today; that one goes when the device does.
#
# What no artifact reaches -- a service's address, the build it runs, a vhost's
# label, a legacy row's retirement wave -- is not restated here either. Each is
# typed at its own declaration, so a copy would fail for nobody but the person
# editing it, and what it is coupled to is stated on the row instead.
# `BridgedService.address` states that the LAN's leases already point at it;
# `artifact` names the build as its registry repository names it, which is what
# refuses a pin published somewhere else; `LegacyVhost` states what its wave is
# coupled to; `BridgedService.vhost` states what the name is for -- a
# public-zone name public resolvers do not answer, reached by the split-horizon
# rewrite (dns.md §4) -- and not what changing one costs the clients already
# using it, which is the constraint that belongs on that row.


def test_every_bridged_service_sits_on_the_container_vlan() -> None:
    """The unit places a service by injecting this address with the VLAN's prefix.

    An address outside the subnet would be configured onto the interface and
    reach nothing, and the resolvers are what every lease on the LAN points at.
    """
    for service in conventions.gateway.SERVICES:
        if isinstance(service, conventions.gateway.BridgedService):
            assert service.address in conventions.CONTAINER_VLAN.v4


def test_every_name_the_gateway_serves_is_one_label_under_the_primary_zone() -> None:
    """The gateway holds one wildcard certificate, and a wildcard covers one label.

    So a vhost deeper than that — or in another zone — is a name its own
    Caddyfile would serve from no site block at all (rfc-002 §9.3). The census
    is where such a name would be introduced, which is where the rule belongs.

    Held symbolically, against `ZONE_PRIMARY` rather than the domain it is:
    every row today builds its name by joining a label to that constant, so a
    deliberate rename of the zone moves the rows with it and this stays green,
    while a row that spells its own name out -- which the field's type admits
    -- is a name in some other zone and reddens it.
    """
    served = [conventions.gateway.VHOST_CONTROLLER]
    served += [service.vhost for service in conventions.gateway.RESOLVERS if service.vhost is not None]

    assert len(served) == 1 + len(conventions.gateway.RESOLVERS)
    for vhost in served:
        assert vhost.partition('.')[2] == conventions.ZONE_PRIMARY, vhost


def test_every_legacy_name_is_one_label_under_the_retiring_zone() -> None:
    """The same wildcard rule, held for the census that empties instead of growing.

    A legacy row deeper than one label is a name no site block in the rendered
    file covers, and one under some other zone is a name the proxy holds no
    certificate for at all.

    `LegacyVhost.host` is the label joined to `ZONE_LEGACY`, so what this can
    catch is a label carrying a dot, and a zone rename moves the rows with it.
    The rendered names themselves are held by `test_device_services`, against
    the configuration the device serves today.
    """
    hosts = [vhost.host for vhost in conventions.gateway.LEGACY_VHOSTS]

    assert len(set(hosts)) == len(conventions.gateway.LEGACY_VHOSTS)
    for host in hosts:
        assert host.partition('.')[2] == conventions.gateway.ZONE_LEGACY, host


def test_every_service_names_a_build_the_registry_publishes() -> None:
    """One image can serve two services, and each still gets a pin of its own.

    The resolvers are the case: one `adguard` build behind both, and two
    `versions:image-` keys, which is what lets a new build be proven on one
    instance before the other (`conventions.gateway.image_pin`; the namespace
    those keys live in is framework/pulumi.md §3.2).
    """
    pins = [conventions.gateway.image_pin(service) for service in conventions.gateway.SERVICES]
    assert len(set(pins)) == len(conventions.gateway.SERVICES)

    alice, bob = conventions.gateway.RESOLVERS
    assert alice.artifact == bob.artifact
    assert conventions.gateway.CADDY.artifact != alice.artifact
    # And the artifact is what names the repository, so two instances of one
    # build pull the same image rather than two that happen to agree.
    repository = conventions.gateway.image_repository
    assert repository(alice.artifact) == repository(bob.artifact)
    assert repository(conventions.gateway.CADDY.artifact) != repository(alice.artifact)


def test_every_volume_is_attached_to_a_node_the_fleet_declares() -> None:
    """A volume attached to a node that does not exist is an apply that half works."""
    for name, volume in conventions.NODE_VOLUMES.items():
        assert volume.attached_node in conventions.CLOUD_NODES, name


def test_no_two_volumes_claim_the_same_mount() -> None:
    """Two volumes at one path is one dataset the node quietly hides."""
    mounts = [volume.mount for volume in conventions.NODE_VOLUMES.values()]
    assert sorted(mounts) == sorted(set(mounts))


def test_no_node_carries_two_volumes() -> None:
    """Checked after the sentinel resolves, which is where the pair can collide.

    Volumes are spread one per node so that the machine configuration's disk
    selection stays "the disk that is not the boot disk" and one node's loss
    takes one preserved dataset rather than two. A following volume landing on
    a node another volume already holds is exactly the state the table can
    reach by an edit somewhere else — `DEDICATED_VIP_NODE`.
    """
    nodes = [volume.attached_node for volume in conventions.NODE_VOLUMES.values()]
    assert sorted(nodes) == sorted(set(nodes))


def test_the_following_volume_is_wherever_the_dedicated_vip_is() -> None:
    """The workload's traffic must leave by the address it arrives on.

    So the volume's node is not a name that could be edited out of step with
    the VIP: it is the sentinel, and it resolves to whatever
    `DEDICATED_VIP_NODE` says today.

    Held against `DEDICATED_VIP_NODE` rather than against the node it names
    today: what this case is about is the *resolution*, and a row that named a
    node directly instead of stating the sentinel would resolve to that node
    and redden here. A literal would add nothing to that and would take a
    deliberate move of the VIP -- an edit typed at `DEDICATED_VIP_NODE`, under
    its own documentation -- and charge it a second edit here as well.
    """
    hath = conventions.NODE_VOLUMES['hath-cache']

    assert hath.node is conventions.FOLLOWS_DEDICATED_VIP
    assert hath.attached_node == conventions.DEDICATED_VIP_NODE
    assert conventions.NodeVolumeEntry(node='cp3', size_gb=1, mount='/var/mnt/elsewhere').attached_node == 'cp3'


def test_the_block_quota_admits_the_largest_volume_and_a_restore_beside_it() -> None:
    """The quota refuses at creation, so a table that outgrew it never applies.

    The guardrail is a literal of its own on purpose — an envelope this program
    is held to rather than a number derived from what it happens to declare —
    which is exactly why the two have to be compared somewhere. A volume added
    to the table without the envelope following it would not be caught by a
    preview: the refusal arrives from the tenancy, mid-apply, against this
    program's own policy.
    """
    from kluster.components.cloud.guardrails import BLOCK_STORAGE_GB_PER_AD

    largest = max(volume.size_gb for volume in conventions.NODE_VOLUMES.values())
    # One node's boot volume, the largest volume it may carry, and room to
    # restore such a volume beside the one it replaces.
    assert conventions.NODE_BOOT_VOLUME_GB + 2 * largest <= BLOCK_STORAGE_GB_PER_AD


# --------------------------------------------------------------------------
# The overlay's managed DNS.
# --------------------------------------------------------------------------
# `conventions.overlay.MANAGED_DNS` is derived from two other tables -- its
# domain from the overlay block's name, its servers from the resolver census --
# and what holds it is its relation to each, never a copy of either. That the
# `physical` program hands it to the network is held where that program runs
# (`test_physical_stack`).

#: What a member keeps of the pushed list: `ZT_MAX_DNS_SERVERS` in the
#: client's `include/ZeroTierOne.h`. A longer list is truncated on the
#: device with no error anywhere.
ZT_MAX_DNS_SERVERS = 4


def test_the_push_fits_what_a_member_keeps() -> None:
    """A list pushed past the bound reaches every member truncated, and nothing reports it."""
    assert len(conventions.overlay.MANAGED_DNS.servers) <= ZT_MAX_DNS_SERVERS


def test_the_pushed_domain_is_where_the_block_is_published() -> None:
    """Every name the block publishes is one label under the domain a member scopes to.

    Held against the `dns` program's own spelling of the block -- its labels,
    in the one zone it is declared in -- rather than against the join that
    builds the constant: a member that opts in resolves exactly the names
    under the pushed domain at home, so the domain has to be the block's
    suffix and nothing wider or narrower. The primary itself would put every
    application name behind the home resolvers for an opted-in device off-site
    (physical/gateway.md §2.7); any other zone would scope the resolver to
    names nothing publishes.
    """
    (zone,) = conventions.PRIMARY_ONLY
    published = {f'{record.label}.{zone}' for record in overlay_records()}
    assert published, 'the block is empty'

    for name in published:
        _, dot, domain = name.partition('.')
        assert dot and domain == conventions.overlay.MANAGED_DNS.domain, name
    assert conventions.overlay.MANAGED_DNS.domain == conventions.OVERLAY_DOMAIN


def test_the_pushed_servers_are_the_resolver_census_in_its_order_and_nothing_else() -> None:
    """The resolvers a member is told to use are the site's, all of them, as listed.

    Each direction is a different failure. A resolver the census carries and
    the push lacks is one no opted-in member ever tries, so that resolver's
    replaceability -- the reason there are two -- does not reach the overlay;
    an address pushed that the census does not carry is one nothing on the
    container VLAN answers at, and a member that tries it first waits on it
    for every `*.zt` name. The order is the census's own, alice before bob,
    because a member tries them in the order pushed.
    """
    assert conventions.overlay.MANAGED_DNS.servers == tuple(
        resolver.address for resolver in conventions.gateway.RESOLVERS
    )


def test_every_pushed_server_sits_behind_a_route_the_network_manages() -> None:
    """A member reaches the resolvers only through a route it was handed.

    The resolvers are containers on the gateway and not members, so an address
    of theirs the managed routes do not cover -- an overlay address, or a
    subnet the census stopped routing -- is one an opted-in member sends
    `*.zt` queries to and never reaches, with the public record it would
    otherwise have used pre-empted by the scoped resolver.
    """
    for server in conventions.overlay.MANAGED_DNS.servers:
        assert any(server in route.target for route in conventions.overlay.MANAGED_ROUTES), server


# --------------------------------------------------------------------------
# The route census.
# --------------------------------------------------------------------------
# `conventions.routes.Route` makes a row's fields correct together; what a
# row's type cannot express is a relation between rows, or between a row and
# the zones the installation declares.
#
# The census is empty until `apps` declares its first route, so each invariant
# is a predicate over a table and each case applies it twice: to
# `conventions.routes.ROUTES`, and to a row that breaks it. A case that only
# inspected `ROUTES` would pass by having nothing to inspect -- and would keep
# passing if the predicate stopped checking anything at all.

#: A DNS label's shape: letters, digits and interior hyphens, and no dot at all.
DNS_LABEL = re.compile(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?\Z')

#: A DNS label's maximum length, in octets (RFC 1035 §2.3.4), which the shape
#: above does not bound. Counted with `.encode()` because the limit is octets
#: and not characters; the names a row publishes are ASCII either way -- a host
#: by the shape check, a service label by its `_service._proto` form -- so the
#: two counts agree here.
DNS_LABEL_MAX_OCTETS = 63


def _unknown_zones(routes: Iterable[conventions.routes.Route]) -> set[str]:
    return {zone for route in routes for zone in route.zones} - set(conventions.ALL_ZONES)


def _published_names(route: conventions.routes.Route) -> tuple[str, ...]:
    """Every zone-relative name the row publishes: its own, and its extras'.

    An `Srv` label is a name in the zone rather than under the host, so it
    collides with another row's the same way two hosts do.
    """
    return (route.host, *(extra.label for extra in route.extras))


def _duplicated_names(routes: Iterable[conventions.routes.Route]) -> list[tuple[str, str]]:
    published = [(name, zone) for route in routes for name in _published_names(route) for zone in route.zones]
    return sorted({name for name in published if published.count(name) > 1})


def _hosts_that_are_not_labels(routes: Iterable[conventions.routes.Route]) -> set[str]:
    return {route.host for route in routes if not DNS_LABEL.fullmatch(route.host)}


def _oversized_labels(routes: Iterable[conventions.routes.Route]) -> set[str]:
    """Every name a row publishes that carries a label over the limit.

    The limit is per label and a published name is not always one label -- a
    service record's is `_service._proto` -- so the name is split before each
    piece is weighed.
    """
    return {
        name
        for route in routes
        for name in _published_names(route)
        if any(len(label.encode()) > DNS_LABEL_MAX_OCTETS for label in name.split('.'))
    }


def test_every_zone_a_row_names_is_one_the_installation_declares() -> None:
    """A misspelled zone is silent in both directions.

    `dns` writes rewrites for a domain nobody serves, and `apps` finds no zone
    id to declare the public record against -- neither is an error anything
    else reports.
    """
    assert _unknown_zones(conventions.routes.ROUTES) == set()
    assert _unknown_zones([conventions.routes.Route(host='photos', zones=('ucw.pdh',))]) == {'ucw.pdh'}


def test_no_two_rows_publish_the_same_host_in_the_same_zone() -> None:
    """One name is one application.

    Two rows on it are two records and two rewrites for the same name, and
    which application answers depends on the order the census happens to be
    in.
    """
    duplicated = [
        conventions.routes.Route(host='photos', zones=('ucw.phd',)),
        conventions.routes.Route(host='photos', zones=('ucw.phd', 'peifeng.phd')),
    ]

    assert _duplicated_names(conventions.routes.ROUTES) == []
    assert _duplicated_names(duplicated) == [('photos', 'ucw.phd')]


def test_no_two_rows_publish_the_same_name_in_the_same_zone() -> None:
    """A host and an extra's label are one namespace, not two.

    An `Srv` label sits in the zone rather than under the row's host, so it
    collides with another row's label and with another row's host alike. One
    walk over every name a row publishes is what sees both; a walk per kind
    would see neither the second pair nor the third.
    """
    identity = conventions.routes.Srv('_matrix-identity._tcp', priority=10, weight=0, port=443)
    two_extras = [
        conventions.routes.Route(host='matrix', zones=('ucw.phd',), extras=(identity,)),
        conventions.routes.Route(host='chat', zones=('ucw.phd',), extras=(identity,)),
    ]
    a_host_and_an_extra = [
        conventions.routes.Route(host='status', zones=('ucw.phd',)),
        conventions.routes.Route(
            host='chat',
            zones=('ucw.phd',),
            extras=(conventions.routes.Srv('status', priority=10, weight=0, port=443),),
        ),
    ]

    assert _duplicated_names(conventions.routes.ROUTES) == []
    assert _duplicated_names(two_extras) == [('_matrix-identity._tcp', 'ucw.phd')]
    assert _duplicated_names(a_host_and_an_extra) == [('status', 'ucw.phd')]


def test_a_rows_host_fits_in_a_dns_label() -> None:
    """The shape check bounds the characters, not how many of them there are.

    DNS caps a label at 63 octets, so a longer host matches the pattern and is
    refused by Cloudflare at apply time instead -- past the gate, in the one
    place the census exists to keep a name out of.
    """
    too_long = 'a' * 64

    assert _oversized_labels(conventions.routes.ROUTES) == set()
    assert _hosts_that_are_not_labels([conventions.routes.Route(host=too_long)]) == set()
    assert _oversized_labels([conventions.routes.Route(host=too_long)]) == {too_long}


def test_every_label_an_extra_publishes_fits_as_well() -> None:
    """An extra publishes a name too, and that name is several labels.

    The limit applies to each of them rather than to the name, so a service
    record whose pieces are all short is legal however long the whole reads,
    and one oversized piece is refused however short the whole reads.
    """
    oversized = conventions.routes.Srv('_' + 'a' * 63 + '._tcp', priority=10, weight=0, port=443)
    long_but_legal = conventions.routes.Srv('_' + 'a' * 60 + '._tcp', priority=10, weight=0, port=443)

    assert _oversized_labels([conventions.routes.Route(host='matrix', extras=(oversized,))]) == {oversized.label}
    assert _oversized_labels([conventions.routes.Route(host='matrix', extras=(long_but_legal,))]) == set()


def test_a_rows_host_is_a_label_and_not_a_fully_qualified_name() -> None:
    """The row is fanned out across its zones, so it may not carry one.

    A host written out in full would publish `photos.ucw.phd.ucw.phd` in every
    zone the row names.
    """
    assert _hosts_that_are_not_labels(conventions.routes.ROUTES) == set()
    assert _hosts_that_are_not_labels([conventions.routes.Route(host='photos.ucw.phd')]) == {'photos.ucw.phd'}


def test_a_row_is_published_in_the_primary_zone_alone_unless_it_says_otherwise() -> None:
    """Every zone a name is published in costs a certificate to cover it.

    So the zones a row reaches are what its owner asked for: a default that
    fanned every name across every zone would buy the whole set for a name one
    audience uses, and a LAN-only name -- which no public resolver answers --
    would buy one wildcard per zone for nothing.

    Held against `ZONE_PRIMARY` rather than against `PRIMARY_ONLY`, which is
    the value the default *is*: that comparison agrees with any default the
    census is given, every zone included. Naming the zone symbolically keeps
    this red if the default is re-pointed at `WEB_ZONES` or if `PRIMARY_ONLY`
    is quietly redefined, and green across a deliberate rename of the zone,
    which is not this case's subject.
    """
    assert conventions.routes.Route(host='photos').zones == (conventions.ZONE_PRIMARY,)


def test_a_row_states_what_it_publishes_beside_its_name() -> None:
    """An application that publishes more than a hostname says so in its row.

    The alternative is a published record that lives in neither this census nor
    the `dns` tables, findable only by reading the components. The target is
    the row itself rather than the hostname written out, so the name is spelled
    once and a rename cannot leave the two halves disagreeing.
    """
    matrix = conventions.routes.Route(
        host='matrix',
        proxied=False,
        extras=(conventions.routes.Srv('_matrix-identity._tcp', priority=10, weight=0, port=443),),
    )

    assert matrix.extras[0].target is conventions.routes.SELF
    assert conventions.routes.Route(host='photos').extras == ()


# --------------------------------------------------------------------------
# The forge census.
# --------------------------------------------------------------------------
# The `github` program declares from this table and the `credentials` command
# pushes secrets into what it names, so neither can disagree with it: both read
# it. What can is the workflow files and the composite actions their steps
# call. They spell labels, logins and the Environment a job deploys into as
# literals no import reaches, so a census that stopped carrying one of those
# leaves a condition that is never true or a job GitHub refuses to start -- and
# the cases below are that seam and nothing else.

ROOT = Path(__file__).parent.parent
GITHUB = ROOT / '.github'


def _workflows_and_actions() -> list[Path]:
    """The workflow files and the composite actions at `.github/actions/*/`.

    A step inside a composite action carries an `if:` and a `run:` GitHub
    evaluates exactly as it does a step written in the workflow, so a login or
    a label compared there is one the workflow branches on, and a census that
    read the workflows alone would be blind to it. Workflows can live nowhere
    but `.github/workflows/`, so that half of the set is closed by GitHub. A
    local action can live in any directory of the repository, and an action's
    step can `uses:` another, so that half is closed by a case instead: every
    local `uses:` in a file here resolves to a file here
    (`test_every_local_action_a_step_uses_is_one_the_censuses_read`), which is
    what makes reading this set the same as reading every step a workflow runs
    out of this repository.

    Both spellings of the suffix, because GitHub reads `.yml` and `.yaml`
    identically, and a file named the other way would be invisible the same way.
    """
    return sorted(
        path
        for pattern in ('workflows/*.yml', 'workflows/*.yaml', 'actions/*/action.yml', 'actions/*/action.yaml')
        for path in GITHUB.glob(pattern)
    )


#: How a step names an action in this repository rather than one on the
#: marketplace, and how a job names a reusable workflow in it: a path from
#: the repository root, in either spelling GitHub accepts. What it names is a
#: directory, whose action is its `action.yml` or `action.yaml`, or a
#: reusable workflow's file, which carries one of those suffixes itself --
#: GitHub confines those to `.github/workflows/`, so the file is one the
#: censuses read wherever it is a workflow at all.
LOCAL_ACTION_IN_A_STEP = re.compile(r'^[ \t-]*uses:[ \t]*[\'"]?(?:\./|\$/)([^\s\'"#]+)', re.MULTILINE)


def test_every_local_action_a_step_uses_is_one_the_censuses_read() -> None:
    """What closes the set above under `uses:`, and the only thing that does.

    `_workflows_and_actions` globs one directory of actions, and nothing in
    GitHub holds actions to that directory: a step may name any directory in
    the repository, and a composite action's own steps may name another. A
    condition in an action outside the glob would pass every census here
    exactly as one in `.github/actions/` did before the glob reached it. So
    every local `uses:` in every file the censuses read is resolved, and the
    file it lands on has to be one they read too -- which turns the glob from
    a directory someone chose into the whole of what a workflow runs out of
    this repository. A job's `uses:` of a reusable workflow resolves the same
    way; GitHub already holds those to `.github/workflows/`, so that half
    closes nothing and only checks the glob's spelling reached the file.
    """
    read = set(_workflows_and_actions())
    reached: set[Path] = set()
    unread: list[str] = []
    for path in sorted(read):
        for target in LOCAL_ACTION_IN_A_STEP.findall(path.read_text()):
            if target.endswith(('.yml', '.yaml')):
                candidates = [ROOT / target]
                absent = 'which does not exist'
            else:
                candidates = [ROOT / target / name for name in ('action.yml', 'action.yaml')]
                absent = 'where no action file exists'
            found = [candidate for candidate in candidates if candidate.is_file()]
            if not found:
                unread.append(f'{_name(path)} uses {target}, {absent}')
            elif found[0] not in read:
                unread.append(f'{_name(path)} uses {found[0].relative_to(ROOT)}, which no census reads')
            else:
                reached.add(found[0])

    # The jobs that reach a device join the overlay through this action first
    # (ci.md §2), so a pattern that stopped matching `uses:` lines would be
    # silent about the one every apply runs.
    assert GITHUB / 'actions' / 'zerotier' / 'action.yml' in reached
    assert not unread, f'a `uses:` reaches a file the censuses do not read: {unread}'


def _name(path: Path) -> str:
    """How a failure names a file: relative to `.github/`, since every action file is `action.yml`."""
    return str(path.relative_to(GITHUB))


#: How a workflow condition names a label on the pull request it is running
#: for, which is the only way any of them reads a label.
LABEL_IN_A_CONDITION = re.compile(r"pull_request\.labels\.\*\.name,\s*'([^']+)'")

#: How a workflow compares who is behind the event against a login: one of the
#: contexts that carries one -- `github.actor`, `github.triggering_actor`, a
#: `user.login`, a `sender.login` -- either way round the comparison is
#: written. `!=` is matched as well as `==`, because a login misspelled in a
#: negative test fails *open*, which is the worse of the two directions to
#: leave unpinned. Two groups, one per way round, so a match carries the login
#: in whichever of them is not empty.
AUTHOR_IN_A_CONDITION = re.compile(
    r"\.(?:\w+_)?(?:actor|login)\s*[=!]=\s*'([^']+)'|'([^']+)'\s*[=!]=\s*[\w.]*(?:actor|login)\b"
)

#: How a workflow reads a repository variable: the `vars` context, in either
#: of the two spellings GitHub's expression syntax accepts for a property.
#: Two groups, one per spelling, so a match carries the name in whichever of
#: them is not empty. Matched wherever it is written -- an input, an `env:`
#: line, a condition -- because a variable read anywhere and set nowhere is
#: an empty string at that place, and the step that receives it fails on its
#: own terms rather than reporting the absence.
VARIABLE_IN_A_WORKFLOW = re.compile(r"""\bvars\.([A-Za-z_][A-Za-z0-9_]*)|\bvars\[\s*['"]([^'"]+)['"]\s*\]""")

#: The Environment a job deploys into, as a workflow writes it out. The matrix
#: forms (`environment: ${{ matrix.stack }}`) name a value assembled elsewhere
#: and are skipped by the shape of this pattern: what it reaches is the
#: Environment spelled in the file, which is how the deploy chain and the drift
#: runs spell every one of theirs.
ENVIRONMENT_IN_A_JOB = re.compile(r'^[ \t]*environment:[ \t]*([a-z0-9-]+)[ \t]*$', re.MULTILINE)

#: The other way GitHub spells the same identity. `conventions.forge.Author`
#: carries a login and no id and says why, so a workflow reaching for the id
#: form is outside what the census covers -- and this is what makes that a red
#: check rather than a workflow that slipped past the scan above. It matches
#: the id wherever it is written, read as a value as readily as compared in a
#: condition, so a workflow that only ever *printed* one -- a bot's git
#: identity, say -- would fail this census too. That is deliberate: the
#: refusal fails closed and its message names the file, and whoever needs the
#: id form then extends the census on purpose rather than around it.
AUTHOR_BY_ID = re.compile(r'\.(?:user|sender)\.id\b|\.actor_id\b')


def test_every_environment_a_workflow_deploys_into_is_one_the_census_carries() -> None:
    """An Environment a workflow names and the census does not is a job that cannot start.

    GitHub refuses to run a job whose Environment does not exist, so a row
    renamed in the census strands the deploy chain at the step that still names
    the old one -- and the failure arrives on a merge to main rather than on
    the pull request that caused it. The workflow files are what makes this
    checkable at all: they are text no import reaches, so what they name and
    what the census carries are two sources that can disagree.
    """
    declared = {
        environment.name for repository in conventions.forge.REPOSITORIES for environment in repository.environments
    }
    read = {name for path in _workflows_and_actions() for name in ENVIRONMENT_IN_A_JOB.findall(path.read_text())}

    # The gated apply is the Environment whose credentials can root the gateway
    # (ci.md §3), so a scan that stopped reaching the chain would be silent
    # about the one it matters most for.
    assert 'physical' in read
    assert read <= declared, f'deployed into by a workflow and declared nowhere: {sorted(read - declared)}'


def test_every_label_a_workflow_branches_on_is_one_the_census_carries() -> None:
    """A label a workflow reads and nothing declares fails in the quietest way there is.

    The condition is simply never true, so the behavior it guards is
    unavailable at the moment somebody needs it and nothing anywhere reports
    that. Reading the workflows, and the actions their steps call, is what
    keeps the census from being shorter than what they depend on.
    """
    declared = {label.name for repository in conventions.forge.REPOSITORIES for label in repository.labels}
    read = {label for path in _workflows_and_actions() for label in LABEL_IN_A_CONDITION.findall(path.read_text())}

    # `expect-changes` stands noop-automerge down altogether (ci.md §3). A
    # census that lost it would leave a live condition pointing at a label no
    # pull request can carry, and that one fails open: the escape hatch is what
    # stops a deliberate change from merging on a proof it was never going to
    # pass.
    assert 'expect-changes' in read
    assert read <= declared, f'read by a workflow and declared nowhere: {sorted(read - declared)}'


def test_every_variable_a_workflow_reads_is_one_the_census_declares_for_this_repository() -> None:
    """A variable a workflow reads and nothing declares is a step that fails on an empty string.

    `sdk-regenerate.yml` hands `vars.DISPATCH_APP_CLIENT_ID` to the action
    that mints the dispatch App's token, and an unset variable reaches it as
    an empty client id: the mint fails, the run is red, and nothing says the
    variable was the cause. The forge declares every variable from the census
    (`components/forge`), so what this holds is the other side of that seam:
    the workflows are text no import reaches, and a variable they read that
    the census stopped carrying is caught here rather than on the next run.

    Held to **this** repository's row alone, unlike the label case, which
    pools every repository's labels: a variable is set on one repository, and
    every file under `.github/workflows/` here is `kluster`'s. One direction
    only, as the label case is: a declared variable no workflow reads is a
    value nothing consumes, not a failure.
    """
    declared = {variable.name for variable in conventions.forge.DEPLOYMENT.variables}
    read = {
        (_name(path), name)
        for path in _workflows_and_actions()
        for match in VARIABLE_IN_A_WORKFLOW.findall(path.read_text())
        for name in match
        if name
    }

    # The client id is the one variable the mint needs, and the mint runs
    # before anything is regenerated (ci.md §3): a scan that stopped reaching
    # it would be silent about the one read this case exists for.
    assert ('workflows/sdk-regenerate.yml', 'DISPATCH_APP_CLIENT_ID') in read
    undeclared = sorted(f'{file} reads vars.{name}' for file, name in read if name not in declared)
    assert undeclared == [], (
        f'read by a workflow and declared for {conventions.forge.DEPLOYMENT.name} nowhere: {undeclared}'
    )


def test_an_apps_client_id_is_a_variable_only_where_the_app_is_installed() -> None:
    """A mint handed a client id on a repository the App is not installed on fails at the mint.

    `actions/create-github-app-token` resolves the App's installation on the
    repository the run names, so the client id and the installation are two
    fields of one row that are only correct together: a row that carries the
    variable and not the App is a run that fails at its first step. The
    installation itself is console state nothing here declares
    (framework/github.md §4), so this is the one place the relation is held.
    """
    apps = {app.client_id: app for repository in conventions.forge.REPOSITORIES for app in repository.apps}

    # The dispatch App's row is the one this exists for: a census that lost
    # it would leave both loops below over empty tuples and the case green.
    assert conventions.forge.DISPATCH_APP in conventions.forge.DEPLOYMENT.apps
    assert conventions.forge.DISPATCH_APP_CLIENT_ID in conventions.forge.DEPLOYMENT.variables
    for repository in conventions.forge.REPOSITORIES:
        for variable in repository.variables:
            app = apps.get(variable.value)
            if app is not None:
                assert app in repository.apps, f'{repository.name} hands {app.slug} to a mint and does not carry it'


def test_every_login_a_workflow_compares_against_is_one_the_census_names() -> None:
    """A login a workflow compares against and nothing names is a route that never fires.

    Which is indistinguishable from a route nobody has needed yet, so nothing
    reports it. `renovate[bot]` is the hosted app's own login; a self-hosted
    instance, a different app slug or a personal-access-token user arrives
    under another one, and the difference is invisible until a pull request
    that should have taken the route quietly does not. Naming the login is what
    makes a wrong literal a red check instead.

    **What this reaches is a login written as a literal beside a comparison**,
    which is how every workflow here spells it and what the case below holds
    still. It is not a proof that no workflow can consult an identity any other
    way: a `startsWith`, a login inside a `fromJSON` list, and a literal parked
    in `env:` and compared in the shell all read as ordinary text to it. The id
    spelling is the one exception, refused by name in the case after this,
    because that is the substitution a workflow is most likely to make on
    purpose.
    """
    named = {author.login for repository in conventions.forge.REPOSITORIES for author in repository.authors}
    read = {
        login
        for path in _workflows_and_actions()
        for match in AUTHOR_IN_A_CONDITION.findall(path.read_text())
        for login in match
        if login
    }

    # The login this scan reaches is sdk-regenerate's, which spells it in a
    # step; noop-automerge keeps its own in `env:`, a form this scan cannot
    # see and the recipe-step case holds instead. What this holds still is
    # that a workflow naming the login names one the census carries.
    assert 'renovate[bot]' in read
    assert read <= named, f'compared against by a workflow and named nowhere: {sorted(read - named)}'


#: How a workflow step sets the author its commits carry, and how
#: `renovate.json5` names the authors whose commits leave a branch renovate's
#: own. Each is one literal in a file no import reaches.
COMMIT_EMAIL_IN_A_STEP = re.compile(r"git config user\.email '([^']+)'")
IGNORED_AUTHORS_IN_RENOVATE = re.compile(r"gitIgnoredAuthors:\s*\[\s*((?:'[^']+',?\s*)+)\]")


def test_every_author_a_workflow_commits_as_is_one_renovate_ignores() -> None:
    """A commit a workflow makes onto a renovate branch must not take the branch away from renovate.

    Renovate leaves a branch alone once someone else has committed to it --
    no rebase, no move to a newer release -- unless the author is one
    `gitIgnoredAuthors` names. The regeneration workflow commits onto
    renovate's branches by design (ci.md §3), so the address it commits with
    and the address renovate ignores are two spellings of one decision, and a
    workflow that changed its author would silently strand every bump it
    finished.
    """
    config = (ROOT / 'renovate.json5').read_text()
    found = IGNORED_AUTHORS_IN_RENOVATE.search(config)
    assert found is not None, 'renovate.json5 names no gitIgnoredAuthors'
    ignored = set(re.findall(r"'([^']+)'", found.group(1)))
    committing = {
        (_name(path), email)
        for path in _workflows_and_actions()
        for email in COMMIT_EMAIL_IN_A_STEP.findall(path.read_text())
    }

    # The regeneration workflow is the one that commits onto a branch that is
    # not its own, so a pattern that stopped matching would be silent about
    # the one case this exists for.
    assert any(name == 'workflows/sdk-regenerate.yml' for name, _ in committing)
    stranded = sorted(f'{name} commits as {email}' for name, email in committing if email not in ignored)
    assert stranded == [], f'commits as an author renovate does not ignore: {stranded}'


def test_no_workflow_identifies_an_account_by_id() -> None:
    """`conventions.forge.Author` carries a login and no id, and says why.

    A workflow that switches to the id form is reaching for a spelling the
    census does not carry -- and one the case above cannot see, since it reads
    logins written as literals. Refusing it by name is what keeps that a red
    check with a reason on it, instead of a census that silently stopped
    covering the condition it exists for.
    """
    reached = {_name(path) for path in _workflows_and_actions() if AUTHOR_BY_ID.search(path.read_text())}

    assert not reached, f'identifies an account by id, which conventions.forge.Author does not carry: {sorted(reached)}'


#: How a step points a `pulumi` command at one stack: the flag in both of its
#: spellings, since `-s` and `--stack` are the same flag and a census that knew
#: only the long one would be blind to the short -- the short one alone or at
#: the end of a cluster of short flags (`-ys`), its value after a space, an `=`
#: or nothing; and `stack select`, which points every later command in the
#: step there, with any flags it carries ahead of the name. The name is bare or
#: quoted, and read as its last segment, so a fully qualified
#: `<organization>/<project>/<stack>` is the stack it ends in.
PULUMI_STACK_NAMED = re.compile(
    r"""(?:--stack(?:=|\s+)|(?<![\w-])-[A-Za-z]*s(?:=|\s*)|\bstack\s+select\s+(?:--?[\w-]+\s+)*)['"]?(?:[\w.-]+/)*([\w.-]+)"""
)

#: How a step runs a mise task: `mise run`, its alias `mise r`, the long
#: `mise tasks run`, or the bare `mise <task>` mise accepts for a task no
#: command of its own shadows -- each with mise's own flags allowed ahead of it.
MISE_TASK_RUN = re.compile(
    r"""\bmise\s+(?:--?[\w-]+\s+)*(?:(?:tasks\s+)?(?:run|r)\s+)?(?:--?[\w-]+\s+)*['"]?([\w:.-]+)"""
)


def _tasks_run_against(stacks: set[str]) -> set[str]:
    """The mise tasks whose script points `pulumi` at one of `stacks`.

    Read off `mise.toml` rather than named here: a task is a second way to
    reach a stack, and one that wraps the stack's own passphrase is the easiest
    way there is. What this does not see: a file task (under `mise-tasks/` or
    `.mise/tasks/`), and a task whose script reaches the stack through another
    task -- one that runs `mise run github` -- rather than through `pulumi`.
    """
    tasks = cast('dict[str, dict[str, object]]', tomllib.loads((ROOT / 'mise.toml').read_text()).get('tasks', {}))
    return {
        name
        for name, task in tasks.items()
        if any(match.group(1) in stacks for match in PULUMI_STACK_NAMED.finditer(str(task.get('run', ''))))
    }


def _apart_stack_named(text: str, apart: set[str], tasks: set[str]) -> list[str]:
    """Every place `text` points a command at a stack in `apart`, as `text` spells it.

    The flag and `stack select` are read only in a file that runs `pulumi` at
    all, because `-s` means something else to half the tools a workflow calls.
    A task is read everywhere: its name is the whole of what points it at the
    stack.
    """
    named = [match.group(0) for match in MISE_TASK_RUN.finditer(text) if match.group(1) in tasks]
    if 'pulumi' in text:
        named += [match.group(0) for match in PULUMI_STACK_NAMED.finditer(text) if match.group(1) in apart]
    return named


#: Every spelling the census has to catch, with `{stack}` for a stack encrypted
#: apart and `{task}` for a task that runs against one.
POINTED_AT = (
    'pulumi preview --stack {stack}',
    'pulumi preview --stack={stack}',
    "pulumi preview --stack '{stack}'",
    'pulumi preview --stack "{stack}"',
    'pulumi up -s {stack} --yes',
    "pulumi up -s '{stack}'",
    'pulumi stack select {stack}',
    'pulumi stack select --create {stack}',
    'pulumi up -s{stack}',
    'pulumi up -s={stack}',
    'pulumi up -ys {stack}',
    'pulumi preview --stack organization/kluster/{stack}',
    'mise run {task} up --yes',
    'mise r {task} preview',
    'mise {task} preview',
    'mise -q run {task} up',
    'mise tasks run {task} up',
)

#: What the census must stay silent on: other stacks, other tasks, an
#: expression, and the word itself where it is not a stack.
NOT_POINTED_AT = (
    'pulumi preview --stack physical --diff',
    'mise x -- pulumi up --stack apps --yes',
    'pulumi stack select dns',
    'mise run lint',
    'mise x -- pulumi preview --stack ${{{{ matrix.stack }}}}',
    'git push https://x-access-token@{stack}.com/${{{{ {stack}.repository }}}}',
    'curl -s https://api.{stack}.com/repos',
    'ssh -s {stack} sftp',
)


def test_the_census_of_stacks_named_catches_every_spelling_and_nothing_else() -> None:
    """The census's positive control, and its negative one.

    A census that reads a workflow and finds nothing is only evidence if it
    would have found the thing it looks for, so each spelling a step could
    point a command at the apart stack with is shown to it, and so is each
    near miss it must let pass. The expression is among the near misses on
    purpose: the census does not catch it, and the case below says what does.
    """
    apart = set(pulumi_config.APART)
    tasks = _tasks_run_against(apart)
    # The task set is discovered, so it is held to have found something before
    # the spellings that need a task are walked.
    assert tasks, 'no mise task runs against a stack encrypted apart, so the task spellings test nothing'

    for stack in sorted(apart):
        for task in sorted(tasks):
            for spelling in POINTED_AT:
                line = spelling.format(stack=stack, task=task)
                assert _apart_stack_named(line, apart, tasks), f'the census misses {line!r}'
        for spelling in NOT_POINTED_AT:
            line = spelling.format(stack=stack)
            assert not _apart_stack_named(line, apart, tasks), f'the census fires on {line!r}'


def test_no_workflow_points_a_pulumi_command_at_the_stack_encrypted_apart() -> None:
    """The stack CI may not run, held against what CI actually contains.

    What keeps the forge's admin token out of CI is not that the token is hard
    to reach -- it is a config secret in a committed file like any other -- but
    that its stack is encrypted under a passphrase no Environment holds *and*
    that no job names it. Either alone is one accident from gone, so both are
    held: this case, and the slot map's own (`tests/test_slots.py`).

    A `preview` would be as bad as an `up`. Reading that stack's config at all
    means holding its passphrase, and a workflow that held it would have it in
    an Environment -- which is the partition being defended (ci.md §3).

    **This census reads literal names**, in the spellings the case above shows
    it, so a stack named through an expression -- `--stack ${{ matrix.stack }}`,
    with the name added to a matrix list -- passes it. Widening it to the text
    is impractical rather than merely unwritten: every workflow says `github`
    many times over through `${{ github.* }}`, so a census over the word would
    be noise. What catches that spelling is the other half of the pair, and it
    catches it at run time rather than at review time: the job's Environment
    holds the estate passphrase alone, so the run dies `error: incorrect
    passphrase` with nothing of that stack's config in reach. This case is the
    cheap, early half of a guard whose expensive half cannot be evaded.
    """
    apart = set(pulumi_config.APART)
    tasks = _tasks_run_against(apart)
    named = [
        f'{_name(path)}: {spelling}'
        for path in _workflows_and_actions()
        for spelling in _apart_stack_named(path.read_text(), apart, tasks)
    ]

    assert named == [], f'a workflow runs `pulumi` against a stack encrypted apart from the estate: {named}'


# --------------------------------------------------------------------------
# The alert payload and its producer.
# --------------------------------------------------------------------------
# `conventions.alert` is the payload every alert travels as, and nothing in
# this tree reads it at run time: the writer is `alert.yml`, a workflow no
# import reaches, and the reader is the ops repository's dispatch handler,
# which nothing here can see. So what holds the convention on this side is the
# cases below -- the producer's event type, inputs and payload keys against the
# census, every workflow that runs on `main` against the rule for calling it,
# and the credential it mints with against the register's map and the forge.

#: The reusable workflow every alert in CI goes through, as a caller's
#: `uses:` names it.
PRODUCER = GITHUB / 'workflows' / 'alert.yml'
PRODUCER_USES = './.github/workflows/alert.yml'

#: The triggers that do **not** run a workflow on `main` outside a pull
#: request: a pull request's own, and being called by another workflow -- a
#: called workflow runs as a job of its caller, whose `alert` sees its failure,
#: and the producer is one. Every other trigger does -- `push` and
#: `workflow_dispatch` today, and `repository_dispatch`, `workflow_run`,
#: `release` or `merge_group` the day one is added -- so the rule is read as
#: the complement of this set rather than as a list of the ones that count.
NOT_ON_MAIN = frozenset({'pull_request', 'workflow_call'})

#: The one condition every `alert` job is written with (framework/ci.md §3):
#: after every needed job, whatever its result; never for a pull request; and
#: only when one of them failed. Compared whole, whitespace folded, because a
#: condition that merely contains these words can mean their opposite --
#: `!contains(...)`, or `always() || ...`.
ALERT_CONDITION = "always() && github.event_name != 'pull_request' && contains(needs.*.result, 'failure')"

#: The workflows that run on `main` and do not end in the alert job yet, each
#: with the reason. An entry is removed by the change that converts its
#: workflow, and the case below goes red if an entry outlives its reason in
#: either direction: a workflow that no longer runs on `main`, or one that
#: already calls the producer.
NOT_YET_CALLING = {
    'deploy.yml': (
        'keeps its `notify-failure` job, which posts to Home Assistant directly, until the ops '
        "repository's dispatch handler is there to receive the producer's dispatch"
    ),
}

#: How the producer names the event it posts, and how it names the target of
#: the post: `event_type:` inside the payload `jq` builds, and the
#: `repos/<owner>/<name>/dispatches` endpoint.
EVENT_TYPE_IN_A_PAYLOAD = re.compile(r'\bevent_type:\s*["\']?([A-Za-z0-9_.-]+)')
DISPATCH_TARGET = re.compile(r'repos/\$\w+/([A-Za-z0-9_.-]+)/dispatches')
#: The payload object `jq` builds, and each entry of it as a key and the
#: `jq` variable it takes its value from.
CLIENT_PAYLOAD = re.compile(r'client_payload:\s*\{(.*?)\}', re.DOTALL)
PAYLOAD_ENTRY = re.compile(r'\b(\w+):\s*\$(\w+)')
#: How the producer's step binds a `jq` variable to a shell variable
#: (`--arg tier "$TIER"`, `--arg key "${KEY:-$source}"`), and how its `env:`
#: fills a shell variable from an input (`TIER: ${{ inputs.tier }}`).
JQ_ARGUMENT = re.compile(r'--arg (\w+) "\$\{?(\w+)')
INPUT_IN_ENV = re.compile(r'^\$\{\{\s*inputs\.(\w+)\s*\}\}$')
#: GitHub's cap on the top-level properties of a dispatch's client payload
#: (REST API, "Create a repository dispatch event": `client_payload` takes at
#: most 10). A payload over it is refused outright.
CLIENT_PAYLOAD_PROPERTIES_LIMIT = 10
#: A secret as an expression reads it; the names a reusable workflow declares
#: may carry hyphens, which repository secret names cannot.
SECRET_IN_AN_EXPRESSION = re.compile(r'\bsecrets\.([A-Za-z0-9_-]+)')
#: A repository variable as the mint's `client-id` reads it, whole.
CLIENT_ID_VARIABLE = re.compile(r'^\$\{\{\s*vars\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$')
#: A playbook reference: a document of this repository and a numbered section.
PLAYBOOK_REFERENCE = re.compile(r'^(docs/[\w./-]+\.md) §(\d+(?:\.\d+)*)$')
#: The permissions a mint asks for, as `actions/create-github-app-token`
#: spells them: one input per permission, and none at all means every
#: permission the installation has.
PERMISSION_INPUT = 'permission-'


def _mapping(value: object, what: str) -> dict[str, object]:
    assert isinstance(value, dict), f'{what} is not a mapping'
    return cast('dict[str, object]', value)


def _workflow(path: Path) -> dict[str, object]:
    return _mapping(yaml.safe_load(path.read_text()), _name(path))


def _on(workflow: dict[str, object]) -> object:
    keyed = cast('dict[object, object]', workflow)
    return keyed.get('on', keyed.get(True))


def _triggers(workflow: dict[str, object]) -> set[str]:
    """The events a workflow runs on, in any of the three shapes `on:` takes.

    YAML 1.1 reads a bare `on` key as the boolean `true`, which is how PyYAML
    hands it back, so a read under the string alone finds no trigger in any
    workflow here.
    """
    on = _on(workflow)
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(event) for event in cast('list[object]', on)}
    return {str(event) for event in _mapping(on, 'on:')}


def _jobs(workflow: dict[str, object], what: str) -> dict[str, dict[str, object]]:
    jobs = _mapping(workflow.get('jobs'), f'{what} jobs:')
    return {name: _mapping(job, f'{what} job {name}') for name, job in jobs.items()}


def _needs(job: dict[str, object]) -> set[str]:
    needs = job.get('needs', [])
    return {needs} if isinstance(needs, str) else {str(need) for need in cast('list[object]', needs)}


def _workflows_running_on_main() -> dict[str, dict[str, object]]:
    return {
        path.name: workflow
        for path in _workflows_and_actions()
        if path.parent.name == 'workflows' and _triggers(workflow := _workflow(path)) - NOT_ON_MAIN
    }


def _producer_callers() -> dict[str, dict[str, object]]:
    """Every job in every workflow that calls the producer, as `<file>: <job>`."""
    return {
        f'{path.name}: {name}': job
        for path in _workflows_and_actions()
        if path.parent.name == 'workflows'
        for name, job in _jobs(_workflow(path), path.name).items()
        if job.get('uses') == PRODUCER_USES
    }


def _producer_step(uses: str | None) -> dict[str, object]:
    """The producer's one step whose `uses:` starts with `uses`, or its one `run:` step for `None`."""
    steps = [
        _mapping(step, 'a step')
        for step in cast('list[object]', _jobs(_workflow(PRODUCER), 'alert.yml')['dispatch'].get('steps'))
    ]
    found = [
        step
        for step in steps
        if (uses is None and 'run' in step) or (uses is not None and str(step.get('uses', '')).startswith(uses))
    ]
    assert len(found) == 1, f'the producer has {len(found)} steps of that kind'
    return found[0]


def _producer_call() -> dict[str, object]:
    """The producer's `on.workflow_call`: the inputs and the secrets it declares."""
    return _mapping(_mapping(_on(_workflow(PRODUCER)), 'on:').get('workflow_call'), 'workflow_call')


def test_every_workflow_that_runs_on_main_ends_in_the_alert_job() -> None:
    """A red run on `main` that raises no alert is one nobody hears about.

    Nothing stands between a merge and what runs after it, so a workflow that
    fails on `main` without calling the producer is silent in the way the
    alert discipline exists to prevent (cluster/architecture.md §4.3). The
    rule is written as a definition -- every workflow with a trigger other
    than `pull_request` and `workflow_call`, since a called workflow runs as a
    job of its caller, whose `alert` sees its failure -- so a workflow added
    later is held to it without anyone remembering to list it. Each such workflow ends in a job named
    `alert` that calls the producer, needs every other job of the workflow (a
    job it does not need is one whose failure it cannot see), and carries the
    one condition every caller writes, which stands it down for a pull request
    because a fork's run holds no secret to mint with and a pull request's
    failure is already on it.
    """
    running_on_main = _workflows_running_on_main()

    # A trigger read that stopped matching would empty the set and pass the
    # loop below on nothing. `drift` is in it by its one trigger,
    # `workflow_dispatch`, which a read of `push` alone would miss.
    assert 'drift.yml' in running_on_main
    for name, reason in NOT_YET_CALLING.items():
        assert name in running_on_main, f'{name} is excused from the alert job but does not run on main: {reason}'
        calling = [job for job in _jobs(running_on_main[name], name).values() if job.get('uses') == PRODUCER_USES]
        assert not calling, f'{name} calls the producer; drop its excuse'

    findings: list[str] = []
    for name, workflow in sorted(running_on_main.items()):
        if name in NOT_YET_CALLING:
            continue
        jobs = _jobs(workflow, name)
        alert = jobs.get('alert')
        if alert is None:
            findings.append(f'{name} has no `alert` job')
            continue
        if alert.get('uses') != PRODUCER_USES:
            findings.append(f'{name}: `alert` does not use {PRODUCER_USES}')
        if (missing := set(jobs) - {'alert'} - _needs(alert)) or _needs(alert) - set(jobs):
            findings.append(
                f'{name}: `alert` needs {sorted(_needs(alert))}, not every other job (missing {sorted(missing)})'
            )
        condition = ' '.join(str(alert.get('if', '')).split())
        if condition != ALERT_CONDITION:
            findings.append(f'{name}: `alert` is conditioned {condition!r}, not {ALERT_CONDITION!r}')

    assert findings == [], findings


def test_the_event_type_is_spelled_once() -> None:
    """The producer posts the census's event type, and nothing else under `.github/` posts one.

    A dispatch whose event type the handler does not filter for is accepted
    with a `204` and starts nothing, so a producer that drifted from the
    census is silent at exactly the moment it was meant to speak. The second
    half keeps the producer the one place a dispatch is made: a second caller
    of the endpoint would be a second spelling nothing here holds.
    """
    from kluster.conventions import alert

    assert EVENT_TYPE_IN_A_PAYLOAD.findall(PRODUCER.read_text()) == [alert.EVENT]
    elsewhere = sorted(
        str(path.relative_to(GITHUB))
        for path in GITHUB.rglob('*')
        if path.is_file() and path != PRODUCER and re.search(r'dispatches|event_type', path.read_text())
    )
    assert elsewhere == [], f'a dispatch is spelled outside the producer: {elsewhere}'


def test_the_producer_takes_every_field_but_the_computed_ones_and_sends_them_all() -> None:
    """The payload the producer builds carries exactly the census's fields, each from its own input.

    A caller passes what only it knows -- the tier, the summary, the
    playbook, a key, details -- and the producer fills in the rest from the
    run. An input the census does not carry is a field the handler never
    reads; a field the payload drops is one the handler reads as missing,
    and for `tier`, `key` or `summary` that is a malformed alert. The keys
    alone are not enough: a payload whose `tier` carries the summary has the
    right keys and is malformed all the same, so each field is followed from
    the input through `env:` and `jq`'s `--arg` to the payload entry.
    """
    from kluster.conventions import alert

    assert set(alert.COMPUTED) <= set(alert.FIELDS)
    assert len(alert.FIELDS) <= CLIENT_PAYLOAD_PROPERTIES_LIMIT
    inputs = _mapping(_producer_call().get('inputs'), 'workflow_call inputs')
    assert set(inputs) == set(alert.FIELDS) - set(alert.COMPUTED)

    step = _producer_step(None)
    script = str(step['run'])
    payload = CLIENT_PAYLOAD.search(script)
    assert payload is not None, 'the producer builds no client_payload'
    entries = PAYLOAD_ENTRY.findall(payload.group(1))
    assert sorted(key for key, _ in entries) == sorted(alert.FIELDS), entries
    crossed = [f'{key}: ${variable}' for key, variable in entries if key != variable]
    assert crossed == [], f"a payload entry takes another field's value: {crossed}"

    arguments = dict(JQ_ARGUMENT.findall(script))
    env = _mapping(step.get('env'), 'the step env:')
    unbound: list[str] = []
    for field in sorted(inputs):
        shell = arguments.get(field)
        filled = INPUT_IN_ENV.match(str(env.get(field.upper(), '')))
        if shell != field.upper() or filled is None or filled.group(1) != field:
            unbound.append(f'{field}: --arg from ${shell}, env {field.upper()} from {env.get(field.upper())!r}')
    assert unbound == [], unbound


def test_every_tier_a_caller_passes_is_one_the_census_names() -> None:
    """A tier outside the census is an alert the handler refuses as malformed.

    It is still delivered -- as an issue about the producer -- but not as the
    alert it was meant to be, and a spelling like `warning` reads as a
    perfectly good tier to whoever wrote it.
    """
    from kluster.conventions import alert

    callers = _producer_callers()

    assert 'drift.yml: alert' in callers
    passed = {name: _mapping(job.get('with'), f'{name} with:').get('tier') for name, job in callers.items()}
    unknown = sorted(f'{name} passes {tier!r}' for name, tier in passed.items() if tier not in set(alert.Tier))
    assert unknown == [], unknown


def test_every_playbook_a_caller_passes_is_a_section_that_exists() -> None:
    """An alert whose playbook does not resolve is an alert shipped without one.

    The discipline is that no alert ships without its written procedure
    (cluster/architecture.md §4.3), and the handler renders the reference as a
    link into this repository: a file that moved or a section renumbered is a
    link to nothing at the moment somebody needs it.
    """
    callers = _producer_callers()

    assert 'drift.yml: alert' in callers
    dangling: list[str] = []
    for name, job in sorted(callers.items()):
        playbook = str(_mapping(job.get('with'), f'{name} with:').get('playbook'))
        reference = PLAYBOOK_REFERENCE.match(playbook)
        if reference is None:
            dangling.append(f'{name}: {playbook!r} is not `docs/<file>.md §N`')
            continue
        document = ROOT / reference.group(1)
        if not document.is_file() or reference.group(2) not in sections(prose(document.read_text())):
            dangling.append(f'{name}: {playbook} is no section of this repository')
    assert dangling == [], dangling


def test_the_secret_the_producer_is_handed_is_the_one_the_map_fills() -> None:
    """Every caller hands the producer the dispatch App's key, from where the register puts it.

    The name is a contract between the workflows and the `credentials` slot
    map, and a rename on either side is a mint that fails on an empty key at
    the first alert. Where the map puts it matters as much as what it calls
    it: a repository secret of this repository, because the alert job belongs
    to no stack and names no Environment, and an Environment secret is
    invisible to a job that does not.
    """
    from kluster.scripts.credentials import slots
    from kluster.scripts.credentials.github_secrets import Slot

    declared = set(_mapping(_producer_call().get('secrets'), 'workflow_call secrets'))
    assert declared, 'the producer declares no secret'
    assert set(SECRET_IN_AN_EXPRESSION.findall(PRODUCER.read_text())) == declared

    callers = _producer_callers()
    assert 'drift.yml: alert' in callers
    handed: set[str] = set()
    for name, job in callers.items():
        secrets = _mapping(job.get('secrets'), f'{name} secrets:')
        assert set(secrets) == declared, f'{name} hands {sorted(secrets)}, the producer declares {sorted(declared)}'
        handed |= {found for value in secrets.values() for found in SECRET_IN_AN_EXPRESSION.findall(str(value))}
    assert handed == {slots.DISPATCH_APP_KEY}

    filled = [
        target
        for row in slots.ROWS.values()
        for target in row.targets
        if isinstance(target, Slot) and target.name == slots.DISPATCH_APP_KEY
    ]
    assert filled == [Slot(repository=conventions.forge.DEPLOYMENT.full_name, name=slots.DISPATCH_APP_KEY)]


def test_the_producer_mints_for_the_ops_repository_with_an_app_installed_there() -> None:
    """The client id the producer reads names an App the target repository carries.

    `actions/create-github-app-token` resolves the App's installation on the
    repositories it is asked for, so a client id whose App is not installed on
    the ops repository is a mint that fails at the first alert. The variable
    is this repository's (the workflow runs here), and the installation that
    matters is the ops repository's (the dispatch lands there): two rows of
    the forge, which is why the relation between them is held here rather than
    by `test_an_apps_client_id_is_a_variable_only_where_the_app_is_installed`,
    which holds one row. The endpoint the dispatch goes to is the same
    repository, spelled in the workflow, and held to the forge too.
    """
    mint = _mapping(_producer_step('actions/create-github-app-token@').get('with'), 'the mint')

    variable = CLIENT_ID_VARIABLE.match(str(mint.get('client-id')))
    assert variable is not None, 'the client id is not read from a repository variable'
    values = {declared.name: declared.value for declared in conventions.forge.DEPLOYMENT.variables}
    assert variable.group(1) in values, (
        f'{variable.group(1)} is declared for {conventions.forge.DEPLOYMENT.name} nowhere'
    )
    client_ids = {app.client_id for app in conventions.forge.OPS.apps}
    assert values[variable.group(1)] in client_ids, (
        'the App the producer mints as is not installed on the ops repository'
    )

    assert mint.get('repositories') == conventions.forge.OPS.name
    assert DISPATCH_TARGET.findall(PRODUCER.read_text()) == [conventions.forge.OPS.name]

    # What a `repository_dispatch` costs, and nothing else. An absent
    # permission input is not a narrower token but the widest one: the mint
    # then asks for every permission the installation has.
    permissions = {str(key): value for key, value in mint.items() if str(key).startswith(PERMISSION_INPUT)}
    assert permissions == {f'{PERMISSION_INPUT}contents': 'write'}, permissions


def test_every_caller_is_named_for_its_file() -> None:
    """A caller's `name:` is its file name without the suffix, because that is what the alert's source says.

    The producer reads the workflow's name from the run, not its file, so the
    `source` -- and with it the default deduplication key -- is whatever the
    caller's `name:` says (`conventions.alert`). One that drifts from the file
    is an alert whose source names no workflow anybody can find, and a rename
    of the `name:` alone is a new key: the open issue for the old one is never
    commented again.
    """
    callers = {name.split(': ')[0] for name in _producer_callers()}

    assert 'drift.yml' in callers
    misnamed = sorted(
        f'{file} is named {_workflow(GITHUB / "workflows" / file).get("name")!r}'
        for file in callers
        if _workflow(GITHUB / 'workflows' / file).get('name') != Path(file).stem
    )
    assert misnamed == [], misnamed


def test_the_alert_label_is_declared_and_on_no_public_repository() -> None:
    """The label the dispatch handler finds an alert's open issue by is declared, and only where the issues live.

    The handler lists open issues by this label to find the one an alert's
    `key` already has, and opens a new one carrying it (operations.md §4).
    Nothing in this tree reads the label -- the handler is a workflow in the
    ops repository -- so the census entry is the only thing that makes the
    `github` stack declare it; a label the handler lists by and nothing
    declares is an empty list, and every repeat of an alert opens a second
    issue. It sits on the ops repository and on no public one, because alert
    issues never live in a public tracker (cluster/architecture.md §4.3).
    """
    carrying = [
        repository for repository in conventions.forge.REPOSITORIES if conventions.forge.ALERT in repository.labels
    ]

    assert carrying, 'no repository declares the alert label'
    assert [repository.name for repository in carrying if repository.public] == []


# --------------------------------------------------------------------------
# The local packages.
# --------------------------------------------------------------------------
# `Pulumi.yaml`'s `packages:` block is the recipe for every SDK under `sdks/`:
# which release of Pulumi's any-Terraform-provider bridge, parameterized with
# which upstream provider at which version. No program reads the block -- it is
# `pulumi install`'s input -- and the SDK that command generates records the
# same three values in its own `pulumi-plugin.json`, which is what the engine
# resolves the plugin from at run time. Two artifacts of independent origin,
# so they can disagree: a version edited into the block is not a bump until the
# SDK is regenerated from it, and the cases below are what say so. They are the
# `uv sync --locked` of these packages.

PULUMI_YAML = ROOT / 'Pulumi.yaml'
PACKAGES = ROOT / 'packages'
SDKS = ROOT / 'sdks'

#: The generator every entry of the block names, and the `name` every SDK's
#: `pulumi-plugin.json` carries: the bridge is the plugin, and the upstream
#: provider is its parameter.
BRIDGE = 'terraform-provider'


class Declared(NamedTuple):
    """One entry of the block: the bridge release and the provider it is parameterized with."""

    bridge: str
    provider: str
    version: str


def _declared_packages() -> dict[str, Declared]:
    """The block as written, keyed by the SDK it declares."""
    block = yaml.safe_load(PULUMI_YAML.read_text())['packages']
    return {
        name: Declared(str(entry['version']), str(entry['parameters'][0]), str(entry['parameters'][1]))
        for name, entry in block.items()
        if entry['source'] == BRIDGE
    }


def _generated_plugin(directory: Path) -> dict[str, object] | None:
    """What an SDK under `directory` says it was generated from, as the engine reads it.

    `None` where there is no such file: a package with no `pulumi-plugin.json`
    is not a generated SDK at all, which is an answer rather than an error.
    """
    plugins = list(directory.glob('pulumi_*/pulumi-plugin.json'))
    if not plugins:
        return None
    (plugin,) = plugins
    return json.loads(plugin.read_text())


def _bridge_parameterization(provider: str, version: str) -> dict[str, dict[str, str]]:
    """How the bridge records its parameter inside the SDK it generates.

    `parameterization.value` is this JSON, base64-encoded. Transcribed from
    the bridge's own output rather than derived from anything here, which is
    what makes it the side the block cannot move. The coordinates are taken
    as the block spells them: the bridge would generate the same SDK from
    `Backblaze/b2`, but renovate's lookup against the OpenTofu registry is
    case-sensitive and finds only `backblaze/b2`, so the spelling the SDK
    records is the one the block has to hold.
    """
    return {'remote': {'url': f'registry.opentofu.org/{provider}', 'version': version}}


def test_every_sdk_is_declared_and_every_declaration_has_its_sdk() -> None:
    """The block and the directory list the same packages, both ways.

    An SDK the block does not declare is one `pulumi install` cannot regenerate
    and no bump can reach; a declaration with no SDK is an import that fails at
    the first `pulumi` run. The bridge is the only generator the block names
    today, which the filter in `_declared_packages` states rather than assumes.
    """
    declared = set(_declared_packages())
    committed = {path.name for path in SDKS.iterdir() if path.is_dir()}

    assert declared, 'the block declares no bridged package'
    assert declared == committed, (
        f'declared but not committed: {sorted(declared - committed)}; committed but not declared: {sorted(committed - declared)}'
    )


@pytest.mark.parametrize('name', sorted(_declared_packages()))
def test_a_committed_sdk_was_generated_from_what_the_block_declares(name: str) -> None:
    """Each SDK's `pulumi-plugin.json` carries the bridge release and the provider the block names.

    The block is edited -- by renovate or by hand -- and the SDK is generated,
    so the two agree only when `pulumi install` has run since the edit. Every
    pin-bearing field is compared, and the parameter is compared decoded, so a
    provider swapped for another at the same version fails here too.
    """
    declared = _declared_packages()[name]
    plugin = _generated_plugin(SDKS / name)
    assert plugin is not None, f'sdks/{name} carries no pulumi-plugin.json'
    parameterization = cast('dict[str, str]', plugin['parameterization'])
    recorded = json.loads(base64.b64decode(parameterization['value']))

    stale = f'sdks/{name} was generated from a different declaration than Pulumi.yaml holds; run `pulumi install`'
    assert plugin['name'] == BRIDGE, stale
    assert plugin['version'] == declared.bridge, stale
    assert parameterization['name'] == name, stale
    assert parameterization['version'] == declared.version, stale
    assert recorded == _bridge_parameterization(declared.provider, declared.version), stale


#: What `renovate.json5` reads the block with, spelled exactly as that file
#: holds it: one pattern for the bridge release, which every entry pins and
#: one bump moves everywhere, and one for the provider each entry is
#: parameterized with.
BRIDGE_MATCH_STRING = r'source: terraform-provider\s+version: (?<currentValue>\d[\d.]*)'
PROVIDER_MATCH_STRING = (
    r'source: terraform-provider\s+version: [\d.]+\s+parameters:\s+'
    r'- (?<depName>[\w.-]+/[\w.-]+)\s+- (?<currentValue>\d[\d.]*)'
)


def _as_renovate_spells_it(pattern: str) -> str:
    """The JSON5 single-quoted string `renovate.json5` holds a pattern in."""
    return "'" + pattern.replace('\\', '\\\\') + "'"


def _as_python_spells_it(pattern: str) -> re.Pattern[str]:
    """Python spells a named group `(?P<...>`, renovate's regex engine `(?<...>`."""
    return re.compile(pattern.replace('(?<', '(?P<'))


def test_renovate_reads_every_entry_of_the_packages_block() -> None:
    """The two managers reach every pin the block holds, and the pins they read are the block's.

    Renovate reads its configuration from the default branch, so a pattern
    that stopped matching an entry -- a key renamed, a line inserted between
    the two it spans -- would open no pull request and report nothing. Holding
    the pattern here, against the block as written, is what makes that a red
    check on the change that did it.
    """
    config = (ROOT / 'renovate.json5').read_text()
    text = PULUMI_YAML.read_text()
    declared = _declared_packages()

    assert _as_renovate_spells_it(BRIDGE_MATCH_STRING) in config
    assert _as_renovate_spells_it(PROVIDER_MATCH_STRING) in config

    bridges = [found.group('currentValue') for found in _as_python_spells_it(BRIDGE_MATCH_STRING).finditer(text)]
    providers = {
        (found.group('depName'), found.group('currentValue'))
        for found in _as_python_spells_it(PROVIDER_MATCH_STRING).finditer(text)
    }

    assert bridges == [entry.bridge for entry in declared.values()]
    assert providers == {(entry.provider, entry.version) for entry in declared.values()}


#: The workflow that merges a bump of the block without a human, and the one
#: `Pulumi.*` admission it carries (framework/ci.md §3).
NOOP_AUTOMERGE = GITHUB / 'workflows' / 'noop-automerge.yml'

#: How the admission step names the key it removes before comparing the two
#: revisions of `Pulumi.yaml`: a `yq` expression, in the one step that reads
#: that file at all.
BLOCK_REMOVED_BEFORE_COMPARING = re.compile(r'del\(\.(\w+)\)')


def test_the_unattended_route_for_a_bump_removes_the_block_and_tests_renovate() -> None:
    """`classify` admits a bump of the block, and neither half of how it decides is written twice.

    A bump of `packages:` is the one change to a `Pulumi.*` path that merges
    unattended: the block is the generator's recipe, no stack program reads it,
    and `checks` holds every `sdks/<name>` to it. What makes that route
    admissible is exactly two things, and each fails silently on its own. The
    key removed before the two revisions are compared has to be *this* block --
    remove `config:` instead and the versions pins stop being compared, which
    is a stack configuration change merging on a proof that never looked at it.
    And the login has to be the one the census names, spelled once: a workflow
    cannot notice that it guessed a login wrong, because the comparison is
    simply never true, and two spellings are two chances to guess wrong.

    Neither is restated here. The key is read out of the workflow and held
    against the block `Pulumi.yaml` declares, and the login comes from the
    census rather than from a literal typed in this file.
    """
    workflow = NOOP_AUTOMERGE.read_text()
    classify = cast('dict[str, object]', yaml.safe_load(workflow)['jobs']['classify'])
    steps = cast('list[dict[str, str]]', classify['steps'])

    deciding = [step for step in steps if BLOCK_REMOVED_BEFORE_COMPARING.search(step.get('run', ''))]
    assert len(deciding) == 1, f'{len(deciding)} steps of `classify` strip a key before comparing; the admission is one'
    assert 'Pulumi.yaml' in deciding[0]['run'], 'the admission compares some document other than Pulumi.yaml'
    (removed,) = BLOCK_REMOVED_BEFORE_COMPARING.findall(deciding[0]['run'])
    declared = cast('dict[str, object]', yaml.safe_load(PULUMI_YAML.read_text())[removed])

    assert set(declared) == set(_declared_packages()), (
        f'the admission compares the two revisions with `{removed}` removed, which is not the block sdks/ is generated from'
    )
    # A verdict that consults nothing the step decided is the same dead route
    # as a login nothing declares: the step runs, and its answer goes nowhere.
    assert f'steps.{deciding[0]["id"]}.outputs.' in workflow, "nothing reads the admission step's verdict"
    assert workflow.count(conventions.forge.RENOVATE.login) == 1, (
        'the login the admission tests the author against is spelled here more or less than once'
    )


#: How the candidacy test asks for a list GitHub pages, and the reader that
#: stops at that list's first page. `gh pr view --json files` answers with at
#: most a hundred paths and reports nothing about the rest, so a verdict taken
#: on it is a verdict about whatever sorted early; `gh api --paginate` walks
#: every page. The capped form is matched in any `--json` list it is asked for
#: alongside, and `changedFiles` -- how many there were -- is not it.
PAGED_READ = re.compile(r'gh api\b[^\n]*--paginate\b')
CAPPED_FILE_READ = re.compile(r'--json\s+(?:\w+,)*files\b')

#: The refusal `classify` writes, spelled as its step writes it.
REFUSAL_IN_CLASSIFY = 'echo \'noop=false\' >> "$GITHUB_OUTPUT"'


def test_the_candidacy_test_reads_every_changed_path_or_refuses() -> None:
    """A path the verdict never saw is the one that merges a deploy unattended.

    `classify` decides candidacy by grepping a pull request's changed paths for
    the ones a stack program reads, so the verdict is worth exactly what that
    list is. Read from the API's first page it covers at most a hundred paths
    and says nothing about the rest -- a pull request with a hundred
    early-sorting paths can then carry a `src/` file the grep never sees, and
    be admitted as a no-op by the job whose whole purpose is the fence.

    Three things keep the list honest, and each is silent on its own: the read
    pages, what arrived is counted against the number the pull request itself
    reports, and every way out of the step short of the verdict refuses. The
    last is what the count is for -- a short list still greps clean, so
    noticing it only helps if noticing it stands the merge down.
    """
    workflow = NOOP_AUTOMERGE.read_text()
    classify = cast('dict[str, object]', yaml.safe_load(workflow)['jobs']['classify'])
    steps = cast('list[dict[str, str]]', classify['steps'])

    # The verdict names its own step: `noop=true` is written in one place.
    (deciding,) = [step for step in steps if 'noop=true' in step.get('run', '')]
    # What the shell runs, which is what decides anything; a comment is free to
    # name the capped reader in order to say why it is not used.
    code = '\n'.join(line for line in deciding['run'].splitlines() if not line.lstrip().startswith('#'))

    assert PAGED_READ.search(code), 'the candidacy test reads the changed paths without paging them'
    assert not CAPPED_FILE_READ.search(code), (
        "the candidacy test reads the changed paths through a call that stops at the list's first page"
    )
    assert 'changedFiles' in code, 'nothing holds the list the candidacy test read against how long it should be'

    lines = [line.strip() for line in code.splitlines() if line.strip()]
    leaving = [index for index, line in enumerate(lines) if line.startswith('exit ')]
    assert leaving, 'no route out of the candidacy test refuses, so a read that fell short cannot'
    assert all(lines[index - 1] == REFUSAL_IN_CLASSIFY for index in leaving), (
        'a route out of the candidacy test leaves without refusing'
    )


def test_nothing_under_packages_is_a_bridged_sdk() -> None:
    """`packages/` is what this repository authors; a bridged SDK belongs under `sdks/`.

    Held by what a bridged SDK is -- one whose plugin is the bridge -- rather
    than by listing the directories, so a fourth provider added on the wrong
    side of the line fails by name. A member with no plugin file at all is a
    package someone wrote, which is what the directory is for.
    """
    bridged = [
        path.name
        for path in PACKAGES.iterdir()
        if path.is_dir() and (plugin := _generated_plugin(path)) is not None and plugin['name'] == BRIDGE
    ]

    assert bridged == [], f'generated by the bridge and committed under packages/ rather than sdks/: {bridged}'


# --------------------------------------------------------------------------
# The stack census.
# --------------------------------------------------------------------------


def test_every_stack_in_the_census_has_a_program_and_nothing_else_does() -> None:
    """The dispatch table is keyed by the census, both ways.

    A field added to `StackNames` with no program behind it is a stack the
    `credentials` command delivers into and `pulumi up` refuses to declare; a
    program keyed by anything but the census is one the `credentials` command
    refuses to deliver to.
    """
    from kluster import stacks

    assert set(stacks.STACKS) == set(conventions.STACK_NAMES.names())


def test_no_two_stacks_share_a_name() -> None:
    # Two fields spelled alike collapse into one key of the dispatch table, and
    # the program the first one named becomes unreachable without an error.
    names = conventions.STACK_NAMES.names()
    assert len(names) == len(set(names)), names


def test_every_stack_encrypted_apart_is_a_stack_of_the_census() -> None:
    # A key outside the census is a passphrase the `credentials` command
    # generates and escrows for a stack nothing declares.
    assert set(pulumi_config.APART) <= set(conventions.STACK_NAMES.names())


# --------------------------------------------------------------------------
# The stack outputs.
# --------------------------------------------------------------------------
# `conventions.PHYSICAL_OUTPUTS` is the one spelling of the `physical` stack's
# export names; the exporter and every reader take theirs from it. That the
# program exports the structure's names and nothing else, and that what one
# program reads the other exports, are held where the programs run
# (`test_physical_stack`). What is left for the table itself is the relations
# between its entries.


def test_every_continuous_integration_member_has_an_identity_export_and_nothing_else_does() -> None:
    """The identity table is keyed by the roster, both ways.

    A roster member with no export would join no job, having no secret to be
    pushed; an export naming a member the roster does not carry would index an
    identity the overlay never generated, and fail the run there.
    """
    assert set(conventions.PHYSICAL_OUTPUTS.ci_identity) == set(conventions.overlay.CI_MEMBERS)


def test_no_two_outputs_share_a_name() -> None:
    """Two fields spelled alike would be one export, the second write winning silently."""
    names = conventions.PHYSICAL_OUTPUTS.names()

    assert len(names) == len(set(names)), names


# --------------------------------------------------------------------------
# The backup retention classes.
# --------------------------------------------------------------------------
# `conventions.backup.max_age` is the one rule for when a scheduled backup is
# stale, and a retention class's `max_age` is that rule's answer for its
# `period`, spelled in the alert rules' duration syntax. The rule is a
# computation, so the values it has to keep producing are held as literals;
# the classes are a census of the module, so a class is held to the rule from
# the day it is declared; and the period is held to the cron line beside it,
# so the rule cannot be fed a cadence the scheduler does not run.

#: The duration syntax a retention class's threshold is written in: a count
#: and a unit, the units being the ones the alert rules read.
DURATION = re.compile(r'^(\d+)([smhdw])$')
UNIT = {
    's': dt.timedelta(seconds=1),
    'm': dt.timedelta(minutes=1),
    'h': dt.timedelta(hours=1),
    'd': dt.timedelta(days=1),
    'w': dt.timedelta(weeks=1),
}


def _duration(text: str) -> dt.timedelta:
    match = DURATION.match(text)
    assert match, f'{text!r} is not a duration the alert rules read'
    return int(match.group(1)) * UNIT[match.group(2)]


#: Every retention class the module declares, found rather than listed.
RETENTION = [value for value in vars(backup).values() if isinstance(value, backup.RetentionClass)]

#: What a cron line looks like for each cadence a class may state: one time
#: of day on every day, or one time of day on one day of the week. A class
#: with a cadence this table lacks fails below by name, and the row it needs
#: is written here, not guessed.
CRON_SHAPES = {
    dt.timedelta(days=1): re.compile(r'^\d+ \d+ \* \* \*$'),
    dt.timedelta(weeks=1): re.compile(r'^\d+ \d+ \* \* [0-6]$'),
}


def test_stale_is_one_and_a_half_periods() -> None:
    # A run that is merely late is inside it; a run that was missed is half a
    # period overdue by the time it runs out.
    assert backup.max_age(dt.timedelta(days=1)) == dt.timedelta(hours=36)
    assert backup.max_age(dt.timedelta(hours=1)) == dt.timedelta(minutes=90)
    assert backup.max_age(dt.timedelta(weeks=1)) == dt.timedelta(hours=252)


@pytest.mark.parametrize(
    ('delta', 'text'),
    [
        (dt.timedelta(hours=36), '36h'),
        (dt.timedelta(hours=252), '252h'),
        (dt.timedelta(weeks=1), '1w'),
        (dt.timedelta(minutes=90), '90m'),
    ],
)
def test_a_duration_is_spelled_in_the_largest_unit_that_divides_it(delta: dt.timedelta, text: str) -> None:
    # The syntax has no fractions, so ten and a half days is hours, not days.
    assert backup.duration(delta) == text
    assert _duration(text) == delta


def test_a_duration_below_a_second_has_no_spelling() -> None:
    with pytest.raises(ValueError, match='whole number of seconds'):
        _ = backup.duration(dt.timedelta(milliseconds=500))


def test_the_census_found_the_classes() -> None:
    assert RETENTION, 'no RetentionClass at module scope: the census walked nothing'
    assert set(RETENTION) == set(backup.RETENTION_CLASSES)


@pytest.mark.parametrize('retention', RETENTION, ids=lambda r: r.name)
def test_every_class_s_threshold_is_the_rule_s_answer_for_its_period(retention: backup.RetentionClass) -> None:
    # Every class, not the daily ones: the alert rules and the appliance's
    # dump-age probe agree on stale only if no row carries a margin of its own.
    assert _duration(retention.max_age) == backup.max_age(retention.period), retention.name


@pytest.mark.parametrize('retention', RETENTION, ids=lambda r: r.name)
def test_every_class_s_period_is_what_its_cron_line_says(retention: backup.RetentionClass) -> None:
    # Two spellings of one cadence: the cron line the scheduler reads and the
    # period the threshold is derived from. Held the way the appliance's
    # timer is held to its period (`test_probe.py`), so a period cannot be
    # chosen to make the rule produce a threshold someone had in mind.
    shape = CRON_SHAPES.get(retention.period)
    assert shape is not None, f'{retention.name} runs every {retention.period}, a cadence CRON_SHAPES has no row for'
    assert shape.match(retention.schedule), f'{retention.name}: {retention.schedule!r} is not a {retention.period} line'


# --------------------------------------------------------------------------
# The glossary.
# --------------------------------------------------------------------------
# `conventions`' module docstring keeps the vocabulary (style/README.md under
# "Naming"): a term per concept, and under `Not:` the words a diff does not
# introduce for it. It is prose, so nothing stops it naming a term the tree
# never uses or refusing a word the package itself goes on using. The one side
# it is not the source of is the package's own text -- its identifiers, its
# docstrings and its comments -- which is what it is held against here.

CONVENTIONS = Path(conventions.__file__).parent

#: Where the glossary starts in the module docstring: the heading and its
#: underline, on lines of their own.
GLOSSARY_HEADING = 'Glossary\n--------\n'
#: An underlined heading, which is how the docstring opens a section.
SECTION_HEADING = re.compile(r'^\S.*\n-{3,}$', re.MULTILINE)
#: A code span in prose quotes a value or an identifier -- the wire label
#: `zt`, a retired `GW_*` -- and is not the prose using the word.
CODE_SPAN = re.compile(r'`[^`\n]*`')
#: A word of prose. A hyphenated compound is one word, so `lan-gw` in a
#: comment is not the prose using `gw`.
PROSE_WORD = re.compile(r'[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*')
#: The pieces of one `_`-separated part of an identifier: `ZtMember` is `Zt`
#: and `Member`, `ADGUARD_API_PORT` is three, `IPv4Address` splits into
#: fragments that match no word anyone would list.
CAMEL_PIECE = re.compile(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+')


class GlossaryEntry(NamedTuple):
    """One entry: the term, and the words the entry refuses for it."""

    term: str
    refused: tuple[str, ...]


class Words(NamedTuple):
    """One run of words the package uses: an identifier's pieces, or a docstring's or comment's prose."""

    file: str
    words: tuple[str, ...]


def _glossary(doc: str) -> list[GlossaryEntry]:
    """The entries of the glossary section, in order.

    A headword is an unindented line whose next line is indented; the indented
    lines below it are the entry. The preamble is unindented too, and is
    passed over because no indented line follows any line of it.

    The glossary has to be the docstring's last section, because the scan
    below leaves out everything from its heading on: a section after it would
    be prose nobody reads for refused words.
    """
    _, found, section = doc.partition(GLOSSARY_HEADING)
    assert found, 'the conventions docstring carries no glossary section'
    assert not SECTION_HEADING.search(section), 'the glossary is not the last section of the docstring'
    lines = section.splitlines()
    entries: list[GlossaryEntry] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line or line[0].isspace() or index == len(lines) or not lines[index].startswith('    '):
            continue
        body: list[str] = []
        while index < len(lines) and lines[index].startswith('    '):
            body.append(lines[index].strip())
            index += 1
        entries.append(GlossaryEntry(line.strip(), _refused(line.strip(), body)))
    return entries


def _refused(term: str, body: list[str]) -> tuple[str, ...]:
    """The words an entry's `Not:` lists: from the colon to the line ending in a period.

    Read to the period rather than to the end of the line, because the entries
    wrap at the docstring's width and a list read one line deep would drop
    whatever the wrap carried over -- half a phrase, silently, or a trailing
    comma's empty word, which as a phrase of no words is in every run.
    """
    for start, text in enumerate(body):
        if not text.startswith('Not:'):
            continue
        end = next((at for at in range(start, len(body)) if body[at].endswith('.')), len(body) - 1)
        listed = ' '.join(body[start : end + 1]).removeprefix('Not:').rstrip('.')
        refused = tuple(word.strip() for word in listed.split(','))
        assert all(refused), f'{term}: the Not: line lists an empty word: {listed!r}'
        return refused
    return ()


def _phrase(text: str) -> tuple[str, ...]:
    """A term or a refused phrase as the words it has to appear as."""
    return tuple(word.lower() for word in PROSE_WORD.findall(text))


def _identifier_words(name: str) -> tuple[str, ...]:
    return tuple(piece.lower() for part in name.split('_') for piece in CAMEL_PIECE.findall(part))


def _prose_words(text: str) -> tuple[str, ...]:
    """The words of a docstring or a comment, its code spans left out.

    The glossary section itself is left out too: it is the one place that names
    the words it refuses.
    """
    text = text.partition(GLOSSARY_HEADING)[0]
    return tuple(word.lower() for word in PROSE_WORD.findall(CODE_SPAN.sub(' ', text)))


def _vocabulary(package: Path) -> list[Words]:
    """Every run of words a module of the package uses, tagged with its file.

    Identifiers come from the syntax tree: names, attributes, definitions,
    parameters, keywords and imports. Prose is every string that stands as a
    statement of its own -- a module, class, function or attribute docstring --
    and every block of comments; a string that is a value is a value, not the
    package speaking. The runs are kept apart so that a phrase has to occur
    within one identifier or one piece of prose to count -- and a block of
    comment lines is one piece, because that is how a `#:` attribute doc
    wraps, and a phrase broken across two of its lines is still the package
    using it.
    """
    runs: list[Words] = []
    for path in sorted(package.glob('*.py')):
        file = path.name
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            names: list[str] = []
            match node:
                case ast.Name(id=name) | ast.Attribute(attr=name) | ast.arg(arg=name):
                    names.append(name)
                case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name) | ast.ClassDef(name=name):
                    names.append(name)
                case ast.keyword(arg=str(name)) | ast.alias(name=name):
                    names.append(name)
                case ast.ImportFrom(module=str(module)):
                    names.extend(module.split('.'))
                case ast.Expr(value=ast.Constant(value=str(prose))):
                    runs.append(Words(file, _prose_words(prose)))
                case _:
                    pass
            runs.extend(Words(file, _identifier_words(name)) for name in names)
        runs.extend(Words(file, _prose_words(block)) for block in _comment_blocks(source))
    return runs


def _comment_blocks(source: str) -> Iterator[str]:
    """Each run of comment lines, joined: consecutive lines starting at one column are one block."""
    block: list[str] = []
    last: tuple[int, int] | None = None
    for token in tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__):
        if token.type != tokenize.COMMENT:
            continue
        row, column = token.start
        if block and last != (row - 1, column):
            yield ' '.join(block)
            block = []
        block.append(token.string.lstrip('#'))
        last = (row, column)
    if block:
        yield ' '.join(block)


def _uses(run: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    """Whether the phrase occurs in the run as consecutive whole words."""
    return any(run[start : start + len(phrase)] == phrase for start in range(len(run) - len(phrase) + 1))


def _files_using(phrase: tuple[str, ...], vocabulary: Iterable[Words]) -> list[str]:
    return sorted({run.file for run in vocabulary if _uses(run.words, phrase)})


def test_the_glossary_names_terms_the_package_uses_and_refuses_words_it_does_not(tmp_path: Path) -> None:
    """Each term is a word the package uses; each refused word is one it does not.

    The first half keeps a term nobody uses out of the glossary; the second
    keeps the package that defines the vocabulary from using the words it
    refuses. Neither is a mirror: the glossary is prose the reviewer reads,
    and the package's identifiers, docstrings and comments are text the
    glossary does not generate.

    The scan is exercised on a module written here first, one refused word in
    each channel it reads and one phrase wrapped across two comment lines, so
    a channel it stopped reading fails this case rather than leaving the
    second half silent.
    """
    control = tmp_path / 'control.py'
    control.write_text(
        '"""A docstring saying refusedone, and `refusedfour` in a code span."""\n'
        '# a comment saying refusedtwo, and hyphen-refusedfive, then wrapped\n'
        '# refusedseven on the next line of the same block\n'
        'REFUSED_THREE = "refusedsix"\n'
    )
    reached = _vocabulary(tmp_path)
    assert _files_using(('refusedone',), reached) == ['control.py']
    assert _files_using(('refusedtwo',), reached) == ['control.py']
    assert _files_using(('refused', 'three'), reached) == ['control.py']
    assert _files_using(('wrapped', 'refusedseven'), reached) == ['control.py']
    for quoted in ('refusedfour', 'refusedfive', 'refusedsix'):
        assert _files_using((quoted,), reached) == [], quoted

    entries = _glossary(cast(str, conventions.__doc__))
    assert entries, 'the glossary section carries no entry'
    vocabulary = _vocabulary(CONVENTIONS)

    unused = [entry.term for entry in entries if not _files_using(_phrase(entry.term), vocabulary)]
    assert not unused, f'the glossary names a term the conventions package never uses: {unused}'

    used = {
        word: files for entry in entries for word in entry.refused if (files := _files_using(_phrase(word), vocabulary))
    }
    assert not used, f'the conventions package uses a word its own glossary refuses: {used}'
