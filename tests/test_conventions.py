"""What the censuses say, and the invariants their structures cannot carry.

Every census's pins and invariants are here, whatever program reads the census
(style/pulumi.md). The cases are of two kinds:

A **pin** writes a census's content out in literals typed here and read from
nowhere. Once a census is the single source every reader agrees through, a
check that holds one reader against another moves with a rename and passes by
construction -- it stops being able to fail rather than failing. The literal is
the second source that gives such a comparison something to be wrong about, and
it is what makes editing a census cost a second line here.

An **invariant** is a relation between the *entries* of a table -- a node that
exists, a mount claimed once, an address inside the subnet it belongs to. A
dataclass makes a pair that must agree impossible to write apart inside one
row; across rows nothing can. They are checked here because the tables are
static code: nothing can break one at runtime that this suite did not already
catch.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from ipaddress import IPv6Network
from pathlib import Path

from kluster import conventions

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

#: Which node carries the dedicated VIP, written down here as well as in the
#: census. The volume that follows the VIP resolves its node *through*
#: `DEDICATED_VIP_NODE`, so comparing the two is comparing a value with itself:
#: it agrees with whatever the census says and can report no move at all. A
#: literal is the second source that gives the comparison something to be
#: wrong about — moving the VIP is then a two-line edit, and the second line is
#: a reader deciding that the dataset may move with it.
DEDICATED_VIP = 'cp1'


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


# The gateway census's content is pinned outside this file, by hand and row by
# row: `test_device_services` restates every bridged service's name and address
# in `ADDRESSES`, and `tests/data/gw-config-caddyfile` transcribes the
# configuration the device serves today, which is where each legacy row's label
# and upstream are held. A service's `artifact` is reached separately, through
# the image repository names `test_physical_stack` pins. Both of those go when
# the device does, and this note is what has to move with them. A legacy row's
# retirement wave is carried by none of them.


def test_every_bridged_service_sits_on_the_container_vlan() -> None:
    """The unit places a service by injecting this address with the VLAN's prefix.

    An address outside the subnet would be configured onto the interface and
    reach nothing, and the resolvers are what every lease on the LAN points at.
    """
    for service in conventions.gateway.SERVICES:
        if isinstance(service, conventions.gateway.BridgedService):
            assert service.address in conventions.CONTAINER_VLAN.v4


def test_the_zone_census_names_the_domains_the_installation_serves() -> None:
    """Every zone written out, because every other reader agrees through the census.

    The names the gateway serves, the rewrites `dns` writes, the wildcards the
    proxy holds certificates for and the names an application publishes are all
    built by joining a label to one of these. Each of those therefore agrees
    with whatever the census says, and a domain edited here would move every
    one of them in the same edit with nothing going red. These literals are the
    second source: a zone this installation starts or stops serving is two
    edits, and the second is a reader deciding the pin moves too.
    """
    assert conventions.ZONE_PRIMARY == 'unlimited-code.works'
    assert conventions.ZONE_SHORT == 'ucw.phd'
    assert conventions.PRIMARY_ONLY == ('unlimited-code.works',)
    assert conventions.WEB_ZONES == ('unlimited-code.works', 'unlimitedcodeworks.xyz')
    assert conventions.PARKED_ZONES == ('peifeng.phd', 'ucw.phd')
    assert conventions.ZONE_FAMILY == ('jiahui.id', 'jiahui.love')
    assert conventions.ALL_ZONES == (
        'unlimited-code.works',
        'unlimitedcodeworks.xyz',
        'peifeng.phd',
        'ucw.phd',
        'jiahui.id',
        'jiahui.love',
    )
    # The retiring LAN zone is a name inside `ZONE_SHORT` rather than a zone at
    # the registrar, and the second wildcard the proxy holds a certificate for.
    assert conventions.gateway.ZONE_LEGACY == 'lan.ucw.phd'


def test_every_name_the_gateway_serves_is_one_label_under_the_primary_zone() -> None:
    """The gateway holds one wildcard certificate, and a wildcard covers one label.

    So a vhost deeper than that — or in another zone — is a name its own
    Caddyfile would serve from no site block at all (rfc-002 §9.3). The census
    is where such a name would be introduced, which is where the rule belongs.

    What this crosses is a row that spells its name out, which the field's type
    admits. Every row today builds one by joining a label to `ZONE_PRIMARY`, so
    for those the comparison holds a value against what it was built from and
    can report no move at all; the zone itself is pinned by the literal above,
    which is what covers that half.
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

    `LegacyVhost.host` is the label joined to `ZONE_LEGACY`, so the zone half
    of this comparison is the census against itself and only the label half can
    fail -- a row whose label carries a dot. `ZONE_LEGACY` is pinned by the
    literal above; what holds the rendered names themselves is
    `test_device_services`, against the configuration the device serves today.
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
    repositories = {conventions.gateway.image_repository(s.artifact) for s in conventions.gateway.SERVICES}
    assert len(repositories) == 3


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

    The node the sentinel resolves to is checked against `DEDICATED_VIP`, the
    literal above, rather than against the census that computes it — the
    expectation comes from outside the census, which is the only thing that
    makes the equality able to fail. The two assertions either side of it are
    the property that survives a move: the volume states the sentinel, and a
    volume that states a node keeps it.
    """
    hath = conventions.NODE_VOLUMES['hath-cache']

    assert hath.node is conventions.FOLLOWS_DEDICATED_VIP
    assert hath.attached_node == DEDICATED_VIP
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

    Written out rather than compared with `conventions.PRIMARY_ONLY`, which is
    the value the default is: that comparison agrees with any default the
    census is given, including every zone.
    """
    assert conventions.routes.Route(host='photos').zones == ('unlimited-code.works',)


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
# pushes secrets into what it names, so both agree with whatever it says: the
# content is pinned here, in literals. The cases that read the workflow files
# are the seam this census still crosses -- a workflow is text no import
# reaches, so what it names and what the census carries are two sources that
# can disagree.

