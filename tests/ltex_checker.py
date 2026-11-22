"""The bound on one run of the pinned prose checker, for a case that starts it.

`ltex-cli-plus` starts a JVM and a language server cold on every run, so a
run's duration is mostly start-up, and that grows with the machine's load
far more than the document's length does. The suite runs beside its own
workers (framework/testing.md §1), any of which may be starting the checker
too, and CI's runner is a shared machine.
"""

from __future__ import annotations

#: How long a case lets one run of the checker take: a stop-loss, which
#: nothing asserts on, inside the suite's 60 s case bound. Measured on a
#: 24-core workstation while the whole suite ran under six workers, at
#: loads of 6 to 17, one run over two short documents took 5 to 9 s, and
#: the case that makes it took 8 s.
STOP_LOSS = 40
