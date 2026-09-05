"""Taking the Pulumi state out of the appliance, and putting it back.

The box dumps itself nightly (`deploy/state-backend/state-dump.py`, §5); this
module is the operator's side of the same artefact — an on-demand dump, and
the only thing that reads one back. Both halves of every playbook that
rebuilds the box are built out of it: a Postgres major upgrade is a dump, a
re-provision and a restore (§7.2), and the quarterly drill is the same
sequence against a scratch box (§7.3).

**The artefact is the same artefact.** A dump written here is `pg_dump -Fc`
under `age`, encrypted to the recipients the escrow names — the ones the
appliance itself encrypts to, taken from the same function (`config`). A
restore therefore does not care which of the two produced its input, and an
operator's dump is exactly as recoverable as a nightly one.

**Verification is part of the command, not of the playbook.** A dump of a
database with no tables in it has a plausible size and a plausible name; what
it does not have is a table of contents naming anything, so every dump is
listed before it is called one — the appliance's own timer included.
A restore ends by asking the `pulumi` CLI what the restored backend serves,
because a database that is full of rows but cannot be logged in to has
restored nothing anyone needs.

**Bytes, which is why this is not `credentials.age`.** That wrapper is text
in, text out, and a custom-format archive is neither. The invocations here
keep its discipline — the pinned binary, identities on standard input,
recipients on argv — and add the file-to-file form the archive needs.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess as sp
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from kluster.lib import config as lib_config
from kluster.scripts.credentials import age, lifecycle, pulumi_config

from . import config, settings

log = logging.getLogger(__name__)

#: The client tools. `pg_dump` and `pg_restore` come from a Postgres client
#: package rather than from `mise.toml`: they are the one part of this path
#: the appliance cannot supply, and libpq is what reads the client bundle.
PG_DUMP = 'pg_dump'
PG_RESTORE = 'pg_restore'

#: The state is tens of megabytes over a TLS connection to Phoenix, so these
#: are transfer budgets rather than formalities. `age` gets less than the
#: transfer because it is local work on a file that already exists.
TRANSFER_TIMEOUT = 1800
AGE_TIMEOUT = 600
LISTING_TIMEOUT = 120

#: What the two file formats announce themselves as, in their first bytes.
#: The appliance writes `age` binary output; the escrow's ciphertexts are
#: armoured, and a hand-decrypted archive is the `pg_dump` custom format.
AGE_MAGIC = b'age-encryption.org/'
ARMOUR_MAGIC = age.ARMOR_BEGIN.encode()
ARCHIVE_MAGIC = b'PGDMP'

#: What a table entry says it is in a `pg_restore --list` line, and the word
#: that follows it when the entry is the rows rather than the definition.
TABLE = 'TABLE'
DATA = 'DATA'


class StateError(RuntimeError):
    """A dump or a restore did not happen, and says which step refused."""


def dump_name(now: dt.datetime | None = None) -> str:
    """What a dump is called when the operator does not name one.

    The appliance's own objects are `<prefix>/<stamp>.dump.age` (§5); this
    keeps the stamp and the suffixes so that a local file and a B2 object
    sort and read the same way, and prefixes the appliance's name because
    this one lands in whatever directory the operator is standing in.
    """
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime('%Y%m%dT%H%M%SZ')
    return f'{settings.NAME}-{stamp}.dump.age'


@dataclass(frozen=True)
class Connection:
    """What it takes to reach the backend: a string, and where the bundle is.

    Two halves because the connection string is machine-independent by
    design (`config.ClientBundle.url`) — the certificate, its key and the CA
    are named by the `PGSSL*` variables, which libpq and the driver behind
    Pulumi's Postgres backend both read. Carried together so that no caller
    can pass one without the other: a URL on its own authenticates as nobody
    and is refused by the box.
    """

    url: str
    env: dict[str, str]


#: The connection-string parameters that name a file. A bundle written before
#: the paths moved into the environment has them, and they win over the
#: variables — which is what keeps such a bundle working, and why the fix for
#: a checkout that has since moved is to rewrite the file rather than to set
#: a variable.
_FILE_PARAMETERS = ('sslrootcert=', 'sslcert=', 'sslkey=')


def connection(bundle_dir: Path) -> Connection:
    """The connection string beside a client bundle, plus that bundle's files.

    The URL is the same file `mise.toml` turns into `PULUMI_BACKEND_URL`, so a
    dump talks to the backend the operator's `pulumi` runs talk to, over the
    same certificate. The variables are derived from where the file was
    actually found rather than from where it was looked for, so a bundle still
    in its pre-`.credentials/` location is used with its own certificates.
    """
    path = lifecycle.backend_url_file(bundle_dir)
    if path is None:
        raise StateError(
            f'no {bundle_dir / config.URL_FILE}: `state-backend bundle operator --address <ip>` writes one'
        )
    url = path.read_text().strip()
    if not url:
        raise StateError(f'{path} is empty; re-run `state-backend bundle operator --address <ip>`')
    if any(parameter in url for parameter in _FILE_PARAMETERS):
        log.warning(
            '%s names its certificate files inside the URL, so this checkout cannot be moved without '
            'rewriting it: `state-backend bundle operator --address <ip>` writes the portable form',
            path,
        )
    return Connection(url=url, env=config.ssl_env(path.parent))


def endpoint(url: str) -> str:
    """A connection string with its query dropped — what a log line may say.

    The query is the TLS mode, and on a bundle written before the certificate
    paths moved into the environment it is three absolute paths as well:
    nothing secret, nothing informative, and longer than everything else the
    run prints.
    """
    return url.split('?', 1)[0]


def _run(
    argv: Sequence[str],
    *,
    what: str,
    timeout: int,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """One external tool, with its failure turned into something readable.

    Every caller here is a long-running local process or a network transfer,
    so a timeout is a real outcome rather than a guard, and it says which
    step ran out rather than surfacing a `TimeoutExpired` from three frames
    down.

    `env` is overlaid on this process's environment rather than replacing it:
    what a caller adds is the bundle's three `PGSSL*` paths, while `PATH`, a
    home directory and the rest still have to reach the tool.
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
        raise StateError(
            f'{argv[0]} is not on PATH: {PG_DUMP}/{PG_RESTORE} come from a Postgres client package, '
            f'and {age.BINARY} is pinned in mise.toml (`mise x -- ...`)'
        ) from exc
    except sp.TimeoutExpired as exc:
        raise StateError(f'{what} did not finish within {timeout}s') from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        # The pg tools echo the failing statement after the diagnosis, so the
        # last line is as likely to be a fragment of SQL as the error itself.
        found = next((line for line in reversed(detail) if 'error' in line.lower()), None)
        raise StateError(f'{what} failed: {found or (detail[-1] if detail else f"exit {proc.returncode}")}')
    return proc.stdout


