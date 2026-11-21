"""`drill`: the drills of operations.md §4 that a script runs, today the whole-cluster rebuild.

`drill rebuild` runs operations.md §3.5 step by step. The runbook says what
each step changes, why, what it reads and how the reading is judged, and what
a failure leaves; this package holds the commands, and `tests/test_drill.py`
holds the two to one list of steps (style/README.md, "Comments and docs").

-   `steps`: one `Step` per step of §3.5, and the pass readings after them.
-   `readings`: the baselines and the readings compared with them, a read
    that is not whole being a value of its own (`readings.NoReading`).
-   `scope`: which resources a rebuild changes, from the component types.
-   `capacity`: §3.4's capacity reading, as the operator pastes it.
-   `chain`: the deploy chain on the forge, read through `gh`.
-   `record`: the run's directory, its `run.json` and a transcript per step.
-   `tools`: how a step starts a command, and the environment it gets.
"""
