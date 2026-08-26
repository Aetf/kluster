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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

from .kdbx import KdbxStore
from .masters import CredentialRejected

log = logging.getLogger(__name__)

API = 'https://api.cloudflare.com/client/v4'

#: How many zones one listing page carries. The estate is far below it, so the
#: pagination below exists for correctness rather than for the estate's size.
PAGE_SIZE = 50

#: The permission the seed must carry for any of this to work. Checked by name
#: when the seed is pasted in, because a token that merely reads is refused by
#: the create call with a message that does not say which permission is
#: missing -- and a console credential is exactly where the wrong template
#: gets picked.
MINTING_PERMISSION = 'API Tokens Write'

#: The seed's second permission, written the way the dashboard's own tree spells
#: it -- the string an operator has to find on the page, rather than the API's
#: `Zone Read`. The minting template does not include it, and without it the
#: seed cannot turn a zone name into the id a policy names.
ZONE_VISIBILITY_PERMISSION = 'Zone → Zone → Read'

#: What the zones token (§3) carries, by permission-group name. Editing records
#: is the job; reading the zone is what the Cloudflare provider does before
#: every change, and a token that cannot see its zone cannot manage records in
#: it. Names rather than the well-known group ids, because the ids are account
#: data to be looked up (`Session.permission_groups`) and a hard-coded one that
#: the platform renumbered would mint a token with the wrong scope.
ZONE_PERMISSIONS = ('DNS Write', 'Zone Read')

#: The prefix under which a policy names a single zone. A resource map keyed by
#: it grants on exactly the zones listed and on nothing else.
ZONE_RESOURCE = 'com.cloudflare.api.account.zone'


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

    def require_zone_visibility(self) -> None:
        """Refuse a seed that authenticates and can mint but sees no zone.

        Behaviour rather than permission names, because the listing is what the
        seed is used for: a token may carry zone read and still be scoped to
        zones this account does not have. Non-empty is the whole test -- which
        zones the estate expects is `conventions`' business, and adoption comes
        before any of it.
        """
        if not self.zones():
            raise CredentialRejected(
                'this Cloudflare token can mint tokens but can see no zone, so it cannot '
                'turn a zone name into the id a minted policy names: it is missing '
                f'{ZONE_VISIBILITY_PERMISSION}, with Zone Resources at all zones. Adding '
                'that permission to this token in the dashboard does not extend the value '
                'already in hand -- create a fresh token carrying both permissions, record '
                'it with `credentials seed cloudflare create`, and delete this one'
            )

    def tokens(self) -> list[dict[str, Any]]:
        return list(self._call('GET', '/user/tokens'))

    def zones(self, name: str | None = None) -> list[dict[str, Any]]:
        """The zones this token may see, or the ones matching `name`.

        Which zones come back is a property of the token, so this is both how
        the seed resolves a zone name to the id a policy needs and how a minted
        token proves what it was given: the same call, asked of two credentials.
        """
        found: list[dict[str, Any]] = []
        page = 1
        while True:
            query = {'per_page': str(PAGE_SIZE), 'page': str(page)} | ({'name': name} if name else {})
            batch = list[dict[str, Any]](self._call('GET', f'/zones?{urlencode(query)}'))
            found.extend(batch)
            if len(batch) < PAGE_SIZE:
                return found
            page += 1

    def permission_groups(self, names: Sequence[str]) -> list[dict[str, str]]:
        """The ids of the named permission groups, as a policy's `permission_groups`.

        A policy identifies a permission by id, and an id is account data: it is
        read here rather than written down, so a token is minted with the scope
        the name means rather than with whatever the id used to mean.
        """
        catalogue = list[dict[str, Any]](self._call('GET', '/user/tokens/permission_groups'))
        by_name = {
            str(group['name']): str(group['id'])
            for group in catalogue
            if ZONE_RESOURCE in [str(scope) for scope in list[Any](group.get('scopes') or [])]
        }
        missing = [name for name in names if name not in by_name]
        if missing:
            raise CredentialRejected(
                f'Cloudflare lists no zone permission group named {", ".join(missing)} — '
                "the names this repository asks for are no longer the platform's"
            )
        return [{'id': by_name[name]} for name in names]

    def create_token(self, name: str, policies: list[dict[str, Any]]) -> tuple[str, str]:
        result = self._call('POST', '/user/tokens', {'name': name, 'policies': policies})
        return str(result['id']), str(result['value'])

    def delete_token(self, token_id: str) -> None:
        _ = self._call('DELETE', f'/user/tokens/{token_id}')


def _mint_verified(session: Session, name: str, policies: list[dict[str, Any]]) -> tuple[str, str]:
    """Create a token with the given policies and prove it authenticates.

    Verification is a call the new token makes as itself: a value that cannot
    authenticate must not reach a consumer's slot, and nothing later in the
    procedure would notice.
    """
    log.info('minting %s', name)
    token_id, token = session.create_token(name, policies)
    minted = Session.authorize(token)
    log.info('minted %s (%s), verified against the API', name, minted.token_id)
    return token_id, token


def _retire_superseded(session: Session, name: str, keep: str) -> None:
    """Delete same-named tokens other than the one just minted.

    `session` is the *minter's*, not the new token's. Listing and deleting
    tokens are token-management permissions, and the platform allows a minted
    §3 token none of them, so a session signing with the new token is refused
    this outright. The minter cannot saw off the credential it signs with
    either: it is the seed, whose name is not a stack token's, and its own id
    is skipped regardless.

    Only after the replacement exists, authenticates and carries the scope it
    was asked for: every earlier stop must leave a working predecessor behind.
    """
    for existing in session.tokens():
        token_id = str(existing.get('id'))
        if str(existing.get('name')) == name and token_id not in (keep, session.token_id):
            log.info('deleting superseded token %s', token_id)
            session.delete_token(token_id)


