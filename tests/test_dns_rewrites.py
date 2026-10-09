"""The rule list: which rows a census implies, and how rows render.

Plain data, and no runtime: what a census implies is a function of its rows,
and what a row renders as is a function of the row. Each derivation is held
against literals for hand-built rows, since a derivation can move with no new
value typed anywhere (style/pulumi.md). How the program hands the result to
each instance is `test_dns_stack.py`; how the provider writes it is
`test_dns_adguard.py`.
"""

import importlib
from ipaddress import IPv4Address, IPv6Address

import pytest

from kluster import conventions
from kluster.components.dns.base import overlay_records
from kluster.components.dns.resolver_settings import UPSTREAMS, upstream_dns
from kluster.components.dns.rewrites import (
    HEADER,
    Blocklist,
    Rewrite,
    RuleBlock,
    UnusableRow,
    blocklist_clients,
    gateway_rewrites,
    legacy_vhost_rewrites,
    overlay_rewrites,
    rewrites,
    user_rules,
)
from kluster.providers.adguard import Client

PROXY = conventions.gateway.BridgedService(name='proxy', address=IPv4Address('192.0.2.80'), artifact='caddy')

#: A client as `AdGuardClients` takes one, every field but its name beside the point here.
CONSOLE: Client = {
    'name': 'Console',
    'ids': ['02:00:00:00:00:01'],
    'tags': [],
    'upstreams': [],
    'use_global_settings': True,
    'filtering_enabled': False,
    'parental_enabled': False,
    'safebrowsing_enabled': False,
    'safe_search': {
        'enabled': False,
        'bing': True,
        'duckduckgo': True,
        'ecosia': True,
        'google': True,
        'pixabay': True,
        'yandex': True,
        'youtube': True,
    },
    'use_global_blocked_services': True,
    'blocked_services': [],
    'blocked_services_schedule': {'time_zone': 'Local'},
    'ignore_querylog': False,
    'ignore_statistics': False,
    'upstreams_cache_enabled': False,
    'upstreams_cache_size': 0,
}


# --------------------------------------------------------------------------
# Rendering.


def test_every_rewrite_renders_with_the_exact_anchor() -> None:
    """`|name^` matches the name and nothing under it.

    The subtree form, `||name^`, would answer every name under a bare label's
    spelling too -- `||tube^` answers the whole `.tube` top-level domain with
    the proxy's address.
    """
    rendered = user_rules([RuleBlock('one', (Rewrite(domain='tube', answer=IPv4Address('192.0.2.80')),))])

    assert rendered == (HEADER, '! one', '|tube^$dnsrewrite=NOERROR;A;192.0.2.80')


def test_the_record_type_is_the_answers_and_no_rule_narrows_the_query_type() -> None:
    """A for an IPv4 address, AAAA for an IPv6 one, CNAME for a name, and no `$dnstype`.

    Without a type restriction a rewrite answers every query type for its
    name, the other types with an empty `NOERROR`, so no query for the name
    reaches an upstream.
    """
    rendered = user_rules(
        [
            RuleBlock(
                'answers',
                (
                    Rewrite(domain='photos.example.test', answer=IPv4Address('192.0.2.1')),
                    Rewrite(domain='photos.example.test', answer=IPv6Address('2001:db8::1')),
                    Rewrite(domain='nas', answer='host.home.arpa'),
                ),
            )
        ]
    )

    assert rendered[2:] == (
        '|photos.example.test^$dnsrewrite=NOERROR;A;192.0.2.1',
        '|photos.example.test^$dnsrewrite=NOERROR;AAAA;2001:db8::1',
        '|nas^$dnsrewrite=NOERROR;CNAME;host.home.arpa',
    )
    assert not any('dnstype' in rule for rule in rendered)


def test_the_list_is_the_header_then_each_block_under_its_source_in_order() -> None:
    """The list renders the same on every run, so any line in it that the program did not render was typed by hand."""
    rendered = user_rules(
        [
            RuleBlock('first', (Rewrite(domain='a.test', answer=IPv4Address('192.0.2.1')),)),
            RuleBlock('second', ()),
            RuleBlock('third', (Rewrite(domain='b.test', answer=IPv4Address('192.0.2.2')),)),
        ]
    )

    assert rendered == (
        HEADER,
        '! first',
        '|a.test^$dnsrewrite=NOERROR;A;192.0.2.1',
        '! second',
        '! third',
        '|b.test^$dnsrewrite=NOERROR;A;192.0.2.2',
    )
    assert HEADER.startswith('! ')


def test_a_blocklist_renders_one_subtree_rule_per_host_naming_its_own_client() -> None:
    """The subtree form, since a blocked host's names under it are blocked too, and `$client=` its own row's client."""
    other: Client = {**CONSOLE, 'name': 'Tablet'}
    rendered = user_rules(
        [
            RuleBlock(
                'blocked',
                (
                    Blocklist(client=CONSOLE, hosts=('update.example.test', 'cdn.example.test')),
                    Blocklist(client=other, hosts=('games.example.test',)),
                ),
            )
        ]
    )

    assert rendered[2:] == (
        '||update.example.test^$client=Console',
        '||cdn.example.test^$client=Console',
        '||games.example.test^$client=Tablet',
    )


