"""The state table: its key grammar, the statements that read and prune it, and the order a run takes them in.

Pulumi's Postgres backend keeps every object in one table (`STATE_TABLE`),
keyed by the object's path under the database's own prefix. This module
reads that layout (`Kind`, `COPIES`, `RECORD`), and holds every statement
this repository runs against the table outside a dump: the census of its
keys and stored sizes, the prune of the copies stored in a format their
stack no longer writes (`superseded`), the retention rule (`EXPIRED`), and
the ceiling's read of each value's size. `run` takes them in one order,
which `state-backend census` runs on a workstation and `main` runs as a
program.

**A program of its own.** It imports the standard library and nothing else,
so the module is also a script an interpreter with a Postgres client runs
as it is, `python -I -B table.py <settings> <bundle directory>`, with no
code of this repository beside it; a test holds the imports to that. What
it would otherwise read from `kluster` -- the database's name, the
retention rule's lengths, the ceiling, and the names of a client bundle's
files -- reaches it as arguments: from `settings` on a workstation
(`kluster.scripts.state_backend.census`), and as a settings file written
from the same values for a program (`Settings`,
`kluster.lib.state_backend.state.table_settings`). Each value is written
once, where it is defined.

**Every statement goes through `psql`** (`query`), with the script on
standard input and every value it takes as a variable quoted by `psql`, so
no key is spliced into SQL text. A read runs in a session the server holds
read-only, whatever its statements say.
"""

from __future__ import annotations

import argparse
import datetime as dt
import enum
import json
import logging
import os
import re
import subprocess as sp
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO, cast

log = logging.getLogger(__name__)

#: The client every statement goes through: the `postgres` pin in
#: `mise.toml` on a workstation, whose major is the appliance's.
PSQL = 'psql'

#: The bound on one statement, a wait in the pooler's queue included, and on
#: a connection opening, past which the backend is read as one that does not
#: answer; with the variable libpq, and the driver behind Pulumi's Postgres
#: backend, read the second from.
LISTING_TIMEOUT = 120
CONNECT_TIMEOUT = 5
CONNECT_TIMEOUT_ENV = 'PGCONNECT_TIMEOUT'

#: The variable libpq hands the server a session's settings in, and what a
#: read's session is opened with: it may not write, whatever its statements
#: say.
SESSION_OPTIONS_ENV = 'PGOPTIONS'
_READ_ONLY = {SESSION_OPTIONS_ENV: '-c default_transaction_read_only=on'}

#: Where the state is: the table Pulumi's Postgres backend keeps it in -- the
#: backend's default name, since a client bundle's URL names no `table=`
#: -- and the directory, under the database's own prefix, that holds one
#: checkpoint key per stack. The appliance's dump script carries the same
#: two (`STATE_TABLE`, `STACKS`), and the suite holds them equal.
STATE_TABLE = 'pulumi_state'
STACKS = '.pulumi/stacks/'

#: The suffix after `.json` that names the format a key's value was written
#: in, for each compression the backend writes (`PULUMI_DIY_BACKEND_GZIP`,
#: `PULUMI_DIY_BACKEND_ZSTD`); a value written plain has none.
COMPRESSED = ('.gz', '.zst')
_FORMAT = '(' + '|'.join(re.escape(suffix) for suffix in COMPRESSED) + ')?'

#: What follows `STACKS` in a checkpoint's key: `<project>/<name>.json`, or
#: that and a suffix of `COMPRESSED` when the backend compresses. The `.bak`
#: beside each is not one.
CHECKPOINT = re.compile(rf'.+\.json{_FORMAT}')

#: The directory, under the database's own prefix, that every key the backend
#: writes lies under; `STACKS` is one of its directories.
BOOKKEEPING = '.pulumi/'


class StateError(RuntimeError):
    """A step against the state backend did not happen, and says which refused."""


class Kind(enum.Enum):
    """What a key of the state table is, read off its path under `<database>/` + `BOOKKEEPING`.

    The layout is the backend's (`pkg/backend/diy` at the pinned CLI), and
    each member's value is how a census prints it.
    """

    #: A stack's checkpoint: `CHECKPOINT` under `STACKS`.
    CHECKPOINT = 'checkpoint'
    #: The copy of a checkpoint every save makes before it replaces one
    #: (`COPIES`).
    PREVIOUS = 'checkpoint .bak'
    #: One copy of the checkpoint per writing operation (`backupStack`), which
    #: `PULUMI_DIY_BACKEND_DISABLE_CHECKPOINT_BACKUPS` turns off.
    BACKUPS = 'backups/'
    #: Two files per writing operation, the update's record and a copy of the
    #: checkpoint (`addToHistory`), which no switch turns off.
    HISTORY = 'history/'
    #: One per operation that holds a stack's lock, while it holds it.
    LOCKS = 'locks/'
    #: The layout's version.
    META = 'meta.yaml'
    #: Anything else: a tags file, a retained checkpoint, a key outside the
    #: layout.
    OTHER = 'other'


