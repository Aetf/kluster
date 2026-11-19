"""A B2 account's native API as `curl` reaches it from the backup bucket's shell client, answered from a directory.

The scripts that source `components/backup/shell/b2.sh` -- the metrics stores'
and the etcd restore-verify -- reach B2 through `curl` alone, so the suites
that run them put `FAKE_CURL` on `PATH` ahead of the image's tools, under the
name `curl`, and the fake answers each call from a state directory:

-   `bucket/` holds the bucket's current files, by name;
-   `authorization.json` is what `b2_authorize_account` answers (`authorization`);
-   `metrics` is a store's exposition, answered at any `/metrics`;
-   `down` makes B2 answer nothing, and `list-down` makes it answer the
    authorization and nothing after;
-   `page-size` holds the most names one `b2_list_file_names` page carries,
    below the `maxFileCount` the call asks for, as B2 may answer.

It reads its arguments as the scripts pass them, each run's appended to
`curl.argv`: a `-H @<file>` reads one header a line, as `curl` does, and `-o`
writes the answer to a file. B2's answers are the shapes its v4 API documents
for `b2_authorize_account`, `b2_list_file_names`, `b2_download_file_by_id`,
`b2_get_upload_url` and `b2_upload_file`; a file's id is `file_id` of its name.
The key it admits is `FAKE_KEY`, restricted through `bucketIds`, which B2
authorizes on v4 alone and refuses with a 400 on an earlier version. An error
is `curl --fail`'s exit status, 22.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = (
    'AUTHORIZATION_V4',
    'BUCKET',
    'FAKE_CURL',
    'FAKE_KEY',
    'FAKE_TOKENS',
    'authorization',
    'fake_env',
    'file_id',
    'install',
)

FAKE_CURL = """
import base64, hashlib, json, os, sys
from pathlib import Path
from urllib.parse import urlsplit

state = Path(os.environ['FAKE_STATE'])
with (state / 'curl.argv').open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
args, headers, body, url, out = sys.argv[1:], {}, None, None, None
while args:
    arg = args.pop(0)
    if arg == '-H':
        value = args.pop(0)
        lines = Path(value[1:]).read_text().splitlines() if value.startswith('@') else [value]
        for line in lines:
            name, found = line.split(':', 1)
            headers[name.strip().lower()] = found.strip()
    elif arg in ('--data', '--data-binary'):
        value = args.pop(0)
        body = Path(value[1:]).read_text() if value.startswith('@') else value
    elif arg == '-o':
        out = Path(args.pop(0))
    elif arg == '--max-time':
        args.pop(0)
    elif not arg.startswith('-'):
        url = arg
bucket = state / 'bucket'
path = urlsplit(url).path


def refuse(status):
    print(f'curl: (22) The requested URL returned error: {status}', file=sys.stderr)
    sys.exit(22)


def emit(data):
    if out is not None:
        out.write_bytes(data)
    else:
        sys.stdout.buffer.write(data)
    sys.exit(0)


def answer(found):
    emit((json.dumps(found, indent=2) + '\\n').encode())


def file_id(name):
    return 'id-' + hashlib.sha1(name.encode()).hexdigest()


def names():
    return sorted(str(it.relative_to(bucket)) for it in bucket.rglob('*') if it.is_file())


if path == '/metrics':
    emit((state / 'metrics').read_bytes())
if (state / 'down').exists():
    refuse(503)
authorization = json.loads((state / 'authorization.json').read_text())
if path == '/b2api/v4/b2_authorize_account':
    if headers.get('authorization') != 'Basic ' + base64.b64encode(os.environ['FAKE_KEY'].encode()).decode():
        refuse(401)
    answer(authorization)
if path.endswith('/b2_authorize_account'):
    refuse(400)
if (state / 'list-down').exists():
    refuse(503)
if headers.get('authorization') not in (authorization['authorizationToken'], 'upload-token'):
    refuse(401)
if path == '/b2api/v4/b2_list_file_names':
    request = json.loads(body)
    assert request['bucketId'] == os.environ['FAKE_BUCKET_ID']
    start = request.get('startFileName', '')
    listed = [name for name in names() if name.startswith(request['prefix']) and name >= start]
    size = request['maxFileCount']
    if (state / 'page-size').exists():
        size = min(size, int((state / 'page-size').read_text()))
    page, rest = listed[:size], listed[size:]
    answer(
        {
            'files': [{'fileId': file_id(name), 'fileName': name, 'action': 'upload'} for name in page],
            'nextFileName': rest[0] if rest else None,
        }
    )
if path == '/b2api/v4/b2_download_file_by_id':
    wanted = json.loads(body)['fileId']
    found = [name for name in names() if file_id(name) == wanted]
    if not found:
        refuse(404)
    emit((bucket / found[0]).read_bytes())
