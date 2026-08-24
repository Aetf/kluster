"""The B2 credential family (docs/credentials.md §3).

Mints the **management key** the `physical` stack runs on: bucket, key and
lifecycle administration with **no file capabilities at all** — the credential
that manages the backup buckets cannot read a byte out of them.

The master application key stays an offline-tier credential: it is read out of
the KeePassXC database for the duration of one call and never lands in an
environment variable, a shell history, or on disk.

Rotation is a re-run: `mint` creates a fresh key and writes it to its slots,
`prune` retires every older key of the same name once the new one is live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from .kdbx import KdbxStore

log = logging.getLogger(__name__)

AUTHORIZE_URL = 'https://api.backblazeb2.com/b2api/v3/b2_authorize_account'

DEFAULT_KEY_NAME = 'kluster-management'

#: Bucket and key administration only. Managing a bucket never requires
#: touching its contents, so listFiles/readFiles/writeFiles/deleteFiles are
#: deliberately absent.
MANAGEMENT_CAPABILITIES: tuple[str, ...] = (
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
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return cls(
            account_id=data['accountId'],
            api_url=data['apiInfo']['storageApi']['apiUrl'],
            token=data['authorizationToken'],
        )

    def post(self, api: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = requests.post(
            f'{self.api_url}/b2api/v3/{api}',
            json=body,
            headers={'Authorization': self.token},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def create_key(self, name: str, capabilities: tuple[str, ...]) -> tuple[str, str]:
        data = self.post(
            'b2_create_key',
            {'accountId': self.account_id, 'keyName': name, 'capabilities': list(capabilities)},
        )
        return data['applicationKeyId'], data['applicationKey']

    def keys(self) -> list[dict[str, Any]]:
        data = self.post('b2_list_keys', {'accountId': self.account_id, 'maxKeyCount': 1000})
        return data['keys']

    def delete_key(self, key_id: str) -> None:
        _ = self.post('b2_delete_key', {'applicationKeyId': key_id})


def _master_session(store: KdbxStore, master_entry: str) -> Session:
    key_id = store.get(master_entry, attribute='UserName')
    key = store.get(master_entry)
    if not key_id or not key:
        raise ValueError(f'{master_entry!r} must hold the master key id as its username and the key as its password')
    return Session.authorize(key_id, key)


def mint(store: KdbxStore, *, master_entry: str, entry: str, name: str = DEFAULT_KEY_NAME) -> str:
    """Create a management key, verify it, and store it. Returns its key id."""
    session = _master_session(store, master_entry)
    key_id, key = session.create_key(name, MANAGEMENT_CAPABILITIES)

    # Verify by using it, not by trusting the response.
    minted = Session.authorize(key_id, key)
    _ = minted.post('b2_list_buckets', {'accountId': minted.account_id})
    log.info('verified: %s authorizes and can list buckets', key_id)

    store.put(entry, key_id, key)
    log.info('minted %s as %s', name, key_id)
    log.info("remaining slots: pulumi config (physical), CI environment; then 'credentials b2 prune %s'", key_id)
    return key_id


def prune(store: KdbxStore, *, master_entry: str, keep: str, name: str = DEFAULT_KEY_NAME) -> None:
    """Delete every key named `name` except `keep` — the tail of a rotation."""
    session = _master_session(store, master_entry)
    for key in session.keys():
        if key['keyName'] == name and key['applicationKeyId'] != keep:
            log.info('deleting superseded key %s', key['applicationKeyId'])
            session.delete_key(key['applicationKeyId'])
