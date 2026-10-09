"""A fake of the B2 native API, shared by the suites that mint against it.

Its own named module rather than a `conftest`, for the reason `memory_kit` is
one: test modules import it, and every directory with tests may have a
`conftest` of its own on `sys.path`.

What the fake encodes is authorization, because that is what the module under
test is about:

-   every endpoint checks the capability the API reference says it needs, so a
    credential minted narrower than its job shows up as a refusal rather than
    as a call that quietly does nothing;
-   an authorization token is a token *of a key*, so deleting a key stops the
    token that key issued. This is what makes "which credential runs the
    retirement" a real question instead of a stylistic one;
-   `b2_list_keys` and `b2_list_file_names` page, and the page is the server's
    choice — a caller that reads the first page only sees part of the account,
    or calls an old dump the newest;
-   a key confined to a bucket and a prefix is refused a listing of any other
    bucket, or of a prefix outside its own, and `b2_authorize_account` names
    the bucket such a key is confined to — which is what makes "verified by
    listing the prefix as itself" a proof of the grant rather than of the
    key's existence, and what lets a key with no `listBuckets` learn its
    bucket's id;
-   `b2_create_key` refuses a capability name B2 does not have, so a
    misspelled capability is a refused mint rather than a key that quietly
    lacks the grant. The answer is the call's documented generic refusal,
    `400 bad_request`.

The capability names, and which endpoint needs which, come from the API
reference. Everything else above is what the first live run against the
account has to confirm; it is written down here
rather than left out because a fake that is silent about it lets the code
come to depend on the opposite, and the ratchet only tightens
(`docs/framework/testing.md` §4).

Every contract value the fake checks a caller against — the authorization URL,
the capability names — is written here as Backblaze writes it, and none is
imported from the module under test: a fake that took them from `b2` would
agree with a misspelling of either, and every suite minting against it would
stay green.
"""

from __future__ import annotations

import itertools
import json as jsonlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

from kluster.scripts.credentials import b2

#: Where `b2_authorize_account` answers for version 3 of the native API: the one
#: fixed host, before the account's own `apiUrl` is known.
AUTHORIZE_URL = 'https://api.backblazeb2.com/b2api/v3/b2_authorize_account'

ACCOUNT_ID = 'account-1'

#: Where this fake account's storage API answers. `b2_authorize_account` hands
#: the caller a per-account host, and every later call goes to that host rather
#: than to the authorization endpoint.
API_URL = 'https://api001.backblazeb2.com'

#: The capability every endpoint the minter touches requires, from the API
#: reference. A key without it gets 401 `unauthorized`, which is the same
#: answer the real service gives.
REQUIRED: dict[str, str] = {
    'b2_list_buckets': 'listBuckets',
    'b2_create_bucket': 'writeBuckets',
    'b2_update_bucket': 'writeBuckets',
    'b2_create_key': 'writeKeys',
    'b2_list_keys': 'listKeys',
    'b2_delete_key': 'deleteKeys',
    'b2_list_file_names': 'listFiles',
}

#: What the account master key carries: "all capabilities", in the words of
#: backblaze.com/docs/cloud-storage-application-keys, which is every row of the
#: table at backblaze.com/docs/cloud-storage-application-key-capabilities, in
#: that table's order -- the file capabilities the account-wide rows of the
#: register never carry included.
MASTER_CAPABILITIES: tuple[str, ...] = (
    'listKeys',
    'writeKeys',
    'deleteKeys',
    'listBuckets',
    'listAllBucketNames',
    'readBuckets',
    'writeBuckets',
    'deleteBuckets',
    'readBucketRetentions',
    'writeBucketRetentions',
    'readBucketEncryption',
    'writeBucketEncryption',
    'listFiles',
    'readFiles',
    'shareFiles',
    'writeFiles',
    'deleteFiles',
    'readFileLegalHolds',
    'writeFileLegalHolds',
    'readFileRetentions',
    'writeFileRetentions',
    'bypassGovernance',
    'readBucketReplications',
    'writeBucketReplications',
    'readBucketNotifications',
    'writeBucketNotifications',
    'readBucketLogging',
    'writeBucketLogging',
)


@dataclass
class Key:
    """One application key, as the account holds it."""

    key_id: str
    secret: str
    name: str
    capabilities: tuple[str, ...]
    bucket_id: str | None = None
    name_prefix: str | None = None

    def listed(self) -> dict[str, Any]:
        """The key as `b2_list_keys` describes it."""
        return {
            'accountId': ACCOUNT_ID,
            'applicationKeyId': self.key_id,
            'keyName': self.name,
            'capabilities': list(self.capabilities),
            'bucketId': self.bucket_id,
            'namePrefix': self.name_prefix,
        }


