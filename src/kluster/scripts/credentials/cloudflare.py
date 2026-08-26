"""The Cloudflare credential family (docs/credentials.md §2–§3).

Two credentials, two lifetimes:

-   the **account root token** — an account root (`masters.py`), carrying
    *User → API Tokens → Edit* and nothing else. It is created in the
    dashboard once and used only to create the seed;
-   the **seed token** — offline, in the kit, and the only Cloudflare
    credential that is ever stored. It mints the zone-scoped provider token,
    the DNS-01 token and the gateway's ACME token, and — because minting a
    token is itself an API-token permission — its own successor.

**A minted token inherits the policies of the token that minted it.** The
alternative is to name permission groups by id and address the user as a
resource, which needs the account's user id, which needs a permission the root
deliberately does not carry. Copying is also the more faithful reading of §2:
the seed is a *same-permission successor*, and the tokens it goes on to mint
are limited by the user's own permissions rather than by the minting token's.

What the tokens in §3 look like is not here: they are minted by the stack that
consumes them, from this seed.
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

API = 'https://api.cloudflare.com/client/v4'

SEED_TOKEN_NAME = 'kluster-seed'

#: The permission the root must carry for any of this to work. Checked by name
#: before the first mint, because a token that merely reads is refused by the
#: create call with a message that does not say which permission is missing.
MINTING_PERMISSION = 'API Tokens Write'


def _result(resp: requests.Response) -> Any:
    """The `result` of a Cloudflare envelope, or the error it carries instead.

    Every response is `{success, errors, result}`, and a refused credential
    arrives as a 400 with a populated `errors` rather than as an exception, so
    unwrapping and diagnosing are the same step.
    """
    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise
    if not body.get('success'):
        errors: list[dict[str, Any]] = list(body.get('errors') or [])
        messages = '; '.join(str(error.get('message', error)) for error in errors)
        raise CredentialRejected(f'Cloudflare refused the call: {messages or resp.status_code}')
    return body['result']


@dataclass(frozen=True)
class Session:
    """An authorized Cloudflare API session, and the token it authorized with."""

    token: str
    token_id: str

    @classmethod
    def authorize(cls, token: str) -> Session:
        """Verify a token and learn its own id.

        The id is not stored anywhere on the way in: `/user/tokens/verify` is
        the one call every token may make, so identity is recovered rather
        than carried — a stored copy could only disagree with the token.
        """
        resp = requests.get(
            f'{API}/user/tokens/verify',
            headers={'Authorization': f'Bearer {token}'},
            timeout=30,
        )
        if resp.status_code in (400, 401, 403):
            raise CredentialRejected(
                'Cloudflare rejected the token — the value is the one shown once at creation, '
                'not the token id and not the Global API Key'
            )
        result = _result(resp)
        status = str(result.get('status', ''))
        if status != 'active':
            raise CredentialRejected(f'the Cloudflare token is {status or "not active"}')
        return cls(token=token, token_id=str(result['id']))

    @classmethod
    def from_entry(cls, store: KdbxStore, entry: str) -> Session:
        """Authorize with the seed held in the offline store."""
        return cls.authorize(store.get(entry))

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        resp = requests.request(
            method,
            f'{API}{path}',
            json=body,
            headers={'Authorization': f'Bearer {self.token}'},
            timeout=30,
        )
        return _result(resp)

    def policies(self) -> list[dict[str, Any]]:
        """This token's own policies, in the shape `POST /user/tokens` wants.

        Reading a token requires the same permission as writing one, so a root
        that cannot do this is a root that could not have minted anything
        either -- which is why the check for it lives here.
        """
        result = self._call('GET', f'/user/tokens/{self.token_id}')
        policies: list[dict[str, Any]] = list(result.get('policies') or [])
        groups: list[dict[str, Any]] = []
        for policy in policies:
            groups.extend(list[dict[str, Any]](policy.get('permission_groups') or []))
        granted = {str(group.get('name', '')) for group in groups}
        if MINTING_PERMISSION not in granted:
            raise CredentialRejected(
                f'this Cloudflare token does not carry {MINTING_PERMISSION!r} '
                f'(it carries: {", ".join(sorted(granted)) or "nothing"}); '
                'only that permission can mint a token through the API'
            )
        return [
            {
                'effect': policy['effect'],
                'resources': policy['resources'],
                'permission_groups': [{'id': group['id']} for group in policy['permission_groups']],
            }
            for policy in policies
        ]

    def tokens(self) -> list[dict[str, Any]]:
        return list(self._call('GET', '/user/tokens'))

    def create_token(self, name: str, policies: list[dict[str, Any]]) -> tuple[str, str]:
        result = self._call('POST', '/user/tokens', {'name': name, 'policies': policies})
        return str(result['id']), str(result['value'])

    def delete_token(self, token_id: str) -> None:
        _ = self._call('DELETE', f'/user/tokens/{token_id}')


def _mint_verified(session: Session, name: str) -> tuple[str, str, Session]:
    """Create a token with the minter's own policies and prove it works.

    The verified session is handed back rather than dropped: the retirement
    below has to run as the *new* token, and this is the session that proves
    it works.
    """
    log.info("minting %s with the minting token's own policies", name)
    token_id, token = session.create_token(name, session.policies())
    minted = Session.authorize(token)
    log.info('minted %s (%s), verified against the API', name, minted.token_id)
    return token_id, token, minted


def _retire_superseded(session: Session, name: str, keep: str) -> None:
    """Delete same-named tokens other than the one just stored.

    Only after the replacement is stored and verified: an interrupted run must
    leave a working seed either way. `session` must be the kept token's own:
    a session signing with a token it is about to delete stops working
    partway through the deletions, and a run that left an orphan behind is
    exactly the run with more than one deletion to make.
    """
    for existing in session.tokens():
        if str(existing.get('name')) == name and str(existing.get('id')) != keep:
            log.info('deleting superseded token %s', existing['id'])
            session.delete_token(str(existing['id']))


def create_seed(*, root: masters.Credential, seeds: KdbxStore, seed_entry: str) -> str:
    """Create the seed token from the account root token. Returns its id.

    Needed once at bring-up, and again only if the seed is lost — routine
    rotation is `rotate_seed`, which never touches the account root.
    """
    session = Session.authorize(root['token'])
    token_id, token, minted = _mint_verified(session, SEED_TOKEN_NAME)
    seeds.put(seed_entry, token_id, token)

    # A run that died between minting and storing left a token whose value
    # exists nowhere -- with the seed's name and the seed's permissions, so a
    # live credential nobody holds. Only the stored one survives.
    _retire_superseded(minted, SEED_TOKEN_NAME, keep=token_id)
    return token_id


def rotate_seed(store: KdbxStore, *, seed_entry: str, into: KdbxStore | None = None) -> str:
    """Have the seed mint its successor, store it, and delete the predecessor.

    `into` is where the successor is written, defaulting to the database the
    predecessor came from. A whole-kit rotation writes a *new* file (§4.2) and
    the retired one must stay exactly as it was, so it passes the successor
    explicitly rather than letting this edit the kit it is reading.
    """
    session = Session.from_entry(store, seed_entry)
    previous = session.token_id

    token_id, token, minted = _mint_verified(session, SEED_TOKEN_NAME)
    (into or store).put(seed_entry, token_id, token)

    _retire_superseded(minted, SEED_TOKEN_NAME, keep=token_id)
    log.info('seed rotated: %s -> %s', previous, token_id)
    return token_id
