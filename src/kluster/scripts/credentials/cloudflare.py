"""The Cloudflare credential family (docs/credentials.md §2–§3).

One stored credential, the **seed token**: offline, in the kit, carrying
*User → API Tokens → Edit*. It mints the zone-scoped provider token, the
DNS-01 token and the gateway's ACME token.

**The seed is console-made, and there is no Cloudflare account root.** A
token minted through the API may not carry token-management permissions —
"sub-token is not allowed to have permissions to manage other tokens", stated
with the API itself at
developers.cloudflare.com/fundamentals/api/how-to/create-via-api/ — so
nothing can mint a credential of the seed's own class. That forbids both the
root's only job (minting the seed) and the seed minting its own successor:
each is the dashboard's *Create Additional Tokens* template, pasted into the
kit, and rotation is deleting the superseded token on the same page.

**A minted token's policies are its caller's, never a copy of the minter's.**
Copying is what a same-permission successor would need, and it is exactly
what the platform refuses: the minter carries *API Tokens Write* by
definition, so a copy of its policies is a sub-token with token permissions.
What §3's tokens carry is stated by the stack that consumes them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from .kdbx import KdbxStore
from .masters import CredentialRejected

log = logging.getLogger(__name__)

API = 'https://api.cloudflare.com/client/v4'

#: The permission the seed must carry for any of this to work. Checked by name
#: when the seed is pasted in, because a token that merely reads is refused by
#: the create call with a message that does not say which permission is
#: missing -- and a console credential is exactly where the wrong template
#: gets picked.
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

    def granted(self) -> set[str]:
        """The permission groups this token carries, by name.

        Reading a token requires the same permission as writing one, so a
        token that cannot do this is a token that could not have minted
        anything either.
        """
        result = self._call('GET', f'/user/tokens/{self.token_id}')
        return {
            str(group.get('name', ''))
            for policy in list[dict[str, Any]](result.get('policies') or [])
            for group in list[dict[str, Any]](policy.get('permission_groups') or [])
        }

    def require_minting(self) -> None:
        """Refuse a token that cannot mint, naming the permission it lacks."""
        granted = self.granted()
        if MINTING_PERMISSION not in granted:
            raise CredentialRejected(
                f'this Cloudflare token does not carry {MINTING_PERMISSION!r} '
                f'(it carries: {", ".join(sorted(granted)) or "nothing"}); '
                'only that permission can mint a token through the API'
            )

    def tokens(self) -> list[dict[str, Any]]:
        return list(self._call('GET', '/user/tokens'))

    def create_token(self, name: str, policies: list[dict[str, Any]]) -> tuple[str, str]:
        result = self._call('POST', '/user/tokens', {'name': name, 'policies': policies})
        return str(result['id']), str(result['value'])

    def delete_token(self, token_id: str) -> None:
        _ = self._call('DELETE', f'/user/tokens/{token_id}')


def _mint_verified(session: Session, name: str, policies: list[dict[str, Any]]) -> tuple[str, str, Session]:
    """Create a token with the given policies and prove it works.

    The verified session is handed back rather than dropped: the retirement
    below has to run as the *new* token, and this is the session that proves
    it works.
    """
    log.info('minting %s', name)
    token_id, token = session.create_token(name, policies)
    minted = Session.authorize(token)
    log.info('minted %s (%s), verified against the API', name, minted.token_id)
    return token_id, token, minted


def _retire_superseded(session: Session, name: str, keep: str) -> None:
    """Delete same-named tokens other than the one just minted.

    Only after the replacement exists and is verified: an interrupted run
    must leave a working token either way. `session` must be the kept
    token's own: a session signing with a token it is about to delete stops
    working partway through the deletions, and a run that left an orphan
    behind is exactly the run with more than one deletion to make.
    """
    for existing in session.tokens():
        if str(existing.get('name')) == name and str(existing.get('id')) != keep:
            log.info('deleting superseded token %s', existing['id'])
            session.delete_token(str(existing['id']))


def mint_token(session: Session, name: str, policies: list[dict[str, Any]]) -> tuple[str, str]:
    """Mint one §3 token from the seed, as (token id, token value).

    The unit every per-stack Cloudflare credential is made of, and the reason
    the seed exists. Rotating such a token is a re-run of this: a same-named
    predecessor -- or an orphan a lost run left behind, which carries a live
    permission nobody holds the value of -- is deleted once the replacement
    is minted and verified.

    `policies` are the caller's, and may not include token permissions: a
    sub-token carrying them is refused by the platform.
    """
    token_id, token, minted = _mint_verified(session, name, policies)
    _retire_superseded(minted, name, keep=token_id)
    return token_id, token


def adopt_seed(*, token: str, seeds: KdbxStore, seed_entry: str) -> str:
    """Store a console-made seed token in the kit. Returns its id.

    Bring-up and rotation are the same act here, because the platform allows
    no other: the operator makes the token in the dashboard and this verifies
    it and writes the row. The id is recovered from the token rather than
    asked for -- `/user/tokens/verify` is the one call every token may make,
    and a value typed twice is a value that can disagree with itself.

    The superseded token is not deleted from here. Deleting it would mean
    signing with a credential the retired kit still names as current, at the
    moment §4.2 requires that kit to stay exactly as it was; the dashboard
    page the operator is already on is where it goes.
    """
    session = Session.authorize(token)
    session.require_minting()
    seeds.put(seed_entry, session.token_id, token)
    log.info('stored the Cloudflare seed token (%s)', session.token_id)
    return session.token_id