def mint_token(
    session: Session,
    name: str,
    policies: list[dict[str, Any]],
    confirm: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """Mint one §3 token from the seed, as (token id, token value).

    The unit every per-stack Cloudflare credential is made of, and the reason
    the seed exists. Rotating such a token is a re-run of this: a same-named
    predecessor -- or an orphan a lost run left behind, which carries a live
    permission nobody holds the value of -- is deleted once the replacement is
    minted, verified and accepted by `confirm`, which is handed the new value
    and raises if the credential is not the one that was asked for. Retirement
    comes last so that no check can fail after the predecessor is already
    gone.

    `policies` are the caller's, and may not include token permissions: a
    sub-token carrying them is refused by the platform.
    """
    token_id, token = _mint_verified(session, name, policies)
    if confirm is not None:
        confirm(token)
    _retire_superseded(session, name, keep=token_id)
    return token_id, token


@dataclass(frozen=True)
class ZoneToken:
    """A minted zones token and the two facts its consumer needs beside it.

    The account id travels with the token because it is discovered on the way:
    a stack that manages records in these zones is configured with the account
    that owns them, and asking Cloudflare a second time could only produce a
    second answer.
    """

    token_id: str
    value: str
    account_id: str
    zone_ids: dict[str, str]


def _resolve_zones(session: Session, names: Sequence[str]) -> tuple[str, dict[str, str]]:
    """Zone names to ids, plus the account that owns them all.

    Resolved through the minting session because a policy names zones by id.
    A name the session cannot see is refused here rather than turned into a
    token with a hole in its scope.
    """
    ids: dict[str, str] = {}
    accounts: set[str] = set()
    for name in names:
        matches = [zone for zone in session.zones(name=name) if str(zone.get('name')) == name]
        if not matches:
            raise CredentialRejected(
                f'the Cloudflare seed cannot see the zone {name!r}: it is not in this account, '
                'or the seed token carries no zone read permission'
            )
        ids[name] = str(matches[0]['id'])
        accounts.add(str(dict[str, Any](matches[0].get('account') or {}).get('id', '')))
    if len(accounts) != 1:
        raise CredentialRejected(
            f'the zones are spread over {len(accounts)} Cloudflare accounts; one token cannot be scoped to them'
        )
    return accounts.pop(), ids


def _confirm_scope(value: str, expected: Sequence[str]) -> None:
    """Prove the minted token sees the zones it was minted for, as itself.

    The mint call reports what it created; this reports what the credential can
    do, which is the thing the consumer depends on. Extra zones are a warning
    rather than a refusal — a token wider than intended still works, and the
    place to narrow it is the policy above, not a failed run that leaves the
    stack with no credential at all.
    """
    log.info('checking the minted token against the %d estate zones', len(expected))
    visible = {str(zone.get('name')) for zone in Session.authorize(value).zones()}
    missing = [name for name in expected if name not in visible]
    if missing:
        raise CredentialRejected(f'the minted token cannot see {", ".join(missing)}; it was not scoped as asked')
    extra = sorted(visible - set(expected))
    if extra:
        log.warning('the minted token also sees %s, which nothing here asked for', ', '.join(extra))


def mint_zone_token(session: Session, *, name: str, zones: Sequence[str]) -> ZoneToken:
    """The §3 zones token: record edit on exactly `zones`, and nothing else.

    One policy with one resource per zone, carrying `ZONE_PERMISSIONS` and no
    token permission — the class the platform allows a sub-token to have. The
    result is proven against the API as the minted token before anything is
    retired, so a credential that cannot do the job never reaches a slot and
    never costs the predecessor that still could.
    """
    log.info('resolving %d estate zones through the Cloudflare seed', len(zones))
    account_id, zone_ids = _resolve_zones(session, zones)
    policies = [
        {
            'effect': 'allow',
            'resources': {f'{ZONE_RESOURCE}.{zone_id}': '*' for zone_id in zone_ids.values()},
            'permission_groups': session.permission_groups(ZONE_PERMISSIONS),
        }
    ]
    token_id, value = mint_token(session, name, policies, confirm=lambda minted: _confirm_scope(minted, zones))
    return ZoneToken(token_id=token_id, value=value, account_id=account_id, zone_ids=zone_ids)


def adopt_seed(*, token: str, seeds: KdbxStore, seed_entry: str) -> str:
    """Store a console-made seed token in the kit. Returns its id.

    Bring-up and rotation are the same act here, because the platform allows
    no other: the operator makes the token in the dashboard and this verifies
    it and writes the row. The id is recovered from the token rather than
    asked for -- `/user/tokens/verify` is the one call every token may make,
    and a value typed twice is a value that can disagree with itself.

    Both of the seed's permissions are proven here, while the operator is still
    on the dashboard page that fixes either one. Minting alone would let a seed
    that cannot list zones into the kit, and its first symptom would be a
    failed mint one command and one console visit later.

    The superseded token is not deleted from here. Deleting it would mean
    signing with a credential the retired kit still names as current, at the
    moment §4.2 requires that kit to stay exactly as it was; the dashboard
    page the operator is already on is where it goes.
    """
    session = Session.authorize(token)
    session.require_minting()
    session.require_zone_visibility()
    seeds.put(seed_entry, session.token_id, token)
    log.info('stored the Cloudflare seed token (%s)', session.token_id)
    return session.token_id