def pg_dump(target: Connection, destination: Path) -> None:
    """`pg_dump -Fc` over the bundle's connection, into a local file.

    The custom format because that is what the appliance's own timer writes
    and what `pg_restore` can list and reorder; no `--no-owner` and no
    `--no-privileges`, because the roles a dump names are certificate
    subjects that exist on every box (`ci`, `operator`) and flattening
    ownership would hand CI's tables to the operator.
    """
    log.info(
        'running %s -Fc against %s — tens of MB over TLS, expect seconds to minutes',
        PG_DUMP,
        endpoint(target.url),
    )
    _ = _run(
        [
            PG_DUMP,
            '--format=custom',
            # A password prompt in a script is a hang, and there is no
            # password to type: the client certificate is the credential.
            '--no-password',
            f'--file={destination}',
            f'--dbname={target.url}',
        ],
        what=f'{PG_DUMP} against {endpoint(target.url)}',
        timeout=TRANSFER_TIMEOUT,
        env=target.env,
    )
    log.info('wrote %.1f MiB of archive', destination.stat().st_size / 2**20)


def tables(listing: str) -> list[str]:
    """The tables a `pg_restore --list` output names, as `schema.name`.

    An entry line is `<id>; <catalogue oid> <oid> <what> <schema> <name>
    <owner>`, and `<what>` is one word for a table's definition and two —
    `TABLE DATA` — for its rows. Comment lines, which is the whole header,
    start with the semicolon.
    """
    found: set[str] = set()
    for line in listing.splitlines():
        entry = line.split(';', 1)
        if line.startswith(';') or len(entry) != 2:
            continue
        parts = entry[1].split()
        if len(parts) < 4 or parts[2] != TABLE:
            continue
        start = 4 if parts[3] == DATA else 3
        if len(parts) >= start + 2:
            found.add('.'.join(parts[start : start + 2]))
    return sorted(found)


