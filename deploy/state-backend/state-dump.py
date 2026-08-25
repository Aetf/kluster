#!/usr/bin/python3
"""Dump the Pulumi state, encrypt it to the age recipients, upload to B2.

Runs on the appliance from a systemd timer (physical/state-backend.md §5).
Standard library only: FCOS ships python3 and nothing else is installed here.

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


def dump(destination: Path) -> None:
    """pg_dump through the container's local socket, straight into age."""
    recipients: list[str] = []
    for line in RECIPIENTS.read_text().splitlines():
        if line.strip():
            recipients += ['-r', line.strip()]

    with destination.open('wb') as out:
        pg = sp.Popen(
            ['podman', 'exec', CONTAINER, 'pg_dump', '-Fc', '-U', os.environ['PG_ROLE'], os.environ['PG_DATABASE']],
            stdout=sp.PIPE,
        )
        assert pg.stdout is not None
        age = sp.Popen(['/opt/bin/age', '--encrypt', *recipients], stdin=pg.stdout, stdout=out)
        pg.stdout.close()
        age_status = age.wait()
        pg_status = pg.wait()
    if pg_status != 0:
        raise SystemExit(f'pg_dump failed ({pg_status})')
    if age_status != 0:
        raise SystemExit(f'age failed ({age_status})')


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
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'state.dump.age'
        dump(path)
        upload(path, name)
    print(f'uploaded {name}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