def test_the_clients_are_the_ones_the_blocklist_rows_hold_each_once() -> None:
    blocks = [
        RuleBlock('one', (Blocklist(client=CONSOLE, hosts=('a.test',)),)),
        RuleBlock('two', (Rewrite(domain='b.test', answer=IPv4Address('192.0.2.1')), Blocklist(CONSOLE, ('c.test',)))),
    ]

    assert blocklist_clients(blocks) == (CONSOLE,)


@pytest.mark.parametrize(
    'answer',
    ['host.example.test', 'home.arpa', 'host.home.arpa.example.test', 'Host.home.arpa', 'host home.arpa'],
)
def test_a_cname_target_outside_the_device_plane_is_refused_at_construction(answer: str) -> None:
    """An instance resolves a CNAME's target upstream, and only the gateway's resolver answers `home.arpa`."""
    with pytest.raises(UnusableRow, match=r'not a name under home\.arpa'):
        _ = Rewrite(domain='nas', answer=answer)


def test_a_cname_target_on_the_device_plane_is_built() -> None:
    assert Rewrite(domain='nas', answer='host.iot.home.arpa').record_type == 'CNAME'


@pytest.mark.parametrize('domain', ['tube^', 'two words', '', 'a..b', '-lead.test', 'tube$client=x'])
def test_a_name_that_would_end_the_rules_pattern_is_refused(domain: str) -> None:
    with pytest.raises(UnusableRow, match='not a name a rule can match'):
        _ = Rewrite(domain=domain, answer=IPv4Address('192.0.2.1'))


def test_a_client_name_that_would_need_quoting_is_refused() -> None:
    with pytest.raises(UnusableRow, match='bare token'):
        _ = Blocklist(client={**CONSOLE, 'name': 'Living room'}, hosts=('a.test',))


# --------------------------------------------------------------------------
# The derivations.


def test_a_legacy_row_renders_its_name_at_the_proxy_and_a_bare_row_two_more() -> None:
    """The bare label for a client that asks for it, and the label under `home.arpa` for one that appends it first."""
    legacy = (
        conventions.gateway.LegacyVhost(
            label='tube',
            upstream=conventions.gateway.PlainUpstream(host='host.home.arpa', port=8096),
            wave=conventions.gateway.RetirementWave.C,
            bare_name=True,
        ),
        conventions.gateway.LegacyVhost(
            label='spool',
            upstream=conventions.gateway.PlainUpstream(host='host.home.arpa', port=8000),
            wave=conventions.gateway.RetirementWave.B,
        ),
    )

    assert user_rules([RuleBlock('legacy', legacy_vhost_rewrites(legacy, PROXY))])[2:] == (
        '|tube.lan.ucw.phd^$dnsrewrite=NOERROR;A;192.0.2.80',
        '|tube^$dnsrewrite=NOERROR;A;192.0.2.80',
        '|tube.home.arpa^$dnsrewrite=NOERROR;A;192.0.2.80',
        '|spool.lan.ucw.phd^$dnsrewrite=NOERROR;A;192.0.2.80',
    )


def test_the_gateways_own_names_are_the_controllers_and_each_resolvers_at_the_proxy() -> None:
    resolvers = (
        conventions.gateway.BridgedService(
            name='one', address=IPv4Address('192.0.2.3'), artifact='adguard', vhost='one.example.test'
        ),
        conventions.gateway.BridgedService(name='quiet', address=IPv4Address('192.0.2.4'), artifact='adguard'),
    )

    assert gateway_rewrites('console.example.test', resolvers, PROXY) == (
        Rewrite(domain='console.example.test', answer=IPv4Address('192.0.2.80')),
        Rewrite(domain='one.example.test', answer=IPv4Address('192.0.2.80')),
    )


def test_a_public_route_needs_no_rewrite() -> None:
    # LAN clients take the cloud path for it, which is the whole difference.
    assert rewrites([conventions.routes.Route(host='www', exposure=conventions.routes.Exposure.PUBLIC)]) == ()


def test_a_split_route_is_rewritten_in_every_zone_it_is_published_in() -> None:
    route = conventions.routes.Route(
        host='photos', exposure=conventions.routes.Exposure.SPLIT, zones=('ucw.phd', 'peifeng.phd')
    )

    assert rewrites([route]) == (
        Rewrite(domain='photos.ucw.phd', answer=conventions.LAN_POOL.default_vip.v4),
        Rewrite(domain='photos.ucw.phd', answer=conventions.LAN_POOL.default_vip.v6),
        Rewrite(domain='photos.peifeng.phd', answer=conventions.LAN_POOL.default_vip.v4),
        Rewrite(domain='photos.peifeng.phd', answer=conventions.LAN_POOL.default_vip.v6),
    )


