"""The live drill tier: rehearsals against real provider accounts.

Everything in this directory talks to a real tenancy with real credentials and
changes real state, so it is not collected at all unless the operator asks for
it by setting `RUN_LIVE_DRILLS=1`. Collection is skipped rather than the tests
being marked skipped: the ordinary `pytest` run stays green and says nothing
about a tier it did not run, and the opt-in is the whole mechanism — there is
no marker and no `addopts` entry to keep in sync with it.

How to run a drill, and when one is required, is `docs/framework/testing.md`
§5.
"""

from __future__ import annotations

import os

#: Set to `1` to collect this directory. Any other value, including unset,
#: leaves the drills uncollected.
OPT_IN = 'RUN_LIVE_DRILLS'

#: pytest consults this in the conftest of the directory being collected; the
#: conftest itself is loaded first either way, so the constant above stays
#: importable by the drills.
collect_ignore_glob: list[str] = [] if os.environ.get(OPT_IN) == '1' else ['*.py']