def verify_dump(archive: Path) -> list[str]:
    """The tables the archive carries. A dump that cannot list them is not a dump.

    `pg_restore --list` reads the archive's own table of contents, so a
    decryption that produced something else, a file that is not an archive at
    all, or an archive of a database that has lost its tables fails here
    rather than at the restore that needed it. It is not a truncation check:
    the table of contents sits at the head of a custom-format archive, so a
    file cut short still lists what the whole one would have.
    """
    log.info('checking the archive: asking %s to list what it contains', PG_RESTORE)
    listing = _run(
        [PG_RESTORE, '--list', str(archive)],
        what=f'{PG_RESTORE} --list {archive}',
        timeout=LISTING_TIMEOUT,
    )
    found = tables(listing)
    if not found:
        raise StateError(f'{archive} lists no tables, so it is not a dump of the state backend')
    log.info('the archive carries %d table(s): %s', len(found), ', '.join(found))
    return found


def pg_restore(target: Connection, archive: Path) -> None:
    """Restore the archive over the bundle's connection, all of it or none of it.

    `--single-transaction` is the whole failure model: an error anywhere
    rolls the database back to what it was, so a restore that goes wrong
    leaves a box to re-run against rather than a half-populated backend that
    `pulumi` will happily read.

    `--clean --if-exists` is what lets the archive land on a provisioned
    box at all: Ignition's first boot already creates an empty
    `pulumi_state`, so replaying the archive's own CREATE would abort the
    transaction on every fresh appliance. The drops run inside the same
    transaction, so the all-or-nothing shape survives.
    """
    log.info('restoring into %s — this writes the whole archive in one transaction', endpoint(target.url))
    _ = _run(
        [
            PG_RESTORE,
            '--single-transaction',
            '--clean',
            '--if-exists',
            '--no-password',
            f'--dbname={target.url}',
            str(archive),
        ],
        what=f'{PG_RESTORE} into {endpoint(target.url)}',
        timeout=TRANSFER_TIMEOUT,
        env=target.env,
    )
    log.info('the archive went in')


def stacks(target: Connection) -> list[str]:
    """Every stack the backend serves, asked through the CLI that will use it.

    The verification a restore ends on, and deliberately not a SQL query: what
    has to be true afterwards is that `pulumi` can log in to this backend and
    read what is in it, which is a different claim from "the rows are there".

    `--all` because the question is about the backend rather than about the
    project directory the answer is asked from.
    """
    log.info('asking pulumi which stacks %s serves', endpoint(target.url))
    try:
        printed = pulumi_config.run_pulumi(
            ['stack', 'ls', '--all', '--json'],
            cwd=pulumi_config.project_dir(),
            env={'PULUMI_BACKEND_URL': target.url, **target.env},
            stdin=None,
        )
    except pulumi_config.SlotRefused as exc:
        raise StateError(str(exc)) from exc
    return sorted(_stack_names(printed))


