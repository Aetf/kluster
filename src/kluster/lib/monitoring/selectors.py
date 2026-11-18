"""Which series a PromQL expression selects, read off the expression: the walk a component's rules and a script share.

Each selector is read as its metric and the jobs its `job` matcher names
(`Selector`), through GreptimeDB's `promql_parser`. An expression the walk
cannot read whole is refused rather than read short, so a reader never takes
a selector it misread for one that selects nothing: the monitoring
component's scrape-target family (`components.monitoring.rules.inputs`) maps
each to the jobs whose targets it watches, and `perf` keeps the etcd families
a reading reads (`scripts.perf.exposition`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import promql_parser

__all__ = ('JOB_LABEL', 'Selector', 'selectors')

#: The label vmagent gives every series of a scrape job, the job's name.
JOB_LABEL = 'job'


@dataclass(frozen=True)
class Selector:
    """One series selector of an expression: the metric it names, and the jobs its `job` matcher names, if it has one."""

    metric: str
    jobs: frozenset[str] | None


#: A `job` value the reader takes: a literal name, or for `=~` an alternation of
#: them. A Service name holds none of a regular expression's other
#: metacharacters, and the empty name selects series with no `job` at all.
_JOB_ALTERNATION = re.compile(r'[\w:-]+(?:\|[\w:-]+)*')


def _selector(expr: str, node: promql_parser.VectorSelector) -> Selector:
    """One parsed selector as the reader takes it, refusing one it could read only short."""
    if node.name is None:
        # A `__name__` matcher is among these: the parser takes one only on a
        # selector that names no metric otherwise.
        raise ValueError(f'{expr!r} selects series by their labels alone, which names no metric')
    if node.matchers.or_matchers:
        # `{a="x" or b="y"}` leaves `matchers` empty and puts each alternative
        # here, so reading `matchers` alone would read it as naming no job.
        raise ValueError(f'{expr!r} selects {node.name} by alternatives of matchers, which the reader does not follow')
    named: frozenset[str] | None = None
    for matcher in node.matchers.matchers:
        if matcher.name != JOB_LABEL:
            continue
        literal = matcher.op == promql_parser.MatchOp.Equal
        if (
            named is not None
            or not (literal or matcher.op == promql_parser.MatchOp.Re)
            or not _JOB_ALTERNATION.fullmatch(matcher.value)
            or (literal and '|' in matcher.value)
        ):
            raise ValueError(f'{expr!r}: {matcher} names no definite set of jobs')
        named = frozenset(matcher.value.split('|'))
    return Selector(node.name, named)


def selectors(expr: str) -> tuple[Selector, ...]:
    """Every series selector of a PromQL expression, in order, as `promql_parser` parses it.

    An expression the reader cannot follow is refused rather than read short:
    one the parser refuses, MetricsQL's own syntax among them; a selector by
    labels alone; a metric named by a `__name__` matcher; alternatives of
    matchers; and a `job` matcher that names no definite set of jobs.
    """
    try:
        tree = promql_parser.parse(expr)
    except ValueError as error:
        raise ValueError(f'{expr!r}: {error}') from error
    found: list[Selector] = []

    def visit(node: promql_parser.Expr) -> bool:
        if isinstance(node, promql_parser.VectorSelector):
            found.append(_selector(expr, node))
        return True

    _ = promql_parser.walk(tree, pre_visit=visit)
    return tuple(found)
