"""The Cloudflare seed token: what it inherits, what it stores, how it rotates.

Driven against a fake of the API rather than the API, the way `b2`'s HTTP
surface is meant to be: what is being checked is that the minter asks for the
right things and stores what comes back, which is fixed, rather than what
Cloudflare does with the request.
"""

from __future__ import annotations

import itertools
import json as jsonlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import requests
from conftest import MemoryKit

from kluster.scripts.credentials import cloudflare, entries, masters
from kluster.scripts.credentials.kdbx import KdbxStore
from kluster.scripts.credentials.masters import CredentialRejected

PASSWORD = 'kit-password'
SEED_ENTRY = entries.SEEDS['cloudflare'].entry

#: What a root token carries: the one permission that can mint a token.
MINTING_POLICY: dict[str, Any] = {
    'id': 'policy-1',
    'effect': 'allow',
    'resources': {'com.cloudflare.api.user.deadbeef': '*'},
    'permission_groups': [{'id': 'group-1', 'name': cloudflare.MINTING_PERMISSION}],
}


@dataclass
class FakeApi:
    """One account's tokens, answering the four calls the minter makes."""

    tokens: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    #: token value -> token id, since a value is what a caller authorizes with.
    values: dict[str, str] = field(default_factory=dict[str, str])
    counter: itertools.count[int] = field(default_factory=lambda: itertools.count(2))
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    #: The bodies of every create call, so a test can check what was asked
    #: for and not only what came back.
    posted: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    #: The API names a permission group in every response it returns, even
    #: though a request may identify it by id alone.
    group_names: dict[str, str] = field(
        default_factory=lambda: {'group-1': cloudflare.MINTING_PERMISSION, 'g': 'Zone Read'}
    )

    def add(self, name: str, policies: list[dict[str, Any]]) -> str:
        token_id = f'token-{next(self.counter)}'
        value = f'value-of-{token_id}'
        named = [
            {
                **policy,
                'permission_groups': [
                    {'id': group['id'], 'name': self.group_names[str(group['id'])]}
                    for group in policy['permission_groups']
                ],
            }
            for policy in policies
        ]
        self.tokens[token_id] = {'id': token_id, 'name': name, 'status': 'active', 'policies': named}
        self.values[value] = token_id
        return value

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

    def get(self, url: str, headers: dict[str, str], timeout: int) -> requests.Response:
        return self.request('GET', url, headers=headers, timeout=timeout, json=None)

    def request(
        self, method: str, url: str, *, headers: dict[str, str], timeout: int, json: dict[str, Any] | None
    ) -> requests.Response:
        path = url.removeprefix(cloudflare.API)
        token = self._bearer(headers)
        self.calls.append((method, path))
        match (method, path):
            case ('GET', '/user/tokens/verify'):
                return self._envelope({'id': token['id'], 'status': token['status']})
            case ('GET', '/user/tokens'):
                return self._envelope(list(self.tokens.values()))
            case ('POST', '/user/tokens'):
                assert json is not None
                self.posted.append(json)
                value = self.add(str(json['name']), list(json['policies']))
                return self._envelope({'id': self.values[value], 'value': value})
            case ('DELETE', token_path) if token_path.startswith('/user/tokens/'):
                del self.tokens[token_path.removeprefix('/user/tokens/')]
                return self._envelope(None)
            case ('GET', token_path) if token_path.startswith('/user/tokens/'):
                return self._envelope(self.tokens[token_path.removeprefix('/user/tokens/')])
            case _:  # pragma: no cover - a call the minter is not meant to make
                raise AssertionError(f'unexpected call {method} {path}')


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(cloudflare.requests, 'get', fake.get)
    monkeypatch.setattr(cloudflare.requests, 'request', fake.request)
    return fake


@pytest.fixture
def kit(tmp_path: Path) -> KdbxStore:
    return KdbxStore.create(tmp_path / 'kit.kdbx', PASSWORD)