def _stack_names(printed: str) -> list[str]:
    """The names in `pulumi stack ls --json` output, or a refusal quoting it.

    The boundary for that command: a restore is verified by this list, so an
    entry that carries no name must not become an empty string that counts as
    a stack — a backend that answered nothing useful would then read as one
    holding state.
    """
    try:
        listing: object = json.loads(printed or '[]')
    except ValueError as exc:
        raise StateError(f'`pulumi stack ls --json` did not print JSON: {printed[:120]!r}') from exc
    if not isinstance(listing, list):
        raise StateError(f'`pulumi stack ls --json` printed a {type(listing).__name__}, not a list of stacks')
    names: list[str] = []
    for index, entry in enumerate(cast('list[object]', listing)):
        name = cast('dict[str, object]', entry).get('name') if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            raise StateError(f'`pulumi stack ls --json` entry {index} carries no name, and is {entry!r}')
        names.append(name)
    return names


def encrypted(path: Path) -> bool:
    """Whether this file is age output rather than a bare archive.

    Read off the first bytes rather than the file name: what an operator
    downloads from B2 keeps the name the box gave it, what a drill writes
    keeps whatever the workflow called it, and a wrong guess here is either
    a decryption of plaintext or a restore of ciphertext.
    """
    if not path.is_file():
        raise StateError(f'no dump at {path}')
    # The first bytes, not the file: this runs before a restore, and the input
    # is a dump of the whole Pulumi state.
    with path.open('rb') as handle:
        head = handle.read(len(ARMOUR_MAGIC))
    if head.startswith(AGE_MAGIC) or head.startswith(ARMOUR_MAGIC):
        return True
    if head.startswith(ARCHIVE_MAGIC):
        return False
    raise StateError(f'{path} is neither an age file nor a pg_dump custom-format archive')


def encrypt(source: Path, destination: Path, recipients: Sequence[str]) -> None:
    """Age-encrypt an archive to every recipient given.

    Multi-recipient is the design (§5): the current backup generation and the
    one before it, so any object still in retention opens with either.
    """
    if not recipients:
        raise StateError('no recipient to encrypt the dump to; `credentials derived check` says what is missing')
    argv = [age.BINARY, '--encrypt']
    for value in recipients:
        argv += ['--recipient', value]
    log.info('encrypting the archive to %d recipient(s) with %s', len(recipients), age.BINARY)
    _ = _run(
        [*argv, '--output', str(destination), str(source)],
        what=f'{age.BINARY} --encrypt',
        timeout=AGE_TIMEOUT,
    )


def decrypt(source: Path, destination: Path, identities: Sequence[str]) -> None:
    """Open an age-encrypted dump with whichever identity fits.

    Several are passed for the same reason a re-wrap passes several: a dump
    in retention was written under the current generation or the previous
    one, and which is not knowable from the file. The private halves travel
    on standard input, so none of them lands on a filesystem.
    """
    if not identities:
        raise StateError(f'no identity to open {source} with')
    log.info('decrypting %s, trying %d identity/identities', source, len(identities))
    _ = _run(
        [age.BINARY, '--decrypt', '--identity', '-', '--output', str(destination), str(source)],
        what=f'{age.BINARY} --decrypt',
        timeout=AGE_TIMEOUT,
        stdin=''.join(f'{value.strip()}\n' for value in identities),
    )


def identity_file(path: Path) -> tuple[str, ...]:
    """The age identities in a file — the drill key's delivery form.

    The unattended drill (§7.3) holds one key in a repository secret and no
    kit at all, so it writes that key to a file and names it. The file is a
    line per value with `#` comments, which is what `age-keygen` prints, and
    is read by the reader every such file in this repository is read by.
    """
    try:
        return lib_config.lines(path, 'the age identity file')
    except FileNotFoundError as exc:
        raise StateError(f'no age identity file at {path}') from exc
    except OSError as exc:
        raise StateError(f'{path} cannot be read: {exc}') from exc
    except ValueError as exc:
        raise StateError(f'{path} holds no age identity') from exc
