"""`state-backend census`: what the state table holds, the prune of what nothing reads, and the ceiling.

The census reads every key of the table and the size its value is stored
at, and prints the rows and bytes of each kind of key (`table.Kind`), then
every checkpoint with its own size. It writes nothing: the session it reads
in is one the server holds read-only (`table.census`).

`--prune-superseded` adds the copies of a checkpoint stored in a format
their stack no longer writes (`table.superseded`): the `.bak`, `history/`
and `backups/` copies a stack keeps, full-size and uncompressed, from before
its writers compressed (physical/state-backend.md §2). It prints them and
deletes nothing unless `--yes` is given too, and what it deletes is never a
checkpoint, an update's `history/` record, nor a copy in a format its stack
is stored in as of the delete's snapshot (`table.prune`).

`--prune-expired` adds the rows the retention rule expires
(`table.EXPIRED`, physical/state-backend.md §2): past
`settings.EXPIRY_AGE`, every checkpoint copy under `history/` and
`backups/`, every update record beyond the newest `settings.HISTORY_RECORDS`
of its history directory, and every `.bak` whose stack has no checkpoint.
Each is printed with the clause that expires it, and `--yes` deletes them;
the delete applies the rule again as of its own snapshot
(`table.prune_expired`), so it deletes no checkpoint, no lock and no row the
rule has stopped expiring since the listing. One run takes one of the two
modes.

`--ceiling` adds, after the mode's prune, the size of every row's value --
the bytes a client writes and reads, not the stored size the census
prints -- and fails naming each row whose value exceeds
`settings.LARGEST_ROW_BYTES`, the largest row the pooler's cap is derived
for (physical/state-backend.md §2). It prints the largest value's size
whether it passes or fails, and runs whatever the prune answered.

The order is `table.run`'s, which the cluster's program runs too: this
command hands it the values of `settings` as arguments.
"""

from __future__ import annotations

import datetime as dt
from typing import TextIO

from kluster.lib.state_backend import settings, table


def run(
    target: table.Target,
    *,
    prune: bool,
    yes: bool,
    out: TextIO,
    expire: bool = False,
    ceiling: bool = False,
    age: dt.timedelta = settings.EXPIRY_AGE,
    records: int = settings.HISTORY_RECORDS,
    bound: int = settings.LARGEST_ROW_BYTES,
) -> int:
    """`table.run` over `target` under this installation's database; the exit status.

    `age` and `records` are the retention rule's lengths and `bound` the
    ceiling, the settings' unless a caller hands others.
    """
    return table.run(
        target,
        database=settings.DATABASE,
        out=out,
        age=age,
        records=records,
        bound=bound,
        prune=prune,
        expire=expire,
        ceiling=ceiling,
        yes=yes,
    )
