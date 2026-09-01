"""The route census's invariants, which a row's own type cannot carry.

`Route` makes a row's fields correct together; what it cannot express is a
relation between rows, or between a row and the zones the estate declares.
The census is static code, so those are checked here and nowhere at runtime,
the way the other convention tables are (test_conventions.py, rfc-002 §10.2).

Each invariant is a predicate over a table, and each case applies it twice:
to `conventions.ROUTES`, and to a row that breaks it. The census is empty
until `apps` declares its first route, so a case that only inspected `ROUTES`
would pass by having nothing to inspect -- and would keep passing if the
predicate stopped checking anything at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from kluster import conventions
from kluster.conventions.routes import Route

#: A DNS label: letters, digits and interior hyphens, and no dot at all.
LABEL = re.compile(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?\Z')


def _unknown_zones(routes: Iterable[Route]) -> set[str]:
    return {zone for route in routes for zone in route.zones} - set(conventions.ALL_ZONES)


def _duplicated_names(routes: Iterable[Route]) -> list[tuple[str, str]]:
    published = [(route.host, zone) for route in routes for zone in route.zones]
    return sorted({name for name in published if published.count(name) > 1})


def _hosts_that_are_not_labels(routes: Iterable[Route]) -> set[str]:
    return {route.host for route in routes if not LABEL.fullmatch(route.host)}


def test_every_zone_a_row_names_is_one_the_estate_declares() -> None:
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
