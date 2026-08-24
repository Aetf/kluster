"""The B2 credential family (docs/credentials.md §2–§3).

Three credentials, three lifetimes:

-   the **account master key** — an account root, held offline, used only to
    create the seed if it is ever lost;
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

from .kdbx import KdbxStore

log = logging.getLogger(__name__)


class CredentialRejected(RuntimeError):
    pass


AUTHORIZE_URL = 'https://api.backblazeb2.com/b2api/v3/b2_authorize_account'

SEED_KEY_NAME = 'kluster-seed'
MANAGEMENT_KEY_NAME = 'kluster-management'

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
        data = self.post('b2_list_keys', {'accountId': self.account_id, 'maxKeyCount': 1000})
        return data['keys']

    def delete_key(self, key_id: str) -> None:
        _ = self.post('b2_delete_key', {'applicationKeyId': key_id})


def _mint_verified(session: Session, name: str) -> tuple[str, str]:
    """Create a key and prove it works before anything depends on it."""
    key_id, key = session.create_key(name)
    minted = Session.authorize(key_id, key)
    _ = minted.post('b2_list_buckets', {'accountId': minted.account_id})
    log.info('minted %s (%s), verified against the API', name, key_id)
    return key_id, key


def create_seed(store: KdbxStore, *, master_entry: str, seed_entry: str) -> str:
    """Create the seed key from the account master key. Returns its key id.

    Needed once at bring-up, and again only if the seed is lost — routine
    rotation is `rotate_seed`, which never touches the account root.
    """
    session = Session.from_entry(store, master_entry)
    key_id, key = _mint_verified(session, SEED_KEY_NAME)
    store.put(seed_entry, key_id, key)
    return key_id


def rotate_seed(store: KdbxStore, *, seed_entry: str) -> str:
    """Have the seed mint its successor, store it, and delete the old keys.

    The old key is deleted only after the new one is stored and verified, so
    an interrupted rotation leaves a working seed either way.
    """
    session = Session.from_entry(store, seed_entry)
    previous = store.get(seed_entry, attribute='UserName')

    key_id, key = _mint_verified(session, SEED_KEY_NAME)
    store.put(seed_entry, key_id, key)

    for existing in session.keys():
        if existing['keyName'] == SEED_KEY_NAME and existing['applicationKeyId'] != key_id:
            log.info('deleting superseded seed %s', existing['applicationKeyId'])
            session.delete_key(existing['applicationKeyId'])
    log.info('seed rotated: %s -> %s', previous, key_id)
    return key_id


def mint_management(store: KdbxStore, *, seed_entry: str) -> tuple[str, str]:
    """Mint the management key for the bring-up pipeline to place in its slots.

    Deliberately returns the credential instead of storing it: the offline
    store holds seeds, never the credentials automation consumes
    (credentials.md §1 rule 2).
    """
    session = Session.from_entry(store, seed_entry)
    key_id, key = _mint_verified(session, MANAGEMENT_KEY_NAME)
    # Retire predecessors only once the replacement works: a failed mint must
    # leave the running stack's credential alone.
    for existing in session.keys():
        if existing['keyName'] == MANAGEMENT_KEY_NAME and existing['applicationKeyId'] != key_id:
            log.info('deleting superseded management key %s', existing['applicationKeyId'])
            session.delete_key(existing['applicationKeyId'])
    return key_id, key
