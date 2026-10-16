"""The live drill tier: rehearsals against real provider accounts.

Everything in this directory talks to a real tenancy with real credentials and
changes real state, so it is not collected at all unless the operator asks for
it by setting `RUN_LIVE_DRILLS=1`. Collection is skipped rather than the tests
being marked skipped: the ordinary `pytest` run stays green and says nothing
about a tier it did not run, and the opt-in is the whole mechanism — there is
no marker and no `addopts` entry to keep in sync with it.

The one thing this directory adds on top is the absence of a per-case bound.
`pyproject.toml` bounds every case with `pytest-timeout`, a hang guard sized
for the unit cases under the suite's four workers: an order of magnitude above
most of them and a few times above the slowest, whose measurements are
`docs/framework/testing.md` §1. A drill's duration is the provider's — a
rotation waits for a tenancy to authenticate a key — and a bound delivered
mid-rotation against a real account is a hazard rather than a failure. So
every item collected from under this directory is marked `timeout(0)` here,
where the opt-in already lives, rather than on a command line the operator has
to remember: the marker outranks the ini value and a `--timeout` flag alike.

The other is the one credential channel opened for the drills. The test
process closes the running checkout's file layer before collection
(`tests/file_layer.py`, framework/testing.md §1.1), and a drill opens and
rotates the operator's kit, which is a file in that layer or wherever
`KLUSTER_KDBX` names it. So every drill module runs inside
`file_layer.live()`: the checkout's own `.credentials/`, and the path
variables as the process received them. Module-scoped rather than
session-scoped, so that a run collecting unit cases beside the drills never
leaves the real layer open for the cases after them.

How to run a drill, and when one is required, is `docs/framework/testing.md`
§5.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import file_layer
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Set to `1` to collect this directory. Any other value, including unset,
#: leaves the drills uncollected.
OPT_IN = 'RUN_LIVE_DRILLS'

#: pytest consults this in the conftest of the directory being collected; the
#: conftest itself is loaded first either way, so the constant above stays
#: importable by the drills.
collect_ignore_glob: list[str] = [] if os.environ.get(OPT_IN) == '1' else ['*.py']

#: The directory whose items run under no per-case bound: this one.
HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Lift the per-case bound from every drill.

    The hook runs once over the whole session's items, so it filters by path:
    a unit case collected beside the drills keeps its bound.
    """
    for item in items:
        if HERE in item.path.parents:
            item.add_marker(pytest.mark.timeout(0))


@pytest.fixture(autouse=True, scope='module')
def operator_file_layer() -> Iterator[Path]:
    """The running checkout's own `.credentials/`, for every drill in the module."""
    with file_layer.live() as real:
        yield real