class Refused(Exception):
    """Raised inside the fake and turned into the response the API sends."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status: int = status
        self.code: str = code
        self.message: str = message


@dataclass
class FakeApi:
    """One B2 account: its keys, its buckets, and the tokens it has issued."""

    keys: dict[str, Key] = field(default_factory=dict[str, Key])
    buckets: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    #: authorization token -> the key id it was issued to.
    tokens: dict[str, str] = field(default_factory=dict[str, str])
    counter: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    #: Every call, as the API name the body was posted to.
    calls: list[str] = field(default_factory=list[str])
    #: The bodies of those calls, so a test can check what was asked for and
    #: not only what came back.
    posted: list[tuple[str, dict[str, Any]]] = field(default_factory=list[tuple[str, dict[str, Any]]])
    #: How many keys one `b2_list_keys` page may hold. The server picks this,
    #: so a caller cannot assume its own `maxKeyCount` was honored.
    page_limit: int = 1000
    #: bucket id -> the object names it holds.
    objects: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    #: How many names one `b2_list_file_names` page may hold, which the server picks.
    file_page_limit: int = 1000
    #: Every listing served, as (the key it was served to, bucket, prefix).
    listings: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])

    def __post_init__(self) -> None:
        self.keys[ACCOUNT_ID] = Key(
            key_id=ACCOUNT_ID,
            secret='master-key',
            name='master',
            capabilities=MASTER_CAPABILITIES,
        )

    @property
    def master(self) -> Key:
        return self.keys[ACCOUNT_ID]

    def add_key(
        self,
        name: str,
        capabilities: tuple[str, ...] = b2.CAPABILITIES,
        *,
        bucket_id: str | None = None,
        name_prefix: str | None = None,
    ) -> Key:
        """A key that already exists — a console visit, or a run that died."""
        index = next(self.counter)
        key = Key(
            key_id=f'key-{index}',
            secret=f'secret-of-key-{index}',
            name=name,
            capabilities=capabilities,
            bucket_id=bucket_id,
            name_prefix=name_prefix,
        )
        self.keys[key.key_id] = key
        return key

    def named(self, name: str) -> list[str]:
        """The ids of every key carrying this name; B2 names are not unique."""
        return sorted(key.key_id for key in self.keys.values() if key.name == name)

    # -- the wire ---------------------------------------------------------

    def _envelope(self, status: int, body: dict[str, Any]) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response._content = jsonlib.dumps(body).encode()  # pyright: ignore[reportPrivateUsage]
        return response

    def _refusal(self, refused: Refused) -> requests.Response:
        return self._envelope(
            refused.status, {'status': refused.status, 'code': refused.code, 'message': refused.message}
        )

    def get(self, url: str, *, auth: tuple[str, str], timeout: int) -> requests.Response:
        """`b2_authorize_account`, the one call that is not a POST."""
        assert url == AUTHORIZE_URL, f'unexpected GET {url}'
        self.calls.append('b2_authorize_account')
        key_id, secret = auth
        key = self.keys.get(key_id)
        if key is None or key.secret != secret:
            return self._refusal(Refused(401, 'unauthorized', 'invalid credentials'))
        token = f'token-{key_id}-{next(self.counter)}'
        self.tokens[token] = key_id
        storage: dict[str, Any] = {'apiUrl': API_URL, 'capabilities': list(key.capabilities)}
        if key.bucket_id is not None:
            # A confined key is told what it is confined to, which is the
            # only way one without `listBuckets` learns its bucket's id.
            storage['bucketId'] = key.bucket_id
            storage['bucketName'] = self.buckets[key.bucket_id]['bucketName']
            storage['namePrefix'] = key.name_prefix
        return self._envelope(
            200,
            {'accountId': ACCOUNT_ID, 'authorizationToken': token, 'apiInfo': {'storageApi': storage}},
        )

    def _authorized(self, headers: dict[str, str], api: str) -> Key:
        key_id = self.tokens.get(headers['Authorization'])
        # The token outlives nothing: a key that has been deleted cannot go on
        # acting through a token it issued before it was deleted.
        if key_id is None or key_id not in self.keys:
            raise Refused(401, 'bad_auth_token', 'the auth token is not valid')
        key = self.keys[key_id]
        if REQUIRED[api] not in key.capabilities:
            raise Refused(401, 'unauthorized', f'{api} requires the {REQUIRED[api]} capability')
        return key

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: int) -> requests.Response:
        api = urlparse(url).path.rsplit('/', 1)[-1]
        assert url == f'{API_URL}/b2api/v3/{api}', f'unexpected POST {url}'
        self.calls.append(api)
        self.posted.append((api, json))
        try:
            caller = self._authorized(headers, api)
            return self._envelope(200, self._act(api, caller, json))
        except Refused as refused:
            return self._refusal(refused)

    def _act(self, api: str, caller: Key, body: dict[str, Any]) -> dict[str, Any]:
        match api:
            case 'b2_create_key':
                return self._create_key(caller, body)
            case 'b2_list_keys':
                return self._list_keys(body)
            case 'b2_delete_key':
                return self._delete_key(body)
            case 'b2_list_buckets':
                return self._list_buckets(body)
            case 'b2_create_bucket':
                return self._create_bucket(body)
            case 'b2_update_bucket':
                return self._update_bucket(body)
            case 'b2_list_file_names':
                return self._list_file_names(caller, body)
            case _:  # pragma: no cover - a call the minter is not meant to make
                raise AssertionError(f'unexpected call {api}')

    def _create_key(self, caller: Key, body: dict[str, Any]) -> dict[str, Any]:
        # Held to the 28-row capability table rather than to the list of legal
        # values on `b2_create_key`'s own page, which omits the two bucket
        # replication capabilities the table and the account-wide roles carry.
        # That the live call accepts those two is unproven live.
        unknown = sorted({str(capability) for capability in body['capabilities']} - set(MASTER_CAPABILITIES))
        if unknown:
            raise Refused(400, 'bad_request', f'unknown capabilities: {", ".join(unknown)}')
        key = self.add_key(
            str(body['keyName']),
            tuple(str(capability) for capability in body['capabilities']),
            bucket_id=body.get('bucketId'),
            name_prefix=body.get('namePrefix'),
        )
        return {
            'applicationKeyId': key.key_id,
            'applicationKey': key.secret,
            'keyName': key.name,
            'capabilities': list(key.capabilities),
        }

    def _list_keys(self, body: dict[str, Any]) -> dict[str, Any]:
        ordered = [self.keys[key_id] for key_id in sorted(self.keys)]
        start = body.get('startApplicationKeyId')
        if start is not None:
            ordered = [key for key in ordered if key.key_id >= str(start)]
        wanted = min(int(body['maxKeyCount']), self.page_limit)
        page, rest = ordered[:wanted], ordered[wanted:]
        return {
            'keys': [key.listed() for key in page],
            'nextApplicationKeyId': rest[0].key_id if rest else None,
        }

    def _delete_key(self, body: dict[str, Any]) -> dict[str, Any]:
        key_id = str(body['applicationKeyId'])
        if key_id not in self.keys:
            raise Refused(400, 'bad_request', f'no application key {key_id}')
        key = self.keys.pop(key_id)
        return {'applicationKeyId': key.key_id, 'keyName': key.name}

    def _list_buckets(self, body: dict[str, Any]) -> dict[str, Any]:
        name = body.get('bucketName')
        found = [bucket for bucket in self.buckets.values() if name in (None, bucket['bucketName'])]
        return {'buckets': found}

    def _create_bucket(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body['bucketName'])
        if any(bucket['bucketName'] == name for bucket in self.buckets.values()):
            raise Refused(400, 'duplicate_bucket_name', f'bucket {name} already exists')
        bucket_id = f'bucket-{next(self.counter)}'
        bucket: dict[str, Any] = {
            'bucketId': bucket_id,
            'bucketName': name,
            'bucketType': body['bucketType'],
            'lifecycleRules': body.get('lifecycleRules') or [],
        }
        self.buckets[bucket_id] = bucket
        return bucket

    def _update_bucket(self, body: dict[str, Any]) -> dict[str, Any]:
        bucket = self.buckets[str(body['bucketId'])]
        if 'lifecycleRules' in body:
            bucket['lifecycleRules'] = body['lifecycleRules']
        return bucket

    def _list_file_names(self, caller: Key, body: dict[str, Any]) -> dict[str, Any]:
        bucket_id = str(body['bucketId'])
        prefix = str(body.get('prefix', ''))
        if caller.bucket_id is not None and caller.bucket_id != bucket_id:
            raise Refused(401, 'unauthorized', 'the key is confined to another bucket')
        if caller.name_prefix is not None and not prefix.startswith(caller.name_prefix):
            raise Refused(401, 'unauthorized', 'the key is confined to another prefix')
        self.listings.append((caller.key_id, bucket_id, prefix))
        names = sorted(name for name in self.objects.get(bucket_id, []) if name.startswith(prefix))
        start = body.get('startFileName')
        if start is not None:
            names = [name for name in names if name >= str(start)]
        wanted = min(int(body['maxFileCount']), self.file_page_limit)
        page, rest = names[:wanted], names[wanted:]
        return {
            'files': [{'fileName': name, 'action': 'upload'} for name in page],
            'nextFileName': rest[0] if rest else None,
        }
