"""A fake of the Cloudflare API, shared by the suites that mint against it.

Its own named module rather than a `conftest`, for the reason `memory_kit` is
one: test modules import it, and every directory with tests may have a
`conftest` of its own on `sys.path`.

The fake refuses what the platform refuses — a sub-token may not carry token
permissions — and it answers zone listings *as the calling token*, which is
what makes a scope check meaningful: a minted token sees the zones its policies
name, and nothing else.
"""

from __future__ import annotations

import itertools
import json as jsonlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from kluster.scripts.credentials import cloudflare

#: The permission groups the fake account offers, by id. Two zone-scoped ones,
#: because that is what a §3 token carries, and the user-scoped minting one the
#: seed carries and no sub-token may.
GROUPS: dict[str, dict[str, Any]] = {
    'group-1': {'name': cloudflare.MINTING_PERMISSION, 'scopes': ['com.cloudflare.api.user']},
    'dns-write': {'name': 'DNS Write', 'scopes': [cloudflare.ZONE_RESOURCE]},
    'zone-read': {'name': 'Zone Read', 'scopes': [cloudflare.ZONE_RESOURCE]},
    'g': {'name': 'Zone Settings Read', 'scopes': [cloudflare.ZONE_RESOURCE]},
}

#: What the seed carries, and the only template that can mint anything.
MINTING_POLICY: dict[str, Any] = {
    'id': 'policy-1',
    'effect': 'allow',
    'resources': {'com.cloudflare.api.user.deadbeef': '*'},
    'permission_groups': [{'id': 'group-1', 'name': cloudflare.MINTING_PERMISSION}],
}

#: What a §3 token carries: zone work and no token permissions, which is the
#: only class the platform will mint.
ZONE_POLICY: dict[str, Any] = {
    'id': 'policy-2',
    'effect': 'allow',
    'resources': {'com.cloudflare.api.account.zone.abc': '*'},
    'permission_groups': [{'id': 'g'}],
}

ACCOUNT_ID = 'account-1'