#: The directories under `BOOKKEEPING` whose every key is one kind.
_DIRECTORIES = (Kind.BACKUPS, Kind.HISTORY, Kind.LOCKS)

#: A stack's directory under `history/` or `backups/`, as the backend names
#: it (`HistoryDir` and `BackupDir` in `pkg/backend/diy/store.go`):
#: `<project>/<stack>`, or `<stack>` alone in its project-less layout. The
#: first group is the whole, which is the stack's path under `STACKS` less
#: `.json`; the second is its last element, the stack's name.
_STACK_DIRECTORY = r'((?:[^/]+/)*([^/]+))'

#: The copies the backend keeps of a stack's checkpoint, each a pattern the
#: path of the copy's key under `<database>/` + `BOOKKEEPING` matches whole,
#: as `pkg/backend/diy/state.go` at the pinned CLI names them. In each, the
#: first group is the stack's path under `STACKS` less `.json`, and the last
#: is the suffix of `COMPRESSED` naming the format the copy was written in,
#: or nothing for a plain one. Two dialects read them, Python's and
#: Postgres's (`PRUNE_STATEMENT`), so they are written in what the two read
#: alike, and no key matches one of them in two ways.
COPIES: dict[Kind, str] = {
    # `<checkpoint>.bak` (`backupTarget`), in the format of the checkpoint it
    # copied.
    Kind.PREVIOUS: rf'{STACKS.removeprefix(BOOKKEEPING)}(.+)\.json{_FORMAT}\.bak',
    # `<stack>-<unix nanoseconds>.checkpoint.json` (`addToHistory`), in the
    # format its writer was set to write. The `.history.json` record beside
    # it is the update's own, not a copy, and is what `pulumi stack history`
    # reads (`getHistory`).
    Kind.HISTORY: rf'{Kind.HISTORY.value}{_STACK_DIRECTORY}/\2-[0-9]+\.checkpoint\.json{_FORMAT}',
    # `<stack>.<unix nanoseconds>.json` (`backupStack`), in the format of the
    # checkpoint it copied.
    Kind.BACKUPS: rf'{Kind.BACKUPS.value}{_STACK_DIRECTORY}/\2\.[0-9]+\.json{_FORMAT}',
}
_COPIES = tuple(re.compile(pattern) for pattern in COPIES.values())

#: An update's own record under `history/`: `<stack>-<unix nanoseconds>.history.json`
#: and the suffix of its writer's format (`addToHistory`), beside the
#: checkpoint copy `COPIES` names, and the file `pulumi stack history` reads
#: (`getHistory`). Written like `COPIES`, for the same two dialects.
RECORD = rf'{Kind.HISTORY.value}{_STACK_DIRECTORY}/\2-[0-9]+\.history\.json{_FORMAT}'


@dataclass(frozen=True)
class Row:
    """One key of the state table, and a size of its value, which the read that answers it names.

    The census reads the size the value is stored at, `pg_column_size`: the
    bytes Postgres stores, after its own compression where it applied any,
    read without fetching an out-of-line value. The ceiling reads the
    value's own size (`value_sizes`), the bytes a client writes and reads.
    """

    key: str
    size: int


class Target(Protocol):
    """What a statement is run over: a client bundle's connection string, and the variables naming its files.

    `url` is the string as the bundle stores it, which is the form a query
    reads (`kluster.lib.bundle.Purpose.QUERY`). `env` is the libpq variables
    naming the bundle's certificate, key and CA.
    """

    @property
    def url(self) -> str: ...

    @property
    def env(self) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class Client:
    """A `Target` read from a client bundle's directory (`Settings.client`)."""

    url: str
    env: Mapping[str, str]


def endpoint(url: str) -> str:
    """A connection string with its query dropped — what a log line may say.

    The query is the TLS mode, and on a bundle written before the certificate
    paths moved into the environment it is three absolute paths as well:
    nothing secret, nothing informative, and longer than everything else the
    run prints.
    """
    return url.split('?', 1)[0]


