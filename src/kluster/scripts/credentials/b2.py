"""The B2 credential family (docs/credentials.md §2–§3).

Three credentials, three lifetimes:

-   the **account master key** — an account root (`masters.py`), held outside
    the kit and used only to create the seed if it is ever lost;
-   the **seed key** — offline, and the only B2 credential that ever gets
    stored: it mints the management key and, because `b2_create_key` needs
    nothing but `writeKeys`, its own successor, which is what makes B2
    rotation a script rather than a console visit;
-   the **management key** — what the `physical` stack actually runs on,
    minted at bring-up straight into its slots and never written to the
    offline store.

Seed and management carry the same capabilities: what separates them is
lifetime and reach, not permission. Neither carries file capabilities at
all — the credential that manages the backup buckets cannot read a byte out
of them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from . import masters
from .kdbx import KdbxStore
from .masters import CredentialRejected

log = logging.getLogger(__name__)


AUTHORIZE_URL = 'https://api.backblazeb2.com/b2api/v3/b2_authorize_account'

SEED_KEY_NAME = 'kluster-seed'
MANAGEMENT_KEY_NAME = 'kluster-management'

#: How many keys to ask for per `b2_list_keys` page. B2 caps a single Class C
#: transaction at a thousand keys and pages beyond that, so this is a
#: transaction-size choice rather than a limit on what a listing sees.
PAGE_SIZE = 1000

#: Bucket and key administration. Managing a bucket never requires touching
#: its contents, so listFiles/readFiles/writeFiles/deleteFiles are absent;
#: writeKeys/deleteKeys are what let the seed replace itself and mint the
#: prefix-scoped writer keys.
CAPABILITIES: tuple[str, ...] = (
    'listBuckets',
    'readBuckets',
    'writeBuckets',
    'deleteBuckets',
    'readBucketEncryption',
    'writeBucketEncryption',
    'readBucketRetentions',
    'writeBucketRetentions',
    'readBucketReplications',
    'writeBucketReplications',
    'readBucketNotifications',
    'writeBucketNotifications',
    'listKeys',
    'writeKeys',
    'deleteKeys',
)


@dataclass(frozen=True)
class Session:
    """An authorized B2 API session."""

    account_id: str
    api_url: str
    token: str

    @classmethod
    def authorize(cls, key_id: str, key: str) -> Session:
        resp = requests.get(AUTHORIZE_URL, auth=(key_id, key), timeout=30)
        if resp.status_code == 401:
            # The id is not the secret; naming it is what makes a wrong
            # username field (an account e-mail, say) diagnosable at a glance.
            raise CredentialRejected(
                f"B2 rejected key id {key_id!r} — the entry's username must be the key id "
                '(for the master key, that is the account id) and its password the key itself'
            )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return cls(
            account_id=data['accountId'],
            api_url=data['apiInfo']['storageApi']['apiUrl'],
            token=data['authorizationToken'],
        )

    @classmethod
    def from_entry(cls, store: KdbxStore, entry: str) -> Session:
        """Authorize with a credential held in the offline store.

        The seed leaves the database for the duration of one call and never
        reaches an environment variable or a shell history.
        """
        key_id = store.get(entry, attribute='UserName')
        key = store.get(entry)
        if not key_id or not key:
            raise ValueError(f'{entry!r} must hold the key id as its username and the key as its password')
        return cls.authorize(key_id, key)

    def post(self, api: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = requests.post(
            f'{self.api_url}/b2api/v3/{api}',
            json=body,
            headers={'Authorization': self.token},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def create_key(self, name: str) -> tuple[str, str]:
        data = self.post(
            'b2_create_key',
            {'accountId': self.account_id, 'keyName': name, 'capabilities': list(CAPABILITIES)},
        )
        return data['applicationKeyId'], data['applicationKey']

    def keys(self) -> list[dict[str, Any]]:
        """Every application key on the account, following the pages.

        The page size is the server's choice, not the caller's: a listing that
        stopped at the first answer would report keys that exist as gone --
        which retires nothing and rebuilds an appliance that was fine.
        """
        found: list[dict[str, Any]] = []
        start: str | None = None
        while True:
            body: dict[str, Any] = {'accountId': self.account_id, 'maxKeyCount': PAGE_SIZE}
            if start is not None:
                body['startApplicationKeyId'] = start
            data = self.post('b2_list_keys', body)
            found.extend(list[dict[str, Any]](data['keys']))
            start = data.get('nextApplicationKeyId')
            if not start:
                return found

    def delete_key(self, key_id: str) -> None:
        _ = self.post('b2_delete_key', {'applicationKeyId': key_id})


def _mint_verified(session: Session, name: str) -> tuple[Session, str, str]:
    """Create a key, prove it works, and hand back a session as that key.

    The session is part of the answer because retirement has to run as a
    credential that outlives it (`retire_others`).
    """
    key_id, key = session.create_key(name)
    minted = Session.authorize(key_id, key)
    _ = minted.post('b2_list_buckets', {'accountId': minted.account_id})
    log.info('minted %s (%s), verified against the API', name, key_id)
    return minted, key_id, key


def retire_others(session: Session, name: str, *, keep: str) -> None:
    """Delete every other key of this name, as a credential that survives it.

    B2 key names are not unique, so what is retired is "everything called
    this except the one in hand" rather than one known predecessor: a run that
    died after minting left a key nobody holds the secret of, and the next run
    is the only thing that can see it.

    `session` must be a credential that is still valid once the deletions are
    done -- an authorization token is a token *of a key*, so a session that
    deletes its own key cannot delete the next one.
    """
    for existing in session.keys():
        if existing['keyName'] == name and existing['applicationKeyId'] != keep:
            log.info('deleting superseded %s %s', name, existing['applicationKeyId'])
            session.delete_key(str(existing['applicationKeyId']))


def create_seed(*, root: masters.Credential, seeds: KdbxStore, seed_entry: str) -> str:
    """Create the seed key from the account master key. Returns its key id.

    The account master key is not part of the seed kit (credentials.md §2): it
    comes from the desktop secret store or a prompt (`masters.py`), is held
    for the length of this call, and the seed it mints is what the kit gets.

    Needed once at bring-up, and again only if the seed is lost — routine
    rotation is `rotate_seed`, which never touches the account root.
    """
    session = Session.authorize(root['account-id'], root['key'])
    minted, key_id, key = _mint_verified(session, SEED_KEY_NAME)
    seeds.put(seed_entry, key_id, key)
    # Stored first, retired second: an interrupted run leaves a key the kit does
    # not name, and this is the only thing that can clear it -- a seed key whose
    # secret nobody holds is a live permission, not a spare.
    retire_others(minted, SEED_KEY_NAME, keep=key_id)
    return key_id


def rotate_seed(store: KdbxStore, *, seed_entry: str, into: KdbxStore | None = None) -> str:
    """Have the seed mint its successor, store it, and delete the old keys.

    The old key is deleted only after the new one is stored and verified, so
    an interrupted rotation leaves a working seed either way.

    `into` is where the successor is written, defaulting to the database the
    predecessor came from. A whole-kit rotation writes a *new* file (§4.2) and
    the retired one must stay exactly as it was, so it passes the successor
    explicitly rather than letting this edit the kit it is reading.
    """
    session = Session.from_entry(store, seed_entry)
    previous = store.get(seed_entry, attribute='UserName')

    minted, key_id, key = _mint_verified(session, SEED_KEY_NAME)
    (into or store).put(seed_entry, key_id, key)

    # As the successor, not as the predecessor: the predecessor is one of the
    # keys being deleted, and its session stops working the moment it is.
    retire_others(minted, SEED_KEY_NAME, keep=key_id)
    log.info('seed rotated: %s -> %s', previous, key_id)
    return key_id


def mint_management(store: KdbxStore, *, seed_entry: str) -> tuple[str, str]:
    """Mint the management key for the bring-up pipeline to place in its slots.

    Deliberately returns the credential instead of storing it: the offline
    store holds seeds, never the credentials automation consumes
    (credentials.md §1 rule 2).
    """
    session = Session.from_entry(store, seed_entry)
    _, key_id, key = _mint_verified(session, MANAGEMENT_KEY_NAME)
    # Retire predecessors only once the replacement works: a failed mint must
    # leave the running stack's credential alone. The seed signs it, and the
    # seed is not among the keys being deleted.
    retire_others(session, MANAGEMENT_KEY_NAME, keep=key_id)
    return key_id, key


#: The uploader's whole permission: it cannot list, read, or delete, so a
#: compromised appliance cannot walk the dump history (storage.md §4).
DUMP_CAPABILITIES: tuple[str, ...] = ('writeFiles',)


def ensure_bucket(session: Session, name: str, *, prefix: str, retention_days: int) -> str:
    """Create the bucket if absent and pin its retention. Returns the bucket id.

    Retention is a lifecycle rule rather than a pruning job precisely so the
    uploader needs no delete capability; hiding then deleting is what gives a
    retired encryption key a definite end of life.
    """
    rules = [
        {
            'fileNamePrefix': f'{prefix}/',
            'daysFromUploadingToHiding': retention_days,
            'daysFromHidingToDeleting': 1,
        }
    ]
    existing = session.post('b2_list_buckets', {'accountId': session.account_id, 'bucketName': name})['buckets']
    if existing:
        bucket_id = str(existing[0]['bucketId'])
        if existing[0].get('lifecycleRules') != rules:
            _ = session.post(
                'b2_update_bucket',
                {'accountId': session.account_id, 'bucketId': bucket_id, 'lifecycleRules': rules},
            )
            log.info('bucket %s: retention set to %d days', name, retention_days)
        return bucket_id

    created = session.post(
        'b2_create_bucket',
        {
            'accountId': session.account_id,
            'bucketName': name,
            'bucketType': 'allPrivate',
            'lifecycleRules': rules,
        },
    )
    log.info('created bucket %s', name)
    return str(created['bucketId'])


def dump_key_is_current(session: Session, key_id: str, *, bucket_id: str, prefix: str, name: str) -> bool:
    """Whether `key_id` is still the write-only key this bucket and prefix want.

    The appliance's copy of the secret cannot be read back, so "is the box
    holding the right credential" is answered by identity: a key that is gone
    (deleted in the console, superseded by another mint) or one whose scope no
    longer matches the settings is not the intended key, and the only way to
    put the intended one on the box is to build a new box.
    """
    if not key_id:
        return False
    for existing in session.keys():
        if existing['applicationKeyId'] != key_id:
            continue
        return (
            existing['keyName'] == name
            and existing.get('bucketId') == bucket_id
            and existing.get('namePrefix') == f'{prefix}/'
            and sorted(existing.get('capabilities') or []) == sorted(DUMP_CAPABILITIES)
        )
    return False


def mint_dump_key(session: Session, *, bucket_id: str, prefix: str, name: str) -> tuple[str, str]:
    """A write-only key confined to one prefix of one bucket."""
    data = session.post(
        'b2_create_key',
        {
            'accountId': session.account_id,
            'keyName': name,
            'capabilities': list(DUMP_CAPABILITIES),
            'bucketId': bucket_id,
            'namePrefix': f'{prefix}/',
        },
    )
    key_id, key = str(data['applicationKeyId']), str(data['applicationKey'])
    # Retired by the minter rather than by the new key: a write-only key
    # carries no `deleteKeys` and could not retire anything, its predecessor
    # least of all.
    retire_others(session, name, keep=key_id)
    log.info('minted %s (%s)', name, key_id)
    return key_id, key
