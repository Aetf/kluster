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
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TextIO

from kluster.lib.state_backend import settings, state

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


def run(target: state.Connection, *, prune: bool, yes: bool, out: TextIO) -> int:
    """The census over `target`, and the prune where `prune` asks for it; the exit status.

    `yes` is what turns the prune's listing into a delete. A delete that
    passes over a key it was given -- a row a writer made current again
    between the read and the delete, or one already gone -- leaves the row,
    names it, and makes the status 1.
    """
    database = settings.DATABASE
    rows = state.census(target)
    print('\n'.join(summary(rows, database)), file=out)
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
    for key in asked:
        print(f'{"deleted" if key in deleted else "kept"}  {key}', file=out)
    kept = [key for key in asked if key not in deleted]
    if kept:
        log.error(
            '%d row(s) were not deleted, because each is gone or was no longer superseded when the delete ran: %s',
            len(kept),
            ', '.join(kept),
        )
        return 1
    log.info('deleted %d superseded row(s), %s stored', len(deleted), _mib(total))
    return 0