@dataclass
class FakeApi:
    """One account's tokens and zones, answering the calls the minter makes."""

    tokens: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    #: token value -> token id, since a value is what a caller authorizes with.
    values: dict[str, str] = field(default_factory=dict[str, str])
    #: zone name -> zone record, in the one account this fake has.
    zones: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    counter: itertools.count[int] = field(default_factory=lambda: itertools.count(2))
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    #: The bodies of every create call, so a test can check what was asked
    #: for and not only what came back.
    posted: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    groups: dict[str, dict[str, Any]] = field(default_factory=lambda: dict(GROUPS))
    #: Whether a token carrying the minting permission may also list zones.
    #: The dashboard template that makes the seed carries token permissions and
    #: nothing else, so this is the fact a first live run settles.
    seed_sees_zones: bool = True
    #: A zone id the platform silently leaves out of what it creates — the
    #: shape of a mint that reports success and hands back a narrower token
    #: than was asked for.
    withholds_zone: str | None = None

    def _name_of(self, group_id: str) -> str:
        return str(self.groups[group_id]['name'])

    def add(self, name: str, policies: list[dict[str, Any]]) -> str:
        token_id = f'token-{next(self.counter)}'
        value = f'value-of-{token_id}'
        named = [
            {
                **policy,
                'permission_groups': [
                    {'id': group['id'], 'name': self._name_of(str(group['id']))}
                    for group in policy['permission_groups']
                ],
            }
            for policy in policies
        ]
        self.tokens[token_id] = {'id': token_id, 'name': name, 'status': 'active', 'policies': named}
        self.values[value] = token_id
        return value

    def add_zone(self, name: str, account_id: str = ACCOUNT_ID) -> str:
        zone_id = f'zone-{name.replace(".", "-")}'
        self.zones[name] = {'id': zone_id, 'name': name, 'account': {'id': account_id, 'name': 'estate'}}
        return zone_id

    def _visible_zones(self, token: dict[str, Any]) -> list[dict[str, Any]]:
        """The zones this token may list — the API's own answer, per credential."""
        policies = list[dict[str, Any]](token.get('policies') or [])
        carried = {
            str(group['name']) for policy in policies for group in list[dict[str, Any]](policy['permission_groups'])
        }
        if cloudflare.MINTING_PERMISSION in carried:
            return list(self.zones.values()) if self.seed_sees_zones else []
        scoped = {
            resource.removeprefix(f'{cloudflare.ZONE_RESOURCE}.')
            for policy in policies
            for resource in list[str](policy['resources'])
            if resource.startswith(f'{cloudflare.ZONE_RESOURCE}.')
        }
        return [zone for zone in self.zones.values() if str(zone['id']) in scoped]

    def _bearer(self, headers: dict[str, str]) -> dict[str, Any]:
        value = headers['Authorization'].removeprefix('Bearer ')
        token_id = self.values.get(value)
        if token_id is None:
            raise AssertionError(f'the fake was called with an unknown token {value!r}')
        return self.tokens[token_id]

    def _envelope(self, result: Any) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = jsonlib.dumps({'success': True, 'errors': [], 'result': result}).encode()  # pyright: ignore[reportPrivateUsage]
        return response

    def _refusal(self, message: str) -> requests.Response:
        """A refused call: 400 with a populated `errors`, not an exception."""
        response = requests.Response()
        response.status_code = 400
        response._content = jsonlib.dumps(  # pyright: ignore[reportPrivateUsage]
            {'success': False, 'errors': [{'code': 1000, 'message': message}], 'result': None}
        ).encode()
        return response

    def get(self, url: str, headers: dict[str, str], timeout: int) -> requests.Response:
        return self.request('GET', url, headers=headers, timeout=timeout, json=None)

    def _listed_zones(self, token: dict[str, Any], query: str) -> requests.Response:
        """`GET /zones`, with the name filter and the pagination the caller sends."""
        parameters = parse_qs(query)
        found = self._visible_zones(token)
        if wanted := parameters.get('name'):
            found = [zone for zone in found if str(zone['name']) == wanted[0]]
        per_page = int(parameters.get('per_page', ['20'])[0])
        page = int(parameters.get('page', ['1'])[0])
        return self._envelope(found[(page - 1) * per_page : page * per_page])

    def request(
        self, method: str, url: str, *, headers: dict[str, str], timeout: int, json: dict[str, Any] | None
    ) -> requests.Response:
        parts = urlparse(url.removeprefix(cloudflare.API))
        path = parts.path
        token = self._bearer(headers)
        self.calls.append((method, path))
        match (method, path):
            case ('GET', '/user/tokens/verify'):
                return self._envelope({'id': token['id'], 'status': token['status']})
            case ('GET', '/user/tokens'):
                return self._envelope(list(self.tokens.values()))
            case ('GET', '/user/tokens/permission_groups'):
                return self._envelope([{'id': group_id, **group} for group_id, group in self.groups.items()])
            case ('GET', '/zones'):
                return self._listed_zones(token, parts.query)
            case ('POST', '/user/tokens'):
                assert json is not None
                self.posted.append(json)
                policies = list[dict[str, Any]](json['policies'])
                # The platform's own rule: "sub-token is not allowed to have
                # permissions to manage other tokens". It is why there is no
                # account root and no self-reproducing seed.
                if any(
                    self._name_of(str(group['id'])) == cloudflare.MINTING_PERMISSION
                    for policy in policies
                    for group in list[dict[str, Any]](policy['permission_groups'])
                ):
                    return self._refusal('sub-token is not allowed to have permissions to manage other tokens')
                if self.withholds_zone is not None:
                    withheld = f'{cloudflare.ZONE_RESOURCE}.{self.withholds_zone}'
                    policies = [
                        {**policy, 'resources': {k: v for k, v in policy['resources'].items() if k != withheld}}
                        for policy in policies
                    ]
                value = self.add(str(json['name']), policies)
                return self._envelope({'id': self.values[value], 'value': value})
            case ('DELETE', token_path) if token_path.startswith('/user/tokens/'):
                del self.tokens[token_path.removeprefix('/user/tokens/')]
                return self._envelope(None)
            case ('GET', token_path) if token_path.startswith('/user/tokens/'):
                return self._envelope(self.tokens[token_path.removeprefix('/user/tokens/')])
            case _:  # pragma: no cover - a call the minter is not meant to make
                raise AssertionError(f'unexpected call {method} {path}')


def console_seed(api: FakeApi) -> str:
    """A console-made seed token, as the dashboard hands one over."""
    return api.add('kluster-seed', [MINTING_POLICY])