def test_both_families_are_rewritten() -> None:
    """A LAN client asking for AAAA gets the VIP's ULA, not an empty answer.

    AdGuard answers the other family of a rewritten name with an empty
    response rather than forwarding it, so a v4-only rewrite leaves the name
    with no IPv6 address on the LAN.
    """
    entries = rewrites(
        [conventions.routes.Route(host='tube', exposure=conventions.routes.Exposure.SPLIT, zones=('ucw.phd',))]
    )

    assert {entry.answer for entry in entries} == {
        conventions.LAN_POOL.default_vip.v4,
        conventions.LAN_POOL.default_vip.v6,
    }
    assert {entry.record_type for entry in entries} == {'A', 'AAAA'}


def test_an_iot_route_is_answered_by_the_media_vip() -> None:
    # Attaching to the media gateway *is* the "IoT may reach this" decision.
    route = conventions.routes.Route(host='tube', exposure=conventions.routes.Exposure.IOT, zones=('ucw.phd',))

    assert {entry.answer for entry in rewrites([route])} == {
        conventions.LAN_POOL.media_vip.v4,
        conventions.LAN_POOL.media_vip.v6,
    }


def test_a_lan_only_route_is_rewrite_only() -> None:
    """No public record, but the name still has to resolve on the LAN.

    Publishing nothing is what keeps the LAN service census out of public
    resolvers; the rewrite is the only thing that makes the name work.
    """
    route = conventions.routes.Route(host='golinks', exposure=conventions.routes.Exposure.LAN_ONLY, zones=('ucw.phd',))

    assert route.public is False
    assert len(rewrites([route])) == 2


def test_the_overlay_rewrites_are_the_overlay_records_both_ways() -> None:
    """For an overlay name there is one answer, whichever path a client takes.

    alice and bob answer from the rewrite and every other resolver from the
    public record, and both are derived from the same roster entry. Held
    against the host block as the `dns` stack publishes it rather than against
    a copy of the roster, so a pair that differs in the name, the address, the
    members or the count fails here -- and a pair on one side only is a name
    that resolves differently depending on who is asked.
    """
    rewritten = {(entry.domain, str(entry.answer)) for entry in overlay_rewrites(conventions.overlay.ROSTER)}
    published = {(record.fqdn(conventions.ZONE_PRIMARY), record.content) for record in overlay_records()}

    assert rewritten == published


def test_every_overlay_rewrite_answers_under_the_domain_the_network_pushes() -> None:
    """The seam between the two halves of the overlay's naming.

    A member that opts in to the network's managed DNS installs a resolver
    scoped to the pushed domain and nothing wider, so a rewrite outside it is
    one no such member ever asks alice or bob for. The domain is read from what
    `physical` pushes rather than from the constant the derivation uses, so
    the two cannot move apart unnoticed.
    """
    pushed = conventions.overlay.MANAGED_DNS.domain

    for entry in overlay_rewrites(conventions.overlay.ROSTER):
        assert entry.domain.endswith(f'.{pushed}'), entry.domain


def test_an_overlay_rewrite_is_one_family() -> None:
    """The overlay is IPv4-only, so an overlay name has an A answer and no other.

    An A rewrite answers AAAA with an empty `NOERROR`, which is what the
    A-only public record produces too; a second family would be an answer the
    public record does not give.
    """
    entries = overlay_rewrites(conventions.overlay.ROSTER)

    assert entries
    assert {entry.record_type for entry in entries} == {'A'}


def test_the_package_does_not_shadow_this_module_with_the_function_it_holds() -> None:
    """`from kluster.components.dns import rewrites` is the module, not the function.

    The package exports `Rewrite` and `ResolverConfiguration` and deliberately
    not the derivation: a package attribute of that name shadows the module
    that defines it. Nothing raises when it does — the import succeeds and
    binds the function, and the failure surfaces at the first attribute lookup
    on it, arbitrarily far away. It has already happened once, so the reason
    recorded in the package docstring is backed by this case rather than by
    care.

    Asked at runtime because that is where the binding is made: the attribute
    exists as a side effect of the package importing from the submodule, which
    a type checker does not see and this case therefore cannot ask statically.
    """
    package = importlib.import_module('kluster.components.dns')
    module = importlib.import_module('kluster.components.dns.rewrites')

    assert package.rewrites is module
    assert 'rewrites' not in package.__all__


# --------------------------------------------------------------------------
# The upstream list.


def test_the_upstream_list_carries_a_comment_above_each_group_as_the_instances_hold_it() -> None:
    """The device plane's line, the reverse zones' lines and the public upstreams, each under its comment.

    The instance keeps `#` lines in the list and reports them back, so a
    declaration without them would rewrite the list on the first apply and
    differ from the live one where the design names no difference.
    """
    upstreams = upstream_dns(forwarded_zones=('home.arpa', '10.in-addr.arpa'), gateway=IPv4Address('192.0.2.1'))

    assert upstreams == [
        '# Forward .home.arpa queries to the DMSE',
        '[/home.arpa/]192.0.2.1',
        '# Forward Reverse DNS (IP-to-Name) for 10.x.x.x',
        '[/10.in-addr.arpa/]192.0.2.1',
        '# Global DNS servers on the Internet',
        *UPSTREAMS,
    ]
