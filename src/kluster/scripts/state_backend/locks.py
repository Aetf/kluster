"""`state-backend release-lock`: a deploy deletes the lock its own `pulumi up` left, and no other.

A `pulumi` that holds a stack's lock deletes it on the way out, and reports a
delete that failed as a diagnostic alone: `Unlock` prints `there was a
problem deleting the lock at <key>` and leaves the command's exit status as
the update made it (`pkg/backend/diy/lock.go` at the pinned CLI). So a backend
that stops answering at the end of a run -- the Postgres behind it killed and
recovering -- leaves the stack locked after an `up` that failed and after one
that succeeded alike, and every later `up` refuses on it (framework/ci.md
§3.3).

**What proves a lock is the run's own is what `pulumi` writes into it.** The
lock is a key of its own under `.pulumi/locks/<stack's qualified name>/`,
named for an identifier its process drew at start, and its value is
`lockContent` -- `pid`, `username`, `hostname` and `timestamp`, the process's
own `os.Getpid()`, `os.Hostname()` and `time.Now()` among them
(`newLockContent`). The Postgres backend stores each value as
`{"data": "<base64 of the bytes>"}` (`blobData` in
`pkg/backend/diy/postgres/bucket.go`), and a lock is written uncompressed
whatever the checkpoints' format. The caller records the pid its `pulumi`
runs as and the time before it started (`Start`). A lock that names this
machine's hostname and that pid, taken no earlier than that time, is that
process's: a hosted runner runs one job, in its deploy job the `up` is the
one command that takes a lock, and a lock an earlier run left on a machine
that happened to share both names was taken before this run started.

**Only that one key is deleted.** `pulumi cancel` is no substitute: it
deletes every lock the stack holds (`CancelCurrentUpdate` in
`pkg/backend/diy/backend.go`), and a process whose `Lock` raced it can lose
its own row between writing it and reading the directory again, then go on
unlocked. A lock this cannot prove is the run's -- another run's, a
workstation's, an unreadable one, or every lock when the caller recorded no
start -- is named and left where it is.

**The backend is waited for.** The failure this exists for is a backend that
is not answering, so the read is tried again until it answers or `WAIT`
runs out, and the status says which.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from kluster.lib.state_backend import readiness, settings, state

log = logging.getLogger(__name__)

#: How long the backend is given to answer, and the pause between attempts.
#: What the wait outlasts is a Postgres recovering from a kill: in the
#: incident this exists for, its crash recovery inside the running container
#: took about a second (kluster-ops#784), and a container that ends instead
#: is started again 10 s later by its unit (`RestartSec` in
#: `machine/butane.yaml.j2`), with the pooler's unit waiting until it
#: serves. Ten minutes is far above both. What bounds it from above is the
#: `Up` step that runs it: the worst case, the wait and then one last attempt
#: of two `table.LISTING_TIMEOUT`s, stays well inside that step's bound.
WAIT = 600
INTERVAL = 15

#: The directory, under the database's own prefix, that holds every lock.
LOCKS = f'{state.BOOKKEEPING}{state.Kind.LOCKS.value}'

#: Every lock key and the value it holds, as one JSON document. A lock's value
#: is a few hundred bytes, so `->>` here costs the backend nothing, unlike the
#: same expression over a checkpoint.
LISTING_QUERY = (
    "SELECT coalesce(json_agg(json_build_array(key, data->>'data') ORDER BY key), '[]') "
    f"FROM {state.STATE_TABLE} WHERE starts_with(key, :'prefix')"
)

#: The delete of each key in the `keys` variable, a JSON array, answering with
#: the keys it deleted. A lock's key names the one process that drew its
#: identifier, and that process has exited before this runs, so nothing
#: writes a key between its read and its delete.
DELETE_STATEMENT = f"""
WITH gone AS (
    DELETE FROM {state.STATE_TABLE}
    WHERE key IN (SELECT json_array_elements_text(:'keys'::json))
    RETURNING key
)
SELECT coalesce(json_agg(key ORDER BY key), '[]') FROM gone;
"""


@dataclass(frozen=True)
class Start:
    """What the caller recorded of its `pulumi`: the pid it runs as, and a time no later than its start."""

    pid: int
    at: dt.datetime


@dataclass(frozen=True)
class Holder:
    """The process a lock names: `lockContent` at the pinned CLI."""

    pid: int
    username: str
    hostname: str
    timestamp: dt.datetime

    def __str__(self) -> str:
        """The holder the way `pulumi` names one in its refusal."""
        return f'{self.username}@{self.hostname} (pid {self.pid}) at {self.timestamp.isoformat()}'


@dataclass(frozen=True)
class Lock:
    """One lock row: its key, and the process its value names, or None where it names none."""

    key: str
    holder: Holder | None

    def __str__(self) -> str:
        return f'{self.key}, held by {self.holder or "a process its value does not name"}'


def holder(content: str) -> Holder | None:
    """The process a stored lock value names; None for a value that is no `lockContent`.

    `timestamp` is Go's `time.Time` in JSON, RFC 3339 with up to nine
    fractional digits and an offset, which `datetime.fromisoformat` reads.
    """
    try:
        found = cast('object', json.loads(base64.b64decode(content, validate=True)))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(found, dict):
        return None
    fields = cast('dict[str, object]', found)
    pid, username, hostname, timestamp = (fields.get(name) for name in ('pid', 'username', 'hostname', 'timestamp'))
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    if not (isinstance(username, str) and isinstance(hostname, str) and isinstance(timestamp, str)):
        return None
    try:
        taken = dt.datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if taken.tzinfo is None:
        return None
    return Holder(pid=pid, username=username, hostname=hostname, timestamp=taken)


def stack_locks(rows: Sequence[tuple[str, str]], stack: str, database: str) -> list[Lock]:
    """The locks among `rows` held on `stack`, named as `pulumi --stack` names it.

    A lock's directory is the stack's qualified name (`stackLockDir`), so
    the stack is its last element, and a qualified `stack` matches it whole.
    """
    pattern = re.compile(rf'{re.escape(f"{database}/{LOCKS}")}(?:[^/]+/)*{re.escape(stack)}/[^/]+\.json')
    return [Lock(key=key, holder=holder(content)) for key, content in rows if pattern.fullmatch(key)]


def ours(lock: Lock, *, start: Start | None, hostname: str) -> bool:
    """Whether `lock` was taken by the process `start` names on `hostname`, after `start`; never without a `start`."""
    if start is None or lock.holder is None:
        return False
    found = lock.holder
    return (found.pid, found.hostname) == (start.pid, hostname) and found.timestamp >= start.at


def read_start(path: Path) -> Start | None:
    """What the caller recorded in `path`; None where it recorded nothing usable.

    The file is one line, the pid and then the time in whole seconds since
    the epoch, the time read before `pulumi` started (`date +%s`), so it is
    no later than the lock that `pulumi` takes.
    """
    try:
        fields = path.read_text().split()
    except FileNotFoundError:
        return None
    if len(fields) != 2 or not all(field.isdecimal() for field in fields):
        return None
    pid, seconds = (int(field) for field in fields)
    return Start(pid=pid, at=dt.datetime.fromtimestamp(seconds, tz=dt.UTC))


def listing(target: state.Connection) -> list[tuple[str, str]]:
    """Every lock key of the state table and its stored value."""
    what = f'the listing of the locks in {state.STATE_TABLE}'
    found = state.query(target, LISTING_QUERY, what=what, variables={'prefix': f'{settings.DATABASE}/{LOCKS}'})
    if not isinstance(found, list):
        raise state.StateError(f'{what} printed a {type(found).__name__}, not a list of locks')
    rows: list[tuple[str, str]] = []
    for entry in cast('list[object]', found):
        pair = cast('list[object]', entry) if isinstance(entry, list) else []
        if len(pair) != 2 or not isinstance(pair[0], str) or not isinstance(pair[1], str):
            raise state.StateError(f'{what} printed an entry that is no key and value: {entry!r}')
        rows.append((pair[0], pair[1]))
    return rows


def delete(target: state.Connection, keys: Sequence[str]) -> list[str]:
    """Delete the lock rows `keys` name, returning the keys deleted."""
    if not keys:
        return []
    what = f'the delete of {len(keys)} lock(s) from {state.STATE_TABLE}'
    deleted = state.query(target, DELETE_STATEMENT, what=what, variables={'keys': json.dumps(list(keys))})
    if not isinstance(deleted, list) or not all(isinstance(key, str) for key in cast('list[object]', deleted)):
        raise state.StateError(f'{what} answered something other than the keys it deleted: {deleted!r}')
    return cast('list[str]', deleted)


def _attempt(target: state.Connection, *, stack: str, start: Start | None, hostname: str) -> None:
    """One read of the stack's locks, and the delete of the one this run took."""
    log.info('reading the locks held on %s from %s', stack, state.endpoint(target.url))
    held = stack_locks(listing(target), stack, settings.DATABASE)
    own = [lock for lock in held if ours(lock, start=start, hostname=hostname)]
    for lock in held:
        if lock not in own:
            log.warning("left alone, since nothing proves it is this run's: %s", lock)
    if not own:
        log.info('%s holds no lock this run took', stack)
        return
    log.info('deleting the lock this run took on %s: %s', stack, ', '.join(map(str, own)))
    deleted = set(delete(target, [lock.key for lock in own]))
    for lock in own:
        log.info('%s %s', 'deleted' if lock.key in deleted else 'already gone:', lock.key)


def release(
    target: state.Connection,
    *,
    stack: str,
    start: Start | None,
    hostname: str,
    wait: float = WAIT,
    interval: float = INTERVAL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Delete the lock on `stack` that the process `start` names took on `hostname`; the exit status.

    0 once the backend has answered: the lock is deleted, or there was none
    of this run's to delete. 1 when it did not answer within `wait`, so a
    lock of this run's may still be there.
    """
    if start is None:
        log.warning("the caller recorded no start, so no lock on %s can be proven this run's", stack)
    deadline = clock() + wait
    while True:
        try:
            _attempt(target, stack=stack, start=start, hostname=hostname)
        except state.StateError as refusal:
            left = deadline - clock()
            if left <= 0:
                log.error('%s', refusal)
                log.error(
                    'the backend did not answer within %s, so a lock this run took on %s may be left',
                    readiness.duration(wait),
                    stack,
                )
                return 1
            log.warning('%s', refusal)
            log.info(
                'waiting for the backend to answer: a Postgres that was killed recovers before it serves '
                '(trying again in %s, for up to %s more)',
                readiness.duration(min(interval, left)),
                readiness.duration(left),
            )
            sleep(min(interval, left))
        else:
            return 0
