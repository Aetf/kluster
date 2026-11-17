"""`state-backend census`: what the state table holds, and the prune of what nothing reads.

The census reads every key of the table and the size its value is stored
at, and prints the rows and bytes of each kind of key (`state.Kind`), then
every checkpoint with its own size. It writes nothing: the session it reads
in is one the server holds read-only (`state.census`).

`--prune-superseded` adds the copies of a checkpoint stored in a format
their stack no longer writes (`state.superseded`): the `.bak`, `history/`
and `backups/` copies a stack keeps, full-size and uncompressed, from before
its writers compressed (physical/state-backend.md §2). It prints them and
deletes nothing unless `--yes` is given too, and what it deletes is never a
checkpoint, an update's `history/` record, nor a copy in a format its stack
is stored in as of the delete's snapshot (`state.prune`).

`--prune-expired` adds the rows the retention rule expires
(`state.EXPIRED`, physical/state-backend.md §2): past
`settings.EXPIRY_AGE`, every checkpoint copy under `history/` and
`backups/`, every update record beyond the newest `settings.HISTORY_RECORDS`
of its history directory, and every `.bak` whose stack has no checkpoint.
Each is printed with the clause that expires it, and `--yes` deletes them;
the delete applies the rule again as of its own snapshot
(`state.prune_expired`), so it deletes no checkpoint, no lock and no row the
rule has stopped expiring since the listing. One run takes one of the two
modes.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from typing import TextIO

from kluster.lib.state_backend import settings, state

from . import probe

log = logging.getLogger(__name__)


def _mib(size: int) -> str:
    return f'{size / 2**20:.1f} MiB'


def summary(rows: Sequence[state.Row], database: str) -> list[str]:
    """The census as printed: rows and stored bytes per kind, then each checkpoint's own size.

    Every kind is printed, a kind with no row included, so a census reads the
    same way on every box and an absent kind is an answer rather than an
    omission.
    """
    counts = dict.fromkeys(state.Kind, 0)
    sizes = dict.fromkeys(state.Kind, 0)
    for row in rows:
        what = state.kind(row.key, database)
        counts[what] += 1
        sizes[what] += row.size
    width = max(len(member.value) for member in state.Kind)
    lines = [f'{"kind":<{width}}  {"rows":>6}  {"stored bytes":>14}  stored']
    lines += [
        f'{member.value:<{width}}  {counts[member]:>6}  {sizes[member]:>14,}  {_mib(sizes[member])}'
        for member in state.Kind
    ]
    total = sum(row.size for row in rows)
    lines.append(f'{"total":<{width}}  {len(rows):>6}  {total:>14,}  {_mib(total)}')
    lines.append('')
    lines.append(f'{state.Kind.CHECKPOINT.value}s:')
    lines += [
        f'  {row.key}  {row.size:,} bytes' for row in rows if state.kind(row.key, database) is state.Kind.CHECKPOINT
    ]
    return lines


def run(
    target: state.Connection,
    *,
    prune: bool,
    yes: bool,
    out: TextIO,
    expire: bool = False,
    age: dt.timedelta = settings.EXPIRY_AGE,
    records: int = settings.HISTORY_RECORDS,
) -> int:
    """The census over `target`, and the mode `prune` or `expire` asks for; the exit status.

    `prune` lists the superseded copies, `expire` the rows the retention
    rule expires under the lengths `age` and `records`; one run takes one
    of them. `yes` is what turns the listing into a delete. A delete that
    passes over a key it was given -- a row a writer made current again
    between the read and the delete, or one already gone -- leaves the row,
    names it, and makes the status 1.
    """
    if prune and expire:
        raise ValueError('one census run takes one mode: --prune-superseded or --prune-expired')
    database = settings.DATABASE
    rows = state.census(target)
    print('\n'.join(summary(rows, database)), file=out)
    if expire:
        return _expire(target, yes=yes, out=out, age=age, records=records)
    if not prune:
        return 0

    stale = state.superseded(rows, database)
    total = sum(row.size for row in stale)
    print('', file=out)
    print(f'superseded copies: {len(stale)}, {total:,} bytes stored', file=out)
    for row in stale:
        print(f'  {row.key}  {row.size:,} bytes', file=out)
    if not stale:
        return 0
    if not yes:
        log.info('nothing deleted: `--prune-superseded --yes` deletes the %d row(s) listed', len(stale))
        return 0

    asked = [row.key for row in stale]
    deleted = set(state.prune(target, asked))
    return _settled(asked, deleted, out=out, total=total, what='superseded')


def _expire(target: state.Connection, *, yes: bool, out: TextIO, age: dt.timedelta, records: int) -> int:
    """`--prune-expired`: the listing of the rows the retention rule expires, and their delete under `yes`."""
    stale = state.expired(target, age=age, records=records)
    total = sum(row.size for row in stale)
    print('', file=out)
    print(
        f'expired rows: {len(stale)}, {total:,} bytes stored '
        f'(written over {probe.hours(age)} ago; update records beyond the newest {records} of their directory)',
        file=out,
    )
    for row in stale:
        print(f'  {row.key}  {row.size:,} bytes  {row.clause.value}', file=out)
    if not stale:
        return 0
    if not yes:
        log.info('nothing deleted: `--prune-expired --yes` deletes the %d row(s) listed', len(stale))
        return 0

    asked = [row.key for row in stale]
    deleted = set(state.prune_expired(target, asked, age=age, records=records))
    return _settled(asked, deleted, out=out, total=total, what='expired')


def _settled(asked: Sequence[str], deleted: set[str], *, out: TextIO, total: int, what: str) -> int:
    """Each key asked for, deleted or kept, and the status: 1 where the delete passed over any."""
    for key in asked:
        print(f'{"deleted" if key in deleted else "kept"}  {key}', file=out)
    kept = [key for key in asked if key not in deleted]
    if kept:
        log.error(
            '%d row(s) were not deleted, because each is gone or was no longer %s when the delete ran: %s',
            len(kept),
            what,
            ', '.join(kept),
        )
        return 1
    log.info('deleted %d %s row(s), %s stored', len(deleted), what, _mib(total))
    return 0
