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

**Every answer B2 sends crosses into a typed value at one parser** (`payload`):
`post` hands back the decoded JSON as an `object`, and the `_…` functions below
turn each response shape into a record. A field B2 does not send, or sends as
something else, is refused there by name instead of surfacing as a `KeyError`
from inside a mint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from . import masters, payload
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
class AppKey:
    """One B2 application key: what addresses it, and what authenticates as it.

    Two indistinguishable strings, so they travel together rather than as a
    pair a caller is free to hand over the other way round. B2 discloses the
    secret half once, at creation, which is why nothing here can recover from
    getting them the wrong way.
    """

    key_id: str
    key: str


@dataclass(frozen=True)
class ListedKey:
    """One row of `b2_list_keys`: what names a key, and what it may do.

    Scope is the two nullable halves — an account-wide key is confined to
    neither a bucket nor a prefix — and together with the capabilities they are
    what `dump_key_is_current` measures an appliance's key against.
    """

    key_id: str
    name: str
    capabilities: tuple[str, ...]
    bucket_id: str | None
    name_prefix: str | None


@dataclass(frozen=True)
class KeyPage:
    """One page of `b2_list_keys`, and where the page after it starts.

    B2 chooses the page size, so `next_key_id` — absent on the last page — is
    the only thing that says a listing is complete.
    """

    keys: tuple[ListedKey, ...]
    next_key_id: str | None


@dataclass(frozen=True)
class LifecycleRule:
    """One B2 lifecycle rule: which files it governs, and how long each phase lasts.

    A record rather than the JSON document B2 exchanges, so "the bucket already
    says what this program wants it to say" is a comparison of two rules. What
    is compared is what a rule *is* — B2's schema is these three fields — so a
    rule that agrees on all three is not rewritten.
    """

    file_name_prefix: str
    #: `None` is B2's "never": files this rule never hides, or never deletes.
    days_from_uploading_to_hiding: int | None
    days_from_hiding_to_deleting: int | None

    def body(self) -> dict[str, Any]:
        """The rule as `b2_create_bucket` and `b2_update_bucket` take it."""
        return {
            'fileNamePrefix': self.file_name_prefix,
            'daysFromUploadingToHiding': self.days_from_uploading_to_hiding,
            'daysFromHidingToDeleting': self.days_from_hiding_to_deleting,
        }


@dataclass(frozen=True)
class Bucket:
    """A bucket as `b2_list_buckets` and `b2_create_bucket` describe it.

    The id and the rules on it: what `ensure_bucket` returns, and what it
    compares. The name is not part of it — a listing is asked for one name and
    a creation is told one, so the answer's copy adds nothing.
    """

    bucket_id: str
    lifecycle_rules: tuple[LifecycleRule, ...]


def _created_key(answer: object) -> AppKey:
    """`b2_create_key`: the id, and the secret B2 discloses exactly once."""
    body = payload.Payload.of(answer, 'b2_create_key')
    return AppKey(key_id=body.text('applicationKeyId'), key=body.text('applicationKey'))


def _listed_key(entry: payload.Payload) -> ListedKey:
    """One `b2_list_keys` row."""
    return ListedKey(
        key_id=entry.text('applicationKeyId'),
        name=entry.text('keyName'),
        capabilities=entry.texts('capabilities'),
        bucket_id=entry.optional_text('bucketId'),
        name_prefix=entry.optional_text('namePrefix'),
    )


def _key_page(answer: object) -> KeyPage:
    """`b2_list_keys`: one page of rows, and the cursor for the next."""
    body = payload.Payload.of(answer, 'b2_list_keys')
    return KeyPage(
        keys=tuple(_listed_key(entry) for entry in body.objects('keys')),
        next_key_id=body.optional_text('nextApplicationKeyId'),
    )


def _lifecycle_rule(entry: payload.Payload) -> LifecycleRule:
    """One rule of a bucket's `lifecycleRules`."""
    return LifecycleRule(
        file_name_prefix=entry.string('fileNamePrefix'),
        days_from_uploading_to_hiding=entry.optional_whole('daysFromUploadingToHiding'),
        days_from_hiding_to_deleting=entry.optional_whole('daysFromHidingToDeleting'),
    )


def _bucket(body: payload.Payload) -> Bucket:
    """A bucket object, wherever it is being described."""
    return Bucket(
        bucket_id=body.text('bucketId'),
        lifecycle_rules=tuple(_lifecycle_rule(rule) for rule in body.objects('lifecycleRules')),
    )


