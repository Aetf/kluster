#!/usr/bin/python3
"""Dump the Pulumi state, verify it, encrypt it to the age recipients, upload to B2.

Runs on the appliance from a systemd timer (physical/state-backend.md §5).
Standard library only: FCOS ships python3 and nothing else is installed here.

**The nightly object is verified the same way the operator's is.** A dump is
listed with `pg_restore --list` and a listing naming no table fails the run,
which is what catches a dump of a database that has lost its tables — the
shape a box produces after a replacement nobody followed with a restore. The
next reader of an object nobody listed is the restore that needed it, a
retention window later. That check reads an archive rather than a stream, so
the dump lands in the clear beside its own ciphertext for the two steps that
read it instead of being piped straight into `age`.

The upload credential holds `writeFiles` alone — it cannot list, read, or
delete, which is why the bucket is addressed by id (listing is not permitted)
and why pruning is a bucket lifecycle rule rather than this script's job.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import subprocess as sp
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

AUTHORIZE_URL = 'https://api.backblazeb2.com/b2api/v3/b2_authorize_account'
RECIPIENTS = Path('/etc/kluster/age-recipients.txt')
CONTAINER = 'pgstate'

#: Where the archive and its ciphertext sit while the run needs them. Not
#: /tmp: that is a tmpfs sized from a 1 GB box's memory, and this holds the
#: whole state twice over for the length of one dump. The unit that runs this
#: sets no `PrivateTmp=`, which would otherwise settle the same question from
#: the other side: `disconnected` backs the service's /var/tmp with a tmpfs
#: too, and plain `yes` hands it a private /var/tmp instead of the host's
#: (butane.yaml.j2).
SPOOL = '/var/tmp'

#: What an entry line calls a table in `pg_restore --list` output, and the
#: word that follows it when the entry is the rows rather than the definition.
TABLE = 'TABLE'
DATA = 'DATA'


def _request(
    url: str,
    *,
    headers: dict[str, str],
    data: bytes | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Every B2 call goes through here.

    All three of them are JSON-in/JSON-out over the same auth header, upload
    included -- b2_upload_file differs only in carrying a raw body and needing
    a longer deadline, both of which are arguments. There is no second style.
    """
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def tables(listing: str) -> int:
    """How many tables a `pg_restore --list` output names.

    An entry line is `<id>; <catalogue oid> <oid> <what> <schema> <name>
    <owner>`, and `<what>` is one word for a table's definition and two —
    `TABLE DATA` — for its rows. Comment lines, which is the whole header,
    start with the semicolon.

    **This is the operator's `state.tables` with the names counted instead of
    returned**, down to the same set: a table contributes its definition and
    its rows as two entries, so counting entries would answer twice what the
    other side answers once. The grammar is copied line for line because
    nothing of this repository is installed on the appliance, and the
    alternative to a copy is no check here at all;
    `tests/test_state_dump.py` runs both over one table of listings and
    compares the numbers, not their truthiness.
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
    return len(found)


def dump(destination: Path) -> None:
    """pg_dump to an archive, list it, then encrypt the archive to `destination`.

    Three steps rather than one pipeline: a stream cannot be listed, and an
    unlistable object is one nobody discovers until a restore needs it. The
    plaintext is unlinked as soon as the ciphertext exists, so the peak is one
    copy of the state plus its (already compressed) encryption.
    """
    recipients: list[str] = []
    for line in RECIPIENTS.read_text().splitlines():
        if line.strip():
            recipients += ['-r', line.strip()]

    archive = destination.parent / 'state.dump'
    with archive.open('wb') as out:
        status = sp.run(
            ['podman', 'exec', CONTAINER, 'pg_dump', '-Fc', '-U', os.environ['PG_ROLE'], os.environ['PG_DATABASE']],
            stdout=out,
            check=False,
        ).returncode
    if status != 0:
        raise SystemExit(f'pg_dump failed ({status})')

    # `pg_restore` comes from the same container as `pg_dump`; reading the
    # archive on standard input is what saves mounting the spool into it.
    # What this catches is a listing that names no table -- a dump of a
    # database with nothing in it. It is not a truncation check: a
    # custom-format archive keeps its table of contents at the head, so a file
    # cut to a few kilobytes still lists.
    with archive.open('rb') as handle:
        listing = sp.run(
            ['podman', 'exec', '-i', CONTAINER, 'pg_restore', '--list'],
            stdin=handle,
            capture_output=True,
            text=True,
            check=False,
        )
    if listing.returncode != 0:
        # With the output captured, what pg_restore said is the only account
        # of why it refused the archive, and a status on its own sends the
        # reader to a box nobody logs into to reproduce it by hand.
        raise SystemExit(f'pg_restore --list failed ({listing.returncode}): {listing.stderr.strip()}')
    if not tables(listing.stdout):
        raise SystemExit('the archive lists no tables, so it is not a dump of the state backend')

    with archive.open('rb') as handle, destination.open('wb') as out:
        status = sp.run(['/opt/bin/age', '--encrypt', *recipients], stdin=handle, stdout=out, check=False).returncode
    if status != 0:
        raise SystemExit(f'age failed ({status})')
    archive.unlink()


def upload(path: Path, name: str) -> None:
    key_id = os.environ['B2_KEY_ID']
    key = os.environ['B2_KEY']
    bucket_id = os.environ['B2_BUCKET_ID']

    basic = base64.b64encode(f'{key_id}:{key}'.encode()).decode()
    account = _request(AUTHORIZE_URL, headers={'Authorization': f'Basic {basic}'})
    api_url: str = account['apiInfo']['storageApi']['apiUrl']
    token: str = account['authorizationToken']

    upload_target = _request(
        f'{api_url}/b2api/v3/b2_get_upload_url',
        headers={'Authorization': token, 'Content-Type': 'application/json'},
        data=json.dumps({'bucketId': bucket_id}).encode(),
    )

    body = path.read_bytes()
    _ = _request(
        upload_target['uploadUrl'],
        headers={
            'Authorization': upload_target['authorizationToken'],
            'X-Bz-File-Name': urllib.parse.quote(name),
            'Content-Type': 'application/octet-stream',
            'Content-Length': str(len(body)),
            'X-Bz-Content-Sha1': hashlib.sha1(body).hexdigest(),
        },
        data=body,
        timeout=600,
    )


def main() -> int:
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    name = f'{os.environ["B2_PREFIX"]}/{stamp}.dump.age'
    with tempfile.TemporaryDirectory(dir=SPOOL) as tmp:
        path = Path(tmp) / 'state.dump.age'
        dump(path)
        upload(path, name)
    print(f'uploaded {name}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