def execute(
    argv: Sequence[str],
    *,
    what: str,
    timeout: int,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
    hint: str | None = None,
) -> str:
    """One external tool, its standard output, and its failure turned into a `StateError` naming `what`.

    A timeout is a real outcome for a statement over the network, so it says
    which step ran out rather than surfacing a `TimeoutExpired`. `env` is
    overlaid on this process's environment rather than replacing it. `hint`
    says where the tool comes from, for a refusal that it is not on `PATH`.
    """
    try:
        proc = sp.run(
            list(argv),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **env} if env else None,
        )
    except FileNotFoundError as exc:
        raise StateError(f'{argv[0]} is not on PATH' + (f': {hint}' if hint else '')) from exc
    except sp.TimeoutExpired as exc:
        raise StateError(f'{what} did not finish within {timeout}s') from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        # psql echoes the failing statement after the diagnosis, so the last
        # line is as likely to be a fragment of SQL as the error itself.
        found = next((line for line in reversed(detail) if 'error' in line.lower()), None)
        raise StateError(f'{what} failed: {found or (detail[-1] if detail else f"exit {proc.returncode}")}')
    return proc.stdout


def query(
    target: Target,
    sql: str,
    *,
    what: str,
    variables: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> object:
    """One script through `psql` over `target`, its one value read back as JSON.

    The script travels on standard input, which is where `psql` interpolates
    `variables` -- quoted as literals, `:'name'` -- so no key is spliced into
    SQL text here. `env` is overlaid on the target's own variables, as a
    read's session setting is. `what` names the statement in a refusal.
    """
    argv = [
        PSQL,
        '--no-psqlrc',
        '--no-password',
        '--quiet',
        '--tuples-only',
        '--no-align',
        '--set=ON_ERROR_STOP=1',
        *(f'--set={name}={value}' for name, value in variables.items()),
        f'--dbname={target.url}',
    ]
    printed = execute(
        argv,
        what=what,
        timeout=LISTING_TIMEOUT,
        stdin=sql,
        env={**target.env, CONNECT_TIMEOUT_ENV: str(CONNECT_TIMEOUT), **(env or {})},
        hint='on a workstation it is the `postgres` pin in mise.toml (`mise x -- ...`)',
    )
    try:
        return json.loads(printed)
    except ValueError as exc:
        raise StateError(f'{what} printed something other than JSON: {printed[:120]!r}') from exc


def _keys_answered(answer: object, what: str) -> list[str]:
    """The keys a delete answered with, or a refusal quoting what it answered instead."""
    if not isinstance(answer, list) or not all(isinstance(key, str) for key in cast('list[object]', answer)):
        raise StateError(f'{what} answered something other than the keys it deleted: {answer!r}')
    return cast('list[str]', answer)


def _sized(listing: object, what: str) -> list[Row]:
    """The rows of a listing printed as `[[key, size], ...]`, or a refusal quoting the entry that is not one."""
    if not isinstance(listing, list):
        raise StateError(f'{what} printed a {type(listing).__name__}, not a list of keys')
    found: list[Row] = []
    for entry in cast('list[object]', listing):
        pair = cast('list[object]', entry) if isinstance(entry, list) else []
        if len(pair) != 2 or not isinstance(pair[0], str) or not isinstance(pair[1], int):
            raise StateError(f'{what} printed an entry that is no key and size: {entry!r}')
        found.append(Row(key=pair[0], size=pair[1]))
    return found


def kind(key: str, database: str) -> Kind:
    """What `key` is, in the backend's layout under `database`'s prefix."""
    rest = key.removeprefix(f'{database}/{BOOKKEEPING}')
    if rest == key:
        return Kind.OTHER
    if rest == Kind.META.value:
        return Kind.META
    stacks = STACKS.removeprefix(BOOKKEEPING)
    if rest.startswith(stacks):
        if CHECKPOINT.fullmatch(rest.removeprefix(stacks)):
            return Kind.CHECKPOINT
        if re.fullmatch(COPIES[Kind.PREVIOUS], rest):
            return Kind.PREVIOUS
        return Kind.OTHER
    return next((member for member in _DIRECTORIES if rest.startswith(member.value)), Kind.OTHER)


def _copy_of(path: str) -> tuple[str, str] | None:
    """The stack whose checkpoint the key at `path` is a copy of, and the format it is in; `None` for no copy."""
    for pattern in _COPIES:
        if found := pattern.fullmatch(path):
            return found.group(1), found.group(pattern.groups) or ''
    return None


def superseded(rows: Sequence[Row], database: str) -> list[Row]:
    """The copies of a checkpoint stored in a format their stack no longer writes.

    A stack's checkpoint is one key, `<stack>.json` and the suffix of the
    format its last save wrote, and a save that changes the format writes
    the new key and deletes the old (`saveCheckpoint` in the backend's
    `state.go`). Every copy the backend makes carries a suffix too
    (`COPIES`), and nothing it does ever replaces or removes one written in
    the old format: a stack whose writers turned compression on keeps
    `<stack>.json.bak` and every `history/` and `backups/` copy written
    before, all full-size uncompressed values. Nothing reads them back.
    `pulumi stack history` reads the `.history.json` records alone
    (`getHistory`), and no command reads `backups/`.

    A definition rather than a list: a copy is superseded when its stack
    has a checkpoint and none under the suffix the copy carries. A stack
    with no checkpoint keeps its copies, which are all that is left of a
    removed stack; a copy in a format some checkpoint of its stack is
    stored in is kept, however many suffixes the stack has; and the
    update records under `history/` are no copies.
    """
    prefix = f'{database}/{BOOKKEEPING}'
    stacks = STACKS.removeprefix(BOOKKEEPING)
    live: dict[str, set[str]] = {}
    copies: list[tuple[Row, str, str]] = []
    for row in rows:
        if not row.key.startswith(prefix):
            continue
        path = row.key.removeprefix(prefix)
        checkpoint = path.removeprefix(stacks)
        if checkpoint != path and (found := CHECKPOINT.fullmatch(checkpoint)):
            suffix = found.group(1) or ''
            live.setdefault(checkpoint.removesuffix(f'.json{suffix}'), set()).add(suffix)
        elif copied := _copy_of(path):
            copies.append((row, *copied))
    return [row for row, stack, suffix in copies if stack in live and suffix not in live[stack]]


#: Every key of the state table and its stored size, as one JSON document.
CENSUS_QUERY = (
    f"SELECT coalesce(json_agg(json_build_array(key, pg_column_size(data)) ORDER BY key), '[]') FROM {STATE_TABLE}"
)


def census(target: Target) -> list[Row]:
    """Every key of the state table and the size its value is stored at, in key order.

    Read in a session the server holds read-only, and with `pg_column_size`,
    never with an expression over the value: the backend's own size check,
    `octet_length((data->>'data')::text)`, parses the whole value in the
    serving backend (physical/state-backend.md §2).
    """
    log.info('reading every key of %s and its stored size from %s', STATE_TABLE, endpoint(target.url))
    what = f'the census of {STATE_TABLE}'
    return _sized(query(target, CENSUS_QUERY, what=what, variables={}, env=_READ_ONLY), what)


#: The delete of the keys in the `keys` variable, a JSON array, answering with
#: the keys it deleted. It applies `superseded`'s definition again, to the
#: table as of the statement's snapshot, from the same patterns and suffixes
#: (`COPIES`, `COMPRESSED`), which `prune` hands it as variables beside the
#: prefixes they are read under. So it deletes only a copy of a checkpoint
#: whose stack has a checkpoint and none in the copy's format: never a
#: checkpoint, never a key that is no copy, and never a copy in a format its
#: stack is stored in, whatever the list says and whatever a writer
#: committed since the list was read.
PRUNE_STATEMENT = f"""
WITH asked AS (
    SELECT listed.key, substr(listed.key, length(:'layout') + 1) AS path
    FROM json_array_elements_text(:'keys'::json) AS listed (key)
    WHERE starts_with(listed.key, :'layout')
),
copies AS (
    SELECT asked.key,
           :'layout' || :'stacks' || parsed.parts[1] || '.json' AS checkpoint,
           coalesce(parsed.parts[cardinality(parsed.parts)], '') AS format
    FROM asked
    CROSS JOIN LATERAL (
        SELECT matched.parts
        FROM json_array_elements_text(:'copies'::json) AS copy (pattern),
             regexp_match(asked.path, '^(?:' || copy.pattern || ')$') AS matched (parts)
        WHERE matched.parts IS NOT NULL
        LIMIT 1
    ) AS parsed
),
gone AS (
    DELETE FROM {STATE_TABLE} AS stale
    USING copies
    WHERE stale.key = copies.key
      AND EXISTS (
          SELECT 1
          FROM {STATE_TABLE} AS live, json_array_elements_text(:'formats'::json) AS stored (format)
          WHERE live.key = copies.checkpoint || stored.format
      )
      AND NOT EXISTS (SELECT 1 FROM {STATE_TABLE} AS live WHERE live.key = copies.checkpoint || copies.format)
    RETURNING stale.key
)
SELECT coalesce(json_agg(key ORDER BY key), '[]') FROM gone;
"""


def prune(target: Target, keys: Sequence[str], *, database: str) -> list[str]:
    """Delete the superseded copies named, under `database`'s prefix, returning the keys deleted.

    A key is deleted only where it is a copy of a checkpoint superseded as
    of the statement's snapshot (`PRUNE_STATEMENT`), so a key the statement
    passes over is a row a writer has made current again, one whose stack
    has gone, one already gone, or no copy at all; the caller compares what
    it asked for with what came back.
    """
    if not keys:
        return []
    log.info('deleting %d superseded row(s) from %s', len(keys), endpoint(target.url))
    variables = {
        'keys': json.dumps(list(keys)),
        'layout': f'{database}/{BOOKKEEPING}',
        'stacks': STACKS.removeprefix(BOOKKEEPING),
        'copies': json.dumps(list(COPIES.values())),
        'formats': json.dumps(['', *COMPRESSED]),
    }
    what = f'the prune of {STATE_TABLE}'
    return _keys_answered(query(target, PRUNE_STATEMENT, what=what, variables=variables), what)


class Expiry(enum.Enum):
    """The clause of the retention rule that expires a row (`EXPIRED`); each member's value is how a listing prints it."""

    #: A checkpoint copy under `history/` or `backups/`.
    COPY = 'checkpoint copy'
    #: An update record beyond the newest of its history directory.
    RECORD = 'update record'
    #: A `.bak` whose stack has no checkpoint under any suffix.
    LONE = 'lone .bak'


@dataclass(frozen=True)
class Expired:
    """A row the retention rule expires, its stored size as the census reads it, and the clause that expires it."""

    key: str
    size: int
    clause: Expiry


#: The retention rule (physical/state-backend.md §2), the one place it is
#: written: the common table expressions ending in `expired`, the key of
#: every row of the table the rule expires as of the statement's snapshot,
#: and the clause that expires it. A row is expired when it was written more
#: than `age` ago and it is a checkpoint copy under `history/` or `backups/`
#: (`COPIES`), in any format and whether or not its stack still exists; an
#: update record (`RECORD`) that is not among the newest `records` records
#: of its directory, newest by key, the order `getHistory` returns; or a
#: `.bak` (`COPIES`) whose stack has no checkpoint under any suffix of
#: `COMPRESSED`. Nothing else expires. Age is the row's `updated_at`, which
#: the backend sets on every write, against the server's `now()`. Only
#: `key` and `updated_at` are read, so the rule costs what a listing of keys
#: costs. `EXPIRED_QUERY` lists from it and `EXPIRE_STATEMENT` deletes by
#: it, each as of its own snapshot, from the variables `_retention` names.
EXPIRED = f"""
laid_out AS (
    SELECT key, updated_at, substr(key, length(:'layout') + 1) AS path
    FROM {STATE_TABLE}
    WHERE starts_with(key, :'layout')
      AND updated_at < now() - make_interval(secs => :'age'::double precision)
),
ranked AS (
    SELECT key,
           row_number() OVER (
               PARTITION BY regexp_replace(path, '/[^/]*$', '')
               ORDER BY key COLLATE "C" DESC
           ) AS newest
    FROM (
        SELECT key, substr(key, length(:'layout') + 1) AS path
        FROM {STATE_TABLE}
        WHERE starts_with(key, :'layout')
    ) AS everything
    WHERE path ~ ('^(?:' || :'record' || ')$')
),
expired AS (
    SELECT laid_out.key, :'copy' AS clause
    FROM laid_out
    WHERE EXISTS (
        SELECT 1
        FROM json_array_elements_text(:'copies'::json) AS copy (pattern)
        WHERE laid_out.path ~ ('^(?:' || copy.pattern || ')$')
    )
    UNION ALL
    SELECT laid_out.key, :'record_clause'
    FROM laid_out
    JOIN ranked USING (key)
    WHERE ranked.newest > :'records'::integer
    UNION ALL
    SELECT laid_out.key, :'lone'
    FROM laid_out
    CROSS JOIN LATERAL regexp_match(laid_out.path, '^(?:' || :'previous' || ')$') AS matched (parts)
    WHERE matched.parts IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM {STATE_TABLE} AS live, json_array_elements_text(:'formats'::json) AS stored (format)
          WHERE live.key = :'layout' || :'stacks' || matched.parts[1] || '.json' || stored.format
      )
)
"""

#: Every row `EXPIRED` expires, as one JSON document: its key, its stored
#: size (`Row`) and its clause.
EXPIRED_QUERY = f"""
WITH {EXPIRED}
SELECT coalesce(
    json_agg(json_build_array(expired.key, pg_column_size(listed.data), expired.clause) ORDER BY expired.key),
    '[]'
)
FROM expired
JOIN {STATE_TABLE} AS listed USING (key);
"""

#: The delete of the keys in the `keys` variable, a JSON array, answering with
#: the keys it deleted: those among them that `EXPIRED` expires as of the
#: statement's snapshot. A row written again since a listing named it, or a
#: record that is among the newest of its directory again, is not deleted,
#: whatever the list says.
EXPIRE_STATEMENT = f"""
WITH {EXPIRED},
asked AS (
    SELECT listed.key FROM json_array_elements_text(:'keys'::json) AS listed (key)
),
gone AS (
    DELETE FROM {STATE_TABLE} AS stale
    USING expired
    WHERE stale.key = expired.key
      AND expired.key IN (SELECT asked.key FROM asked)
    RETURNING stale.key
)
SELECT coalesce(json_agg(key ORDER BY key), '[]') FROM gone;
"""


def _retention(database: str, age: dt.timedelta, records: int) -> dict[str, str]:
    """The variables `EXPIRED` reads: the rule's two lengths, the key grammar, and each clause's label."""
    return {
        'layout': f'{database}/{BOOKKEEPING}',
        'stacks': STACKS.removeprefix(BOOKKEEPING),
        'copies': json.dumps([COPIES[Kind.HISTORY], COPIES[Kind.BACKUPS]]),
        'previous': COPIES[Kind.PREVIOUS],
        'record': RECORD,
        'formats': json.dumps(['', *COMPRESSED]),
        'age': str(age.total_seconds()),
        'records': str(records),
        'copy': Expiry.COPY.value,
        'record_clause': Expiry.RECORD.value,
        'lone': Expiry.LONE.value,
    }


def expired(target: Target, *, database: str, age: dt.timedelta, records: int) -> list[Expired]:
    """Every row the retention rule expires (`EXPIRED`) under `database`'s prefix, in key order.

    Read in a session that cannot write. `age` and `records` are the rule's
    two lengths, `settings.EXPIRY_AGE` and `settings.HISTORY_RECORDS` where
    an operator runs it.
    """
    log.info(
        'listing the rows of %s older than %s that the retention rule expires, from %s',
        STATE_TABLE,
        age,
        endpoint(target.url),
    )
    what = f'the listing of the expired rows of {STATE_TABLE}'
    listing = query(target, EXPIRED_QUERY, what=what, variables=_retention(database, age, records), env=_READ_ONLY)
    if not isinstance(listing, list):
        raise StateError(f'{what} printed a {type(listing).__name__}, not a list of rows')
    clauses = {member.value: member for member in Expiry}
    found: list[Expired] = []
    for entry in cast('list[object]', listing):
        row = cast('list[object]', entry) if isinstance(entry, list) else []
        if (
            len(row) != 3
            or not isinstance(row[0], str)
            or not isinstance(row[1], int)
            or not isinstance(row[2], str)
            or row[2] not in clauses
        ):
            raise StateError(f'{what} printed an entry that is no key, size and clause: {entry!r}')
        found.append(Expired(key=row[0], size=row[1], clause=clauses[row[2]]))
    return found


def prune_expired(target: Target, keys: Sequence[str], *, database: str, age: dt.timedelta, records: int) -> list[str]:
    """Delete the expired rows named, returning the keys deleted.

    A key is deleted only where the rule expires it as of the statement's
    snapshot (`EXPIRE_STATEMENT`), under the same lengths the listing was
    read with, so a key the statement passes over is a row a writer has
    written again, a record among the newest of its directory again, one
    already gone, or one the rule never expired; the caller compares what it
    asked for with what came back.
    """
    if not keys:
        return []
    log.info('deleting %d expired row(s) from %s', len(keys), endpoint(target.url))
    what = f'the delete of the expired rows of {STATE_TABLE}'
    variables = {**_retention(database, age, records), 'keys': json.dumps(list(keys))}
    return _keys_answered(query(target, EXPIRE_STATEMENT, what=what, variables=variables), what)


#: Every key of the state table and the size of its value, as one JSON
#: document: `octet_length` of the value as text, the bytes a client writes
#: and reads, which is what the box's memory per statement is a multiple of
#: (physical/state-backend.md §2). It is not the stored size the census
#: reads, which TOAST's compression can bring to half of it. Nothing parses
#: the JSON, and the value is taken one row at a time.
VALUE_SIZES_QUERY = (
    f"SELECT coalesce(json_agg(json_build_array(key, octet_length(data::text)) ORDER BY key), '[]') FROM {STATE_TABLE}"
)


def value_sizes(target: Target) -> list[Row]:
    """Every key of the state table and its value's size (`VALUE_SIZES_QUERY`), in key order.

    Read in a session that cannot write.
    """
    log.info('reading the size of every value of %s from %s', STATE_TABLE, endpoint(target.url))
    what = f'the value sizes of {STATE_TABLE}'
    return _sized(query(target, VALUE_SIZES_QUERY, what=what, variables={}, env=_READ_ONLY), what)


def _mib(size: int) -> str:
    return f'{size / 2**20:.1f} MiB'


def summary(rows: Sequence[Row], database: str) -> list[str]:
    """The census as printed: rows and stored bytes per kind, then each checkpoint's own size.

    Every kind is printed, a kind with no row included, so a census reads the
    same way on every box and an absent kind is an answer rather than an
    omission.
    """
    counts = dict.fromkeys(Kind, 0)
    sizes = dict.fromkeys(Kind, 0)
    for row in rows:
        what = kind(row.key, database)
        counts[what] += 1
        sizes[what] += row.size
    width = max(len(member.value) for member in Kind)
    lines = [f'{"kind":<{width}}  {"rows":>6}  {"stored bytes":>14}  stored']
    lines += [
        f'{member.value:<{width}}  {counts[member]:>6}  {sizes[member]:>14,}  {_mib(sizes[member])}' for member in Kind
    ]
    total = sum(row.size for row in rows)
    lines.append(f'{"total":<{width}}  {len(rows):>6}  {total:>14,}  {_mib(total)}')
    lines.append('')
    lines.append(f'{Kind.CHECKPOINT.value}s:')
    lines += [f'  {row.key}  {row.size:,} bytes' for row in rows if kind(row.key, database) is Kind.CHECKPOINT]
    return lines


def run(
    target: Target,
    *,
    database: str,
    out: TextIO,
    age: dt.timedelta,
    records: int,
    bound: int,
    prune: bool = False,
    expire: bool = False,
    ceiling: bool = False,
    yes: bool = False,
) -> int:
    """The census over `target`, then the prune `prune` or `expire` asks for, then the ceiling; the exit status.

    `prune` lists the superseded copies, `expire` the rows the retention
    rule expires under the lengths `age` and `records`; one run takes one
    of them, and `yes` is what turns the listing into a delete. A delete
    that passes over a key it was given -- a row a writer made current
    again between the read and the delete, or one already gone -- leaves
    the row, names it, and makes the status 1.

    `ceiling` reads every value's size after the prune, and names each row
    whose value exceeds `bound`, which makes the status 1. The prune comes
    first because the ceiling bounds what stays in the table: a row over
    the bound that the prune takes fails a read before it and passes the
    one after. The ceiling runs whatever the prune answered, so a row the
    delete kept cannot hide a row over the bound, and the log names each
    step that failed.
    """
    if prune and expire:
        raise ValueError('one census run takes one mode: --prune-superseded or --prune-expired')
    rows = census(target)
    print('\n'.join(summary(rows, database)), file=out)
    status = 0
    if expire:
        status = _expire(target, database=database, yes=yes, out=out, age=age, records=records)
    elif prune:
        status = _supersede(target, rows, database=database, yes=yes, out=out)
    if ceiling:
        status = max(status, _ceiling(target, bound=bound, out=out))
    return status


def _supersede(target: Target, rows: Sequence[Row], *, database: str, yes: bool, out: TextIO) -> int:
    """`--prune-superseded`: the listing of the superseded copies among `rows`, and their delete under `yes`."""
    stale = superseded(rows, database)
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
    deleted = set(prune(target, asked, database=database))
    return _settled(asked, deleted, out=out, total=total, what='superseded')


def _expire(target: Target, *, database: str, yes: bool, out: TextIO, age: dt.timedelta, records: int) -> int:
    """`--prune-expired`: the listing of the rows the retention rule expires, and their delete under `yes`."""
    stale = expired(target, database=database, age=age, records=records)
    total = sum(row.size for row in stale)
    print('', file=out)
    print(
        f'expired rows: {len(stale)}, {total:,} bytes stored '
        f'(written over {age.total_seconds() / 3600:.1f} h ago; '
        f'update records beyond the newest {records} of their directory)',
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
    deleted = set(prune_expired(target, asked, database=database, age=age, records=records))
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


def _ceiling(target: Target, *, bound: int, out: TextIO) -> int:
    """`--ceiling`: the largest value's size, each row whose value exceeds `bound`, and the status: 1 where any does."""
    sizes = value_sizes(target)
    over = [row for row in sizes if row.size > bound]
    print('', file=out)
    if sizes:
        largest = max(sizes, key=lambda row: row.size)
        print(f'largest value: {largest.key}  {largest.size:,} bytes; the ceiling is {bound:,} bytes', file=out)
    else:
        print(f'largest value: none, the table holds no row; the ceiling is {bound:,} bytes', file=out)
    print(f'values over the ceiling: {len(over)}', file=out)
    for row in over:
        print(f'  {row.key}  {row.size:,} bytes', file=out)
    if over:
        log.error(
            '%d row(s) exceed the ceiling of %s bytes, the largest row the pooler cap was derived for: %s',
            len(over),
            f'{bound:,}',
            ', '.join(f'{row.key} ({row.size:,} bytes)' for row in over),
        )
        return 1
    log.info('every value is within the ceiling of %s bytes', f'{bound:,}')
    return 0


def _number(value: object, *, whole: bool = False) -> bool:
    """Whether a value read from JSON is a number, and a whole one where `whole` asks; a boolean is neither."""
    return not isinstance(value, bool) and isinstance(value, int if whole else int | float)


@dataclass(frozen=True)
class Settings:
    """What `main` reads from the settings file: each value a workstation's command passes as an argument.

    `database` is the database whose prefix the table's keys lie under;
    `expiry_age` and `history_records` the retention rule's lengths;
    `largest_row_bytes` the ceiling. `url_file` and `ssl_files` are a client
    bundle's layout: the file in its directory holding the connection
    string, and each libpq variable with the file it names.
    """

    database: str
    expiry_age: dt.timedelta
    history_records: int
    largest_row_bytes: int
    url_file: str
    ssl_files: Mapping[str, str]

    def dumps(self) -> str:
        """The settings file's text, which `load` reads back."""
        return json.dumps(
            {
                'database': self.database,
                'expiry_age_seconds': self.expiry_age.total_seconds(),
                'history_records': self.history_records,
                'largest_row_bytes': self.largest_row_bytes,
                'url_file': self.url_file,
                'ssl_files': dict(self.ssl_files),
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def load(cls, path: Path) -> Settings:
        """The settings in the file at `path`, or a refusal naming the field that is missing or of another type."""
        try:
            read: object = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise StateError(f'the settings file {path} cannot be read as JSON: {exc}') from exc
        fields = cast('dict[str, object]', read) if isinstance(read, dict) else {}
        database, url_file = fields.get('database'), fields.get('url_file')
        seconds, records, bound = (
            fields.get(name) for name in ('expiry_age_seconds', 'history_records', 'largest_row_bytes')
        )
        files = fields.get('ssl_files')
        checked = (
            isinstance(database, str)
            and isinstance(url_file, str)
            and _number(seconds)
            and _number(records, whole=True)
            and _number(bound, whole=True)
            and isinstance(files, dict)
            and all(isinstance(name, str) for pair in cast('dict[object, object]', files).items() for name in pair)
        )
        if not checked:
            raise StateError(
                f'the settings file {path} does not hold every field `Settings` reads, each of its type: {sorted(fields)}'
            )
        return cls(
            database=cast('str', database),
            expiry_age=dt.timedelta(seconds=cast('float', seconds)),
            history_records=cast('int', records),
            largest_row_bytes=cast('int', bound),
            url_file=cast('str', url_file),
            ssl_files=cast('dict[str, str]', files),
        )

    def client(self, bundle_dir: Path) -> Client:
        """The `Target` the client bundle in `bundle_dir` is: its connection string, and the variables naming its files."""
        url_path = bundle_dir / self.url_file
        try:
            url = url_path.read_text().strip()
        except OSError as exc:
            raise StateError(f'the client bundle has no connection string at {url_path}: {exc}') from exc
        if not url:
            raise StateError(f'{url_path} is empty')
        directory = bundle_dir.resolve()
        return Client(url=url, env={variable: str(directory / name) for variable, name in self.ssl_files.items()})


def main(argv: Sequence[str] | None = None) -> int:
    """The program: `run` with the prune of the expired rows and the ceiling, over a bundle, under a settings file."""
    parser = argparse.ArgumentParser(
        prog='table.py',
        description=(
            'Count the state table, delete the rows its retention rule expires, and fail naming '
            'each row whose value exceeds the ceiling.'
        ),
    )
    _ = parser.add_argument('settings', type=Path, help='the settings file (`Settings`)')
    _ = parser.add_argument('bundle', type=Path, help='the client bundle directory to reach the table over')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    settings_file = cast('Path', args.settings)
    bundle_dir = cast('Path', args.bundle)
    try:
        configured = Settings.load(settings_file)
        return run(
            configured.client(bundle_dir),
            database=configured.database,
            out=sys.stdout,
            age=configured.expiry_age,
            records=configured.history_records,
            bound=configured.largest_row_bytes,
            expire=True,
            ceiling=True,
            yes=True,
        )
    except StateError as exc:
        log.error('%s', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