WORKFLOWS = Path(__file__).parent.parent / '.github' / 'workflows'


def _workflows() -> list[Path]:
    """Every workflow file GitHub would run, which is both spellings of the suffix.

    GitHub reads `.yml` and `.yaml` out of this directory identically, so a
    census that globs one of them is one a file named the other way is invisible
    to -- and invisible is the state every case that uses it exists to prevent.
    """
    return sorted(WORKFLOWS.glob('*.yml')) + sorted(WORKFLOWS.glob('*.yaml'))


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

#: The other way GitHub spells the same identity. `conventions.forge.Author`
#: carries a login and no id and says why, so a workflow reaching for the id
#: form is outside what the census covers -- and this is what makes that a red
#: check rather than a workflow that slipped past the scan above.
AUTHOR_BY_ID = re.compile(r'\.(?:user|sender)\.id\b|\.actor_id\b')


def test_the_census_names_the_two_repositories_and_what_the_plan_gives_each() -> None:
    """Visibility is the flag the plan's public-only features are derived from.

    A repository recorded under the wrong one asks for a feature it cannot
    have, or declines one it could.
    """
    assert {repository.name: repository.public for repository in conventions.forge.REPOSITORIES} == {
        'kluster': True,
        'kluster-ops': False,
    }
    assert conventions.forge.DEPLOYMENT.plan_offers_public_features is True
    assert conventions.forge.OPS.plan_offers_public_features is False
    assert conventions.forge.ACCOUNT == conventions.forge.Account(login='Aetf', user_id=1519759)
    assert conventions.forge.DEPLOYMENT.full_name == 'Aetf/kluster'


def test_the_census_carries_the_environments_the_merge_chain_runs() -> None:
    """The credential partition, written out: which Environments exist and what each admits.

    Order is part of it -- the credentials command reads this tuple as the
    order the chain runs in -- and so is the branch policy, which is what keeps
    a credential that can root the gateway off a pull request's own branch.
    """
    protected, any_branch = conventions.forge.BranchPolicy.PROTECTED_ONLY, conventions.forge.BranchPolicy.ANY_BRANCH

    assert [
        (environment.name, environment.branches, environment.gated)
        for environment in conventions.forge.DEPLOYMENT.environments
    ] == [
        ('physical-plan', protected, False),
        ('physical', protected, True),
        ('dns', any_branch, False),
        ('k8s-base', any_branch, False),
        ('apps', any_branch, False),
    ]
    # Ungated, because the drill's scope is the gate (credentials.md §4) and
    # this plan offers a private repository none anyway.
    assert conventions.forge.OPS.environments == (conventions.forge.DRILL,)
    assert conventions.forge.DRILL == conventions.forge.Environment('drill', any_branch, gated=False)


def test_every_label_a_workflow_branches_on_is_one_the_census_carries() -> None:
    """A label a workflow reads and nothing declares fails in the quietest way there is.

    The condition is simply never true, so the behaviour it guards is
    unavailable at the moment somebody needs it and nothing anywhere reports
    that. Reading the workflows is what keeps the census from being shorter
    than what they depend on.
    """
    declared = {label.name for repository in conventions.forge.REPOSITORIES for label in repository.labels}
    read = {label for workflow in _workflows() for label in LABEL_IN_A_CONDITION.findall(workflow.read_text())}

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
        for workflow in _workflows()
        for match in AUTHOR_IN_A_CONDITION.findall(workflow.read_text())
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
    reached = {workflow.name for workflow in _workflows() if AUTHOR_BY_ID.search(workflow.read_text())}

    assert not reached, f'identifies an account by id, which conventions.forge.Author does not carry: {sorted(reached)}'