def _permissions(policies: list[dict[str, Any]]) -> list[tuple[str, Any, list[str]]]:
    """A token's permissions, without the ids the API assigns to a policy."""
    return [
        (str(policy['effect']), policy['resources'], sorted(str(g['name']) for g in policy['permission_groups']))
        for policy in policies
    ]


def _root(value: str) -> masters.Credential:
    return masters.Credential(root=masters.ROOTS['cloudflare'], values={'token': value})


def test_the_seed_is_a_same_permission_successor_of_the_root(api: FakeApi, kit: KdbxStore) -> None:
    root_value = api.add('root', [MINTING_POLICY])

    token_id = cloudflare.create_seed(root=_root(root_value), seeds=kit, seed_entry=SEED_ENTRY)

    # Policies are copied, minus the ids the API assigns: the seed must be
    # able to mint, which is the same permission the root was made with (§2).
    assert api.tokens[token_id]['name'] == cloudflare.SEED_TOKEN_NAME
    assert _permissions(api.tokens[token_id]['policies']) == _permissions(
        api.tokens[api.values[root_value]]['policies']
    )
    # A policy is sent back by group id; the ids the API assigns to the policy
    # itself, and the names it decorates the groups with, are not echoed at it.
    assert api.posted[-1]['policies'] == [
        {
            'effect': 'allow',
            'resources': MINTING_POLICY['resources'],
            'permission_groups': [{'id': 'group-1'}],
        }
    ]


def test_the_row_holds_the_token_id_and_the_token(api: FakeApi, kit: KdbxStore) -> None:
    root_value = api.add('root', [MINTING_POLICY])

    token_id = cloudflare.create_seed(root=_root(root_value), seeds=kit, seed_entry=SEED_ENTRY)

    # UserName is the public half everywhere in the kit; the secret is the
    # value Cloudflare shows once.
    assert kit.get(SEED_ENTRY, attribute='UserName') == token_id
    assert kit.get(SEED_ENTRY) == f'value-of-{token_id}'


def test_the_minted_token_is_verified_before_it_is_stored(api: FakeApi, kit: KdbxStore) -> None:
    root_value = api.add('root', [MINTING_POLICY])

    _ = cloudflare.create_seed(root=_root(root_value), seeds=kit, seed_entry=SEED_ENTRY)

    # The call after the mint is the new token verifying itself: a token that
    # cannot authenticate must not reach the kit. What follows is retirement,
    # which runs as that same verified token.
    minted = api.calls.index(('POST', '/user/tokens'))
    assert api.calls[minted + 1] == ('GET', '/user/tokens/verify')


def test_a_root_without_the_minting_permission_says_which_one_it_needs(api: FakeApi, kit: KdbxStore) -> None:
    read_only = api.add('root', [{**MINTING_POLICY, 'permission_groups': [{'id': 'g', 'name': 'Zone Read'}]}])

    with pytest.raises(CredentialRejected, match=cloudflare.MINTING_PERMISSION):
        _ = cloudflare.create_seed(root=_root(read_only), seeds=kit, seed_entry=SEED_ENTRY)


def test_an_unknown_token_is_reported_as_rejected(kit: KdbxStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(url: str, headers: dict[str, str], timeout: int) -> requests.Response:
        response = requests.Response()
        response.status_code = 401
        return response

    monkeypatch.setattr(cloudflare.requests, 'get', refuse)

    # The distinguishing case: the call arrived and the API said no, which is
    # nearly always the wrong field rather than the wrong network.
    with pytest.raises(CredentialRejected, match='rejected the token'):
        _ = cloudflare.create_seed(root=_root('nonsense'), seeds=kit, seed_entry=SEED_ENTRY)


def test_rotation_replaces_the_token_and_retires_the_predecessor(api: FakeApi, kit: KdbxStore, tmp_path: Path) -> None:
    root_value = api.add('root', [MINTING_POLICY])
    previous = cloudflare.create_seed(root=_root(root_value), seeds=kit, seed_entry=SEED_ENTRY)
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)

    current = cloudflare.rotate_seed(kit, seed_entry=SEED_ENTRY, into=successor)

    assert current != previous
    assert previous not in api.tokens
    assert api.tokens[current]['name'] == cloudflare.SEED_TOKEN_NAME
    # §4.2: the retired kit keeps the predecessor exactly as it was.
    assert kit.get(SEED_ENTRY, attribute='UserName') == previous
    assert successor.get(SEED_ENTRY, attribute='UserName') == current


