"""`gate: conclusion` on a brief: the gate's where its issue is in `area/cluster`, beside any other area.

A brief is presented at a path that names its issue's areas in order
(`change.brief_path`), so `cluster` may fall first, last or alone among
them, and the gate's trigger takes it wherever it falls.
"""

from __future__ import annotations

import pytest

from kluster.scripts.dispatch.gates.change import asks_about_briefs, brief_path
from kluster.scripts.dispatch.gates.gate import named

GATE = named('conclusion')


@pytest.mark.parametrize(
    ('areas', 'asked'),
    [({'cluster'}, True), ({'ci', 'cluster'}, True), ({'cluster', 'dns'}, True), ({'dns'}, False)],
    ids=['cluster', 'cluster-second', 'cluster-first', 'dns'],
)
def test_a_brief_is_the_gates_where_its_issue_is_in_the_cluster_area(areas: set[str], asked: bool) -> None:
    path = brief_path(areas, 'b-897.md')

    assert asks_about_briefs(GATE.spec.trigger, path) is asked
