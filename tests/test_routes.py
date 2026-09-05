"""The route census's invariants: the convention-table checks for one table.

They are the same kind of assertion `test_conventions.py` makes about the
other convention tables (rfc-002 §10.2) -- a relation between the entries of a
static table, checked here and nowhere at runtime, because nothing can break
one that this suite did not already catch. `conventions.routes.Route` makes a
row's fields correct together; what a row's type cannot express is a relation
between rows, or between a row and the zones the installation declares.

Each invariant is a predicate over a table, and each case applies it twice: to
`conventions.routes.ROUTES`, and to a row that breaks it. The census is empty
until `apps` declares its first route, so a case that only inspected `ROUTES`
would pass by having nothing to inspect -- and would keep passing if the
predicate stopped checking anything at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from kluster import conventions

#: A DNS label's shape: letters, digits and interior hyphens, and no dot at all.
LABEL = re.compile(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?\Z')

#: A DNS label's maximum length, in octets (RFC 1035 §2.3.4), which the shape
#: above does not bound. Counted with `.encode()` because the limit is octets
#: and not characters; the names a row publishes are ASCII either way -- a host
#: by the shape check, a service label by its `_service._proto` form -- so the
#: two counts agree here.
LABEL_MAX_OCTETS = 63


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
    return {route.host for route in routes if not LABEL.fullmatch(route.host)}


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
        if any(len(label.encode()) > LABEL_MAX_OCTETS for label in name.split('.'))
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
    """
    assert conventions.routes.Route(host='photos').zones == conventions.PRIMARY_ONLY


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