def _listed_buckets(answer: object) -> tuple[Bucket, ...]:
    """`b2_list_buckets`: the buckets matching what was asked for."""
    body = payload.Payload.of(answer, 'b2_list_buckets')
    return tuple(_bucket(entry) for entry in body.objects('buckets'))


def _created_bucket(answer: object) -> Bucket:
    """`b2_create_bucket`: the bucket that now exists."""
    return _bucket(payload.Payload.of(answer, 'b2_create_bucket'))


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
        return cls._authorized(resp.json())

    @classmethod
    def _authorized(cls, answer: object) -> Session:
        """`b2_authorize_account`, whose three fields are exactly a session.

        The API host is the account's own, handed out here: every later call
        goes to it rather than to the authorization endpoint.
        """
        body = payload.Payload.of(answer, 'b2_authorize_account')
        return cls(
            account_id=body.text('accountId'),
            api_url=body.nested('apiInfo').nested('storageApi').text('apiUrl'),
            token=body.text('authorizationToken'),
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

    def post(self, api: str, body: dict[str, Any]) -> object:
        """One API call, answered with decoded JSON for a parser to read.

        Deliberately untyped on the way out: what a response holds is B2's
        business until one of the `_…` parsers above has said so.
        """
        resp = requests.post(
            f'{self.api_url}/b2api/v3/{api}',
            json=body,
            headers={'Authorization': self.token},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def create_key(self, name: str) -> AppKey:
        return _created_key(
            self.post(
                'b2_create_key',
                {'accountId': self.account_id, 'keyName': name, 'capabilities': list(CAPABILITIES)},
            )
        )

    def keys(self) -> tuple[ListedKey, ...]:
        """Every application key on the account, following the pages.

        The page size is the server's choice, not the caller's: a listing that
        stopped at the first answer would report keys that exist as gone --
        which retires nothing and rebuilds an appliance that was fine.
        """
        found: list[ListedKey] = []
        start: str | None = None
        while True:
            body: dict[str, Any] = {'accountId': self.account_id, 'maxKeyCount': PAGE_SIZE}
            if start is not None:
                body['startApplicationKeyId'] = start
            page = _key_page(self.post('b2_list_keys', body))
            found.extend(page.keys)
            start = page.next_key_id
            if not start:
                return tuple(found)

    def buckets(self, name: str | None = None) -> tuple[Bucket, ...]:
        """The account's buckets, or the one called `name`."""
        body: dict[str, Any] = {'accountId': self.account_id}
        if name is not None:
            body['bucketName'] = name
        return _listed_buckets(self.post('b2_list_buckets', body))

    def delete_key(self, key_id: str) -> None:
        _ = self.post('b2_delete_key', {'applicationKeyId': key_id})


@dataclass(frozen=True)
class MintedKey:
    """A freshly created key, and a session authorized as it.

    The session is part of the answer because retirement has to run as a
    credential that outlives the deletions (`retire_others`).
    """

    session: Session
    app_key: AppKey


def _mint_verified(session: Session, name: str) -> MintedKey:
    """Create a key and prove it works before anything is told to rely on it."""
    app_key = session.create_key(name)
    minted = Session.authorize(app_key.key_id, app_key.key)
    _ = minted.buckets()
    log.info('minted %s (%s), verified against the API', name, app_key.key_id)
    return MintedKey(session=minted, app_key=app_key)


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
        if existing.name == name and existing.key_id != keep:
            log.info('deleting superseded %s %s', name, existing.key_id)
            session.delete_key(existing.key_id)


def create_seed(*, root: masters.Credential, seeds: KdbxStore, seed_entry: str) -> str:
    """Create the seed key from the account master key. Returns its key id.

    The account master key is not part of the seed kit (credentials.md §2): it
    comes from the desktop secret store or a prompt (`masters.py`), is held
    for the length of this call, and the seed it mints is what the kit gets.

    Needed once at bring-up, and again only if the seed is lost — routine
    rotation is `rotate_seed`, which never touches the account root.
    """
    session = Session.authorize(root[masters.B2_ACCOUNT_ID], root[masters.B2_KEY])
    minted = _mint_verified(session, SEED_KEY_NAME)
    seeds.put(seed_entry, minted.app_key.key_id, minted.app_key.key)
    # Stored first, retired second: an interrupted run leaves a key the kit does
    # not name, and this is the only thing that can clear it -- a seed key whose
    # secret nobody holds is a live permission, not a spare.
    retire_others(minted.session, SEED_KEY_NAME, keep=minted.app_key.key_id)
    return minted.app_key.key_id


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

    minted = _mint_verified(session, SEED_KEY_NAME)
    (into or store).put(seed_entry, minted.app_key.key_id, minted.app_key.key)

    # As the successor, not as the predecessor: the predecessor is one of the
    # keys being deleted, and its session stops working the moment it is.
    retire_others(minted.session, SEED_KEY_NAME, keep=minted.app_key.key_id)
    log.info('seed rotated: %s -> %s', previous, minted.app_key.key_id)
    return minted.app_key.key_id


def mint_management(store: KdbxStore, *, seed_entry: str) -> AppKey:
    """Mint the management key for the bring-up pipeline to place in its slots.

    Deliberately returns the credential instead of storing it: the offline
    store holds seeds, never the credentials automation consumes
    (credentials.md §1 rule 2).
    """
    session = Session.from_entry(store, seed_entry)
    minted = _mint_verified(session, MANAGEMENT_KEY_NAME)
    # Retire predecessors only once the replacement works: a failed mint must
    # leave the running stack's credential alone. The seed signs it, and the
    # seed is not among the keys being deleted.
    retire_others(session, MANAGEMENT_KEY_NAME, keep=minted.app_key.key_id)
    return minted.app_key


#: The uploader's whole permission: it cannot list, read, or delete, so a
#: compromised appliance cannot walk the dump history (storage.md §4).
DUMP_CAPABILITIES: tuple[str, ...] = ('writeFiles',)


def _retention(prefix: str, retention_days: int) -> LifecycleRule:
    """What the dump prefix's lifecycle rule has to say.

    Retention is a lifecycle rule rather than a pruning job precisely so the
    uploader needs no delete capability; hiding then deleting is what gives a
    retired encryption key a definite end of life.
    """
    return LifecycleRule(
        file_name_prefix=f'{prefix}/',
        days_from_uploading_to_hiding=retention_days,
        days_from_hiding_to_deleting=1,
    )


def ensure_bucket(session: Session, name: str, *, prefix: str, retention_days: int) -> str:
    """Create the bucket if absent and pin its retention. Returns the bucket id.

    Convergent in the rule as well as in the bucket: a retention someone
    changed is put back, and one that already says this is left alone.
    """
    wanted = _retention(prefix, retention_days)
    existing = session.buckets(name)
    if existing:
        bucket = existing[0]
        if bucket.lifecycle_rules != (wanted,):
            _ = session.post(
                'b2_update_bucket',
                {
                    'accountId': session.account_id,
                    'bucketId': bucket.bucket_id,
                    'lifecycleRules': [wanted.body()],
                },
            )
            log.info('bucket %s: retention set to %d days', name, retention_days)
        return bucket.bucket_id

    created = _created_bucket(
        session.post(
            'b2_create_bucket',
            {
                'accountId': session.account_id,
                'bucketName': name,
                'bucketType': 'allPrivate',
                'lifecycleRules': [wanted.body()],
            },
        )
    )
    log.info('created bucket %s', name)
    return created.bucket_id


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
        if existing.key_id != key_id:
            continue
        return (
            existing.name == name
            and existing.bucket_id == bucket_id
            and existing.name_prefix == f'{prefix}/'
            and sorted(existing.capabilities) == sorted(DUMP_CAPABILITIES)
        )
    return False


def mint_dump_key(session: Session, *, bucket_id: str, prefix: str, name: str) -> AppKey:
    """A write-only key confined to one prefix of one bucket."""
    minted = _created_key(
        session.post(
            'b2_create_key',
            {
                'accountId': session.account_id,
                'keyName': name,
                'capabilities': list(DUMP_CAPABILITIES),
                'bucketId': bucket_id,
                'namePrefix': f'{prefix}/',
            },
        )
    )
    # Retired by the minter rather than by the new key: a write-only key
    # carries no `deleteKeys` and could not retire anything, its predecessor
    # least of all.
    retire_others(session, name, keep=minted.key_id)
    log.info('minted %s (%s)', name, minted.key_id)
    return minted