def test_retirement_runs_as_the_new_token_not_the_predecessor(api: FakeApi, memory_kit: KdbxStore) -> None:
    root_value = api.add('root', [MINTING_POLICY])
    previous = cloudflare.create_seed(root=_root(root_value), seeds=memory_kit, seed_entry=SEED_ENTRY)
    orphan = api.values[api.add(cloudflare.SEED_TOKEN_NAME, [MINTING_POLICY])]

    current = cloudflare.rotate_seed(memory_kit, seed_entry=SEED_ENTRY)

    # Two tokens to delete, and the predecessor is one of them: a session
    # signing with the predecessor stops working after the first deletion, so
    # the retirement has to run as the token that was just minted.
    assert _seed_tokens(api) == [current]
    assert previous not in api.tokens
    assert orphan not in api.tokens


def test_creation_retires_a_token_a_lost_run_left_behind(api: FakeApi, memory_kit: KdbxStore) -> None:
    root_value = api.add('root', [MINTING_POLICY])
    _ = api.add(cloudflare.SEED_TOKEN_NAME, [MINTING_POLICY])

    current = cloudflare.create_seed(root=_root(root_value), seeds=memory_kit, seed_entry=SEED_ENTRY)

    # An orphan carries the seed's name and the seed's permissions and nobody
    # holds its value; create-after-loss is the run that has to remove it.
    assert _seed_tokens(api) == [current]


class Interrupted(RuntimeError):
    """A run that stopped mid-flight, the way a lost process or a 500 does."""


@dataclass
class Faulty:
    """The fake API, counting calls and stopping the run at the k-th.

    Two ways to stop, because they leave different worlds behind: `before`
    never reaches the API, `after` lets the API act and loses the answer --
    which is the one that strands a token nobody holds the value of.
    """

    api: FakeApi
    fail_at: int | None = None
    when: str = 'after'
    counted: int = 0

    def _guard(self, target: Any, *args: Any, **kwargs: Any) -> requests.Response:
        self.counted += 1
        fatal = self.counted == self.fail_at
        if fatal and self.when == 'before':
            raise Interrupted(f'call {self.fail_at} never reached the API')
        result = target(*args, **kwargs)
        if fatal:
            raise Interrupted(f'call {self.fail_at} reached the API; the answer was lost')
        return result

    def get(self, url: str, headers: dict[str, str], timeout: int) -> requests.Response:
        return self._guard(self.api.get, url, headers=headers, timeout=timeout)

    def request(
        self, method: str, url: str, *, headers: dict[str, str], timeout: int, json: dict[str, Any] | None
    ) -> requests.Response:
        return self._guard(self.api.request, method, url, headers=headers, timeout=timeout, json=json)

    def attach(self) -> None:
        setattr(cloudflare.requests, 'get', self.get)  # noqa: B010
        setattr(cloudflare.requests, 'request', self.request)  # noqa: B010


def _seed_tokens(api: FakeApi) -> list[str]:
    return [str(token['id']) for token in api.tokens.values() if token['name'] == cloudflare.SEED_TOKEN_NAME]


def _kit_never_lies(kit: KdbxStore, api: FakeApi) -> None:
    """Whatever the kit holds must be a token the API would still accept."""
    if not kit.has(SEED_ENTRY):
        return
    token_id = kit.get(SEED_ENTRY, attribute='UserName')
    assert api.values.get(kit.get(SEED_ENTRY)) == token_id, 'the stored value is not this token'
    assert token_id in api.tokens, 'the kit holds a token the account no longer has'


