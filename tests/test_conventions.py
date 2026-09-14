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

import base64
import json
import re
from collections.abc import Iterable
from ipaddress import IPv6Network
from pathlib import Path
from typing import NamedTuple, cast

import pytest
import yaml

from kluster import conventions
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
    VLAN's, so the two read as neighbours without ever being one network.
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
    instance before the other (rfc-002 §11.1).
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
#: written. `!=` is matched as well as `==`, because a login misspelt in a
#: negative test fails *open*, which is the worse of the two directions to
#: leave unpinned. Two groups, one per way round, so a match carries the login
#: in whichever of them is not empty.
AUTHOR_IN_A_CONDITION = re.compile(
    r"\.(?:\w+_)?(?:actor|login)\s*[=!]=\s*'([^']+)'|'([^']+)'\s*[=!]=\s*[\w.]*(?:actor|login)\b"
)

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

    The condition is simply never true, so the behaviour it guards is
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

    # noop-automerge's unproven route is renovate's and nobody else's (ci.md
    # §3), so a workflow that stopped naming the login, or a census that
    # stopped carrying it, is what this holds still.
    assert 'renovate[bot]' in read
    assert read <= named, f'compared against by a workflow and named nowhere: {sorted(read - named)}'


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


#: How a workflow step points a `pulumi` command at one stack. Both spellings,
#: because `-s` and `--stack` are the same flag and a census that knew only the
#: long one would be blind to the short.
PULUMI_STACK_FLAG = re.compile(r'(?:--stack[=\s]+|-s\s+)([\w.-]+)')


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

    **This census reads literal flags**, so a stack named through an
    expression -- `--stack ${{ matrix.stack }}`, with the name added to a
    matrix list -- passes it. Widening it to the text is impractical rather
    than merely unwritten: every workflow says `github` many times over
    through `${{ github.* }}`, so a census over the word would be noise. What
    catches that spelling is the other half of the pair, and it catches it at
    run time rather than at review time: the job's Environment holds the
    estate passphrase alone, so the run dies `error: incorrect passphrase`
    with nothing of that stack's config in reach. This case is the cheap,
    early half of a guard whose expensive half cannot be evaded.
    """
    apart = set(pulumi_config.APART)
    named = [
        f'{_name(path)}: {match.group(0)}'
        for path in _workflows_and_actions()
        if 'pulumi' in (text := path.read_text())
        for match in PULUMI_STACK_FLAG.finditer(text)
        if match.group(1) in apart
    ]

    assert named == [], f'a workflow runs `pulumi` against a stack encrypted apart from the estate: {named}'


def test_the_stack_encrypted_apart_is_the_one_the_forge_program_declares() -> None:
    """The two censuses name the same stack rather than agreeing by spelling.

    `pulumi_config.APART` is what makes a `credentials` run reach for the right
    passphrase; the case above is what keeps CI away from that stack. Both are
    about the program that declares the forge, and a rename moving one and not
    the other would leave a green suite and an unguarded stack.
    """
    from kluster.stacks import github

    assert set(pulumi_config.APART) == {github.STACK}


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