if path.startswith(f"/file/{os.environ['FAKE_BUCKET_NAME']}/"):
    found = bucket / path.removeprefix(f"/file/{os.environ['FAKE_BUCKET_NAME']}/")
    if not found.is_file():
        refuse(404)
    emit(found.read_bytes())
if path == '/b2api/v4/b2_get_upload_url':
    answer({'bucketId': os.environ['FAKE_BUCKET_ID'], 'uploadUrl': 'https://pod.b2.invalid/upload', 'authorizationToken': 'upload-token'})
if path == '/upload':
    assert headers['authorization'] == 'upload-token'
    assert headers['x-bz-content-sha1'] == hashlib.sha1(body.encode()).hexdigest()
    target = bucket / headers['x-bz-file-name']
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    answer({'fileName': headers['x-bz-file-name']})
refuse(400)
"""

#: What `b2_authorize_account` v4 answers, recorded whole from the example on
#: the operation's reference page (https://www.backblaze.com/apidocs/b2-authorize-account):
#: the storage API's addresses under `apiInfo.storageApi`, with the key's
#: restriction beside them in `allowed`, the Partner API's under `groupsApi`,
#: and the token at the top. The fake answers it with `allowed.buckets`
#: replaced by the buckets its case restricts the key to (`authorization`).
AUTHORIZATION_V4: dict[str, Any] = {
    'accountId': 'ACCOUNT_ID',
    'apiInfo': {
        'storageApi': {
            'absoluteMinimumPartSize': 5000000,
            'apiUrl': 'https://api001.backblazeb2.com',
            'allowed': {
                'buckets': [
                    {'id': 'bucketId1', 'name': 'bucketIdName1'},
                    {'id': 'bucketId2', 'name': 'bucketIdName2'},
                    {'id': 'bucketId3', 'name': None},
                ],
                'capabilities': [
                    'listAllBucketNames',
                    'listBuckets',
                    'writeBuckets',
                    'readBuckets',
                    'listFiles',
                    'readFiles',
                    'shareFiles',
                    'writeFiles',
                    'deleteFiles',
                    'readBucketEncryption',
                    'writeBucketEncryption',
                    'readBucketNotifications',
                    'writeBucketNotifications',
                    'bypassGovernance',
                    'readBucketRetentions',
                    'writeBucketRetentions',
                    'readFileRetentions',
                    'writeFileRetentions',
                    'readFileLegalHolds',
                    'writeFileLegalHolds',
                ],
                'namePrefix': None,
            },
            'downloadUrl': 'https://f001.backblazeb2.com',
            'recommendedPartSize': 100000000,
            's3ApiUrl': 'https://s3.us-west-001.backblazeb2.com',
        },
        'groupsApi': {
            'capabilities': ['all'],
            'groupsApiUrl': 'https://apiNNN.backblazeb2.com',
            'infoType': 'groupsApi',
        },
    },
    'applicationKeyExpirationTimestamp': None,
    'authorizationToken': 'AUTHORIZATION_TOKEN',
}

#: The bucket the fake's key is restricted to, the one whose id its listing
#: reads and whose name its downloads by name are addressed by.
BUCKET = {'id': 'bucket-id', 'name': 'bucket-name'}

#: The key the fake admits, `id:secret`, and the tokens it hands out: every
#: credential a script holds.
FAKE_KEY = ('key-id', 'key-secret')
FAKE_TOKENS = (AUTHORIZATION_V4['authorizationToken'], 'upload-token')


def file_id(name: str) -> str:
    """The id the fake gives the file `name`, which it lists beside the name and downloads by."""
    return 'id-' + hashlib.sha1(name.encode()).hexdigest()


def authorization(buckets: Sequence[Mapping[str, str | None]]) -> dict[str, Any]:
    """`AUTHORIZATION_V4` with the key restricted to `buckets`."""
    answer = copy.deepcopy(AUTHORIZATION_V4)
    answer['apiInfo']['storageApi']['allowed']['buckets'] = [dict(it) for it in buckets]
    return answer


def install(state: Path, fakes: Path, buckets: Sequence[Mapping[str, str | None]] = (BUCKET,)) -> None:
    """The fake as `fakes/curl`, answering from `state`, with the key restricted to `buckets`."""
    (state / 'bucket').mkdir(parents=True, exist_ok=True)
    fakes.mkdir(parents=True, exist_ok=True)
    _ = (state / 'authorization.json').write_text(json.dumps(authorization(buckets), indent=2))
    binary = fakes / 'curl'
    _ = binary.write_text(f'#!{sys.executable}\n{FAKE_CURL}')
    binary.chmod(0o755)


def fake_env(state: Path, key: str = ':'.join(FAKE_KEY)) -> dict[str, str]:
    """What the fake reads: the state directory, the key B2 admits, and the bucket it is restricted to."""
    return {
        'FAKE_STATE': str(state),
        'FAKE_KEY': key,
        'FAKE_BUCKET_ID': BUCKET['id'],
        'FAKE_BUCKET_NAME': BUCKET['name'],
    }