def _calls_made(*, rotating: bool) -> int:
    """How many API calls one uninterrupted run makes, by measurement.

    The sweep then covers exactly the calls the operation makes today, and
    widens by itself when the operation grows one.
    """
    api = FakeApi()
    faulty = Faulty(api)
    original = (cloudflare.requests.get, cloudflare.requests.request)
    faulty.attach()
    try:
        kit = MemoryKit()
        _ = cloudflare.create_seed(root=_root(api.add('root', [MINTING_POLICY])), seeds=kit, seed_entry=SEED_ENTRY)
        if not rotating:
            return faulty.counted
        before = faulty.counted
        _ = cloudflare.rotate_seed(kit, seed_entry=SEED_ENTRY)
        return faulty.counted - before
    finally:
        setattr(cloudflare.requests, 'get', original[0])  # noqa: B010
        setattr(cloudflare.requests, 'request', original[1])  # noqa: B010


CREATE_CALLS = _calls_made(rotating=False)
ROTATE_CALLS = _calls_made(rotating=True)

#: Both ways a run can stop at call k (see `Faulty`).
CRASH_POINTS = 'before', 'after'


@pytest.fixture
def faulty(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> Callable[[int | None, str], Faulty]:
    """Re-points the module's HTTP calls at a counter that can stop the run."""

    def install(fail_at: int | None, when: str) -> Faulty:
        interrupter = Faulty(api, fail_at=fail_at, when=when)
        monkeypatch.setattr(cloudflare.requests, 'get', interrupter.get)
        monkeypatch.setattr(cloudflare.requests, 'request', interrupter.request)
        return interrupter

    return install


@pytest.mark.parametrize('when', CRASH_POINTS)
@pytest.mark.parametrize('failing_call', range(1, CREATE_CALLS + 1))
def test_creating_the_seed_heals_from_a_failure_at_any_call(
    failing_call: int, when: str, api: FakeApi, memory_kit: KdbxStore, faulty: Callable[[int | None, str], Faulty]
) -> None:
    root = _root(api.add('root', [MINTING_POLICY]))
    _ = faulty(failing_call, when)

    with pytest.raises(Interrupted):
        _ = cloudflare.create_seed(root=root, seeds=memory_kit, seed_entry=SEED_ENTRY)
    _kit_never_lies(memory_kit, api)

    _ = faulty(None, when)
    token_id = cloudflare.create_seed(root=root, seeds=memory_kit, seed_entry=SEED_ENTRY)

    _kit_never_lies(memory_kit, api)
    # One seed token stands, and it is the one the kit holds: a token minted
    # by the run that died is an orphan with the seed's own name and the
    # seed's own permissions.
    assert _seed_tokens(api) == [token_id]


@pytest.mark.parametrize('when', CRASH_POINTS)
@pytest.mark.parametrize('failing_call', range(1, ROTATE_CALLS + 1))
def test_rotating_the_seed_heals_from_a_failure_at_any_call(
    failing_call: int, when: str, api: FakeApi, memory_kit: KdbxStore, faulty: Callable[[int | None, str], Faulty]
) -> None:
    root = _root(api.add('root', [MINTING_POLICY]))
    _ = cloudflare.create_seed(root=root, seeds=memory_kit, seed_entry=SEED_ENTRY)
    _ = faulty(failing_call, when)

    with pytest.raises(Interrupted):
        _ = cloudflare.rotate_seed(memory_kit, seed_entry=SEED_ENTRY)
    # Whichever token the kit holds after the crash, predecessor or successor,
    # it still authenticates.
    _kit_never_lies(memory_kit, api)

    _ = faulty(None, when)
    token_id = cloudflare.rotate_seed(memory_kit, seed_entry=SEED_ENTRY)

    _kit_never_lies(memory_kit, api)
    assert _seed_tokens(api) == [token_id]
