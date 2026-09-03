"""The route census's invariants: the convention-table checks for one table.

They are the same kind of assertion `test_conventions.py` makes about the
other convention tables (rfc-002 §10.2) -- a relation between the entries of a
static table, checked here and nowhere at runtime, because nothing can break
one that this suite did not already catch. `Route` makes a row's fields
correct together; what a row's type cannot express is a relation between rows,
or between a row and the zones the installation declares.

Each invariant is a predicate over a table, and each case applies it twice: to
`conventions.ROUTES`, and to a row that breaks it. The census is empty until
`apps` declares its first route, so a case that only inspected `ROUTES` would
pass by having nothing to inspect -- and would keep passing if the predicate
stopped checking anything at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from kluster import conventions
from kluster.conventions.routes import SELF, Route, Srv

#: A DNS label: letters, digits and interior hyphens, and no dot at all.
LABEL = re.compile(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?\Z')


def _unknown_zones(routes: Iterable[Route]) -> set[str]:
    return {zone for route in routes for zone in route.zones} - set(conventions.ALL_ZONES)


def _duplicated_names(routes: Iterable[Route]) -> list[tuple[str, str]]:
    published = [(route.host, zone) for route in routes for zone in route.zones]
    return sorted({name for name in published if published.count(name) > 1})


def _hosts_that_are_not_labels(routes: Iterable[Route]) -> set[str]:
    return {route.host for route in routes if not LABEL.fullmatch(route.host)}


def test_every_zone_a_row_names_is_one_the_installation_declares() -> None:
    """A misspelled zone is silent in both directions.

    `dns` writes rewrites for a domain nobody serves, and `apps` finds no zone
    id to declare the public record against -- neither is an error anything
    else reports.
    """
    assert _unknown_zones(conventions.ROUTES) == set()
    assert _unknown_zones([Route(host='photos', zones=('ucw.pdh',))]) == {'ucw.pdh'}


def test_no_two_rows_publish_the_same_host_in_the_same_zone() -> None:
    """One name is one application.

    Two rows on it are two records and two rewrites for the same name, and
    which application answers depends on the order the census happens to be
    in.
    """
    duplicated = [
        Route(host='photos', zones=('ucw.phd',)),
        Route(host='photos', zones=('ucw.phd', 'peifeng.phd')),
    ]

    assert _duplicated_names(conventions.ROUTES) == []
    assert _duplicated_names(duplicated) == [('photos', 'ucw.phd')]


def test_a_rows_host_is_a_label_and_not_a_fully_qualified_name() -> None:
    """The row is fanned out across its zones, so it may not carry one.

    A host written out in full would publish `photos.ucw.phd.ucw.phd` in every
    zone the row names.
    """
    assert _hosts_that_are_not_labels(conventions.ROUTES) == set()
    assert _hosts_that_are_not_labels([Route(host='photos.ucw.phd')]) == {'photos.ucw.phd'}


def test_a_row_is_published_in_the_primary_zone_alone_unless_it_says_otherwise() -> None:
    """Every zone a name is published in costs a certificate to cover it.

    So the zones a row reaches are what its owner asked for: a default that
    fanned every name across every zone would buy the whole set for a name one
    audience uses, and a LAN-only name -- which no public resolver answers --
    would buy one wildcard per zone for nothing.
    """
    assert Route(host='photos').zones == conventions.PRIMARY_ONLY


def test_a_row_states_what_it_publishes_beside_its_name() -> None:
    """An application that publishes more than a hostname says so in its row.

    The alternative is a published record that lives in neither this census nor
    the `dns` tables, findable only by reading the components. The target is
    the row itself rather than the hostname written out, so the name is spelled
    once and a rename cannot leave the two halves disagreeing.
    """
    matrix = Route(
        host='matrix', proxied=False, extras=(Srv('_matrix-identity._tcp', priority=10, weight=0, port=443),)
    )

    assert matrix.extras[0].target is SELF
    assert Route(host='photos').extras == ()
