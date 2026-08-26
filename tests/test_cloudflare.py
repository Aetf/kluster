"""The Cloudflare seed token: what it is, what it stores, what it may mint.

Driven against a fake of the API rather than the API, the way `b2`'s HTTP
surface is meant to be: what is being checked is that the minter asks for the
right things and stores what comes back, which is fixed, rather than what
Cloudflare does with the request.

The fake refuses what the platform refuses, and one refusal shapes the whole
module: a token minted through the API may not carry token permissions, so
no credential can mint the seed and the seed cannot mint its successor.
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
from memory_kit import MemoryKit

from kluster.scripts.credentials import cloudflare, entries, lifecycle
from kluster.scripts.credentials.kdbx import KdbxStore
from kluster.scripts.credentials.masters import CredentialRejected

PASSWORD = 'kit-password'
SEED_ENTRY = entries.SEEDS['cloudflare'].entry

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

#: The name a per-stack token is minted under, in these tests.
STACK_TOKEN = 'kluster-dns'


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
                policies = list[dict[str, Any]](json['policies'])
                # The platform's own rule: "sub-token is not allowed to have
                # permissions to manage other tokens". It is why there is no
                # account root and no self-reproducing seed.
                if any(
                    self.group_names[str(group['id'])] == cloudflare.MINTING_PERMISSION
                    for policy in policies
                    for group in list[dict[str, Any]](policy['permission_groups'])
                ):
                    return self._refusal('sub-token is not allowed to have permissions to manage other tokens')
                value = self.add(str(json['name']), policies)
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


def _seed(api: FakeApi) -> str:
    """A console-made seed token, as the dashboard hands one over."""
    return api.add('kluster-seed', [MINTING_POLICY])


def test_the_row_holds_the_token_id_and_the_token(api: FakeApi, kit: KdbxStore) -> None:
    value = _seed(api)

    token_id = cloudflare.adopt_seed(token=value, seeds=kit, seed_entry=SEED_ENTRY)

    # UserName is the public half everywhere in the kit; the secret is the
    # value Cloudflare shows once. The id is read off the token rather than
    # asked for, so the two cannot disagree.
    assert kit.get(SEED_ENTRY, attribute='UserName') == token_id
    assert kit.get(SEED_ENTRY) == value


def test_a_seed_from_the_wrong_template_says_which_permission_it_needs(api: FakeApi, kit: KdbxStore) -> None:
    read_only = api.add('kluster-seed', [{**MINTING_POLICY, 'permission_groups': [{'id': 'g'}]}])

    # The likely mistake with a console credential is the wrong template, and
    # `POST /user/tokens` would answer it without naming the permission.
    with pytest.raises(CredentialRejected, match=cloudflare.MINTING_PERMISSION):
        _ = cloudflare.adopt_seed(token=read_only, seeds=kit, seed_entry=SEED_ENTRY)
    assert not kit.has(SEED_ENTRY)


def test_an_unknown_token_is_reported_as_rejected(kit: KdbxStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(url: str, headers: dict[str, str], timeout: int) -> requests.Response:
        response = requests.Response()
        response.status_code = 401
        return response

    monkeypatch.setattr(cloudflare.requests, 'get', refuse)

    # The distinguishing case: the call arrived and the API said no, which is
    # nearly always the wrong field rather than the wrong network.
    with pytest.raises(CredentialRejected, match='rejected the token'):
        _ = cloudflare.adopt_seed(token='nonsense', seeds=kit, seed_entry=SEED_ENTRY)


def _refuse(_message: str) -> str:
    raise AssertionError('the run prompted for something a token can be asked')


def test_bootstrap_takes_the_seed_from_the_console(
    api: FakeApi, kit: KdbxStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    value = _seed(api)
    monkeypatch.setattr('getpass.getpass', lambda _prompt='': value)

    created = lifecycle.bootstrap(kit, prompt=_refuse, only='cloudflare')

    # The console steps are printed at the moment the run stops for them, and
    # the only thing asked for is the value the dashboard showed once.
    assert created == ['cloudflare']
    assert 'Create Additional Tokens' in caplog.text
    assert kit.get(SEED_ENTRY) == value
    assert kit.get(SEED_ENTRY, attribute='UserName') == api.values[value]


def test_rotation_is_the_same_console_visit_into_the_new_kit(
    api: FakeApi, kit: KdbxStore, memory_kit: KdbxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = _seed(api)
    _ = cloudflare.adopt_seed(token=previous, seeds=kit, seed_entry=SEED_ENTRY)
    current = _seed(api)
    monkeypatch.setattr('getpass.getpass', lambda _prompt='': current)

    rotated = lifecycle.rotate(kit, memory_kit, prompt=_refuse, only='cloudflare')

    # §4.2: the successor goes into the new kit and the retired one keeps the
    # predecessor. Deleting the predecessor is the operator's own next click,
    # not a call signed with a credential the retired kit still names.
    assert rotated == ['cloudflare']
    assert memory_kit.get(SEED_ENTRY) == current
    assert kit.get(SEED_ENTRY) == previous
    assert api.values[previous] in api.tokens


def test_the_seed_may_not_mint_a_successor_to_itself(api: FakeApi, memory_kit: KdbxStore) -> None:
    session = cloudflare.Session.authorize(_seed(api))

    # The finding this module is shaped around: whatever else a token can do,
    # it cannot hand its own class of permission to a token it mints, so
    # there is no API path to a successor seed and no account root either.
    with pytest.raises(CredentialRejected, match='not allowed to have permissions to manage other tokens'):
        _ = cloudflare.mint_token(session, 'kluster-seed', [MINTING_POLICY])


def test_a_minted_token_carries_the_policies_it_was_given(api: FakeApi) -> None:
    session = cloudflare.Session.authorize(_seed(api))

    token_id, value = cloudflare.mint_token(session, STACK_TOKEN, [ZONE_POLICY])

    # The caller states the policies; nothing is copied from the minter,
    # whose own policies are the ones a sub-token may not have.
    assert api.posted[-1] == {'name': STACK_TOKEN, 'policies': [ZONE_POLICY]}
    assert api.values[value] == token_id
    assert api.tokens[token_id]['name'] == STACK_TOKEN


def test_the_minted_token_is_verified_before_it_is_returned(api: FakeApi) -> None:
    session = cloudflare.Session.authorize(_seed(api))

    _ = cloudflare.mint_token(session, STACK_TOKEN, [ZONE_POLICY])

    # The call after the mint is the new token verifying itself: a token that
    # cannot authenticate must not reach a consumer's slot. What follows is
    # retirement, which runs as that same verified token.
    minted = api.calls.index(('POST', '/user/tokens'))
    assert api.calls[minted + 1] == ('GET', '/user/tokens/verify')


def test_minting_retires_the_predecessor_as_the_new_token(api: FakeApi) -> None:
    session = cloudflare.Session.authorize(_seed(api))
    previous, _ = cloudflare.mint_token(session, STACK_TOKEN, [ZONE_POLICY])
    orphan = api.values[api.add(STACK_TOKEN, [ZONE_POLICY])]

    current, _ = cloudflare.mint_token(session, STACK_TOKEN, [ZONE_POLICY])

    # Two tokens to delete: a session signing with one of them would stop
    # working partway through, so the retirement runs as the token that was
    # just minted. Rotating a §3 token is this same re-run.
    assert _named(api, STACK_TOKEN) == [current]
    assert previous not in api.tokens
    assert orphan not in api.tokens


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


def _named(api: FakeApi, name: str) -> list[str]:
    return [str(token['id']) for token in api.tokens.values() if token['name'] == name]


def _kit_never_lies(kit: KdbxStore, api: FakeApi) -> None:
    """Whatever the kit holds must be a token the API would still accept."""
    if not kit.has(SEED_ENTRY):
        return
    token_id = kit.get(SEED_ENTRY, attribute='UserName')
    assert api.values.get(kit.get(SEED_ENTRY)) == token_id, 'the stored value is not this token'
    assert token_id in api.tokens, 'the kit holds a token the account no longer has'


def _calls_made(operation: Callable[[FakeApi, KdbxStore], None]) -> int:
    """How many API calls one uninterrupted run makes, by measurement.

    The sweep then covers exactly the calls the operation makes today, and
    widens by itself when the operation grows one.
    """
    api = FakeApi()
    faulty = Faulty(api)
    original = (cloudflare.requests.get, cloudflare.requests.request)
    faulty.attach()
    try:
        operation(api, MemoryKit())
        return faulty.counted
    finally:
        setattr(cloudflare.requests, 'get', original[0])  # noqa: B010
        setattr(cloudflare.requests, 'request', original[1])  # noqa: B010


def _adopt(api: FakeApi, kit: KdbxStore) -> None:
    _ = cloudflare.adopt_seed(token=_seed(api), seeds=kit, seed_entry=SEED_ENTRY)


def _mint(api: FakeApi, _kit: KdbxStore) -> None:
    _ = cloudflare.mint_token(cloudflare.Session.authorize(_seed(api)), STACK_TOKEN, [ZONE_POLICY])


ADOPT_CALLS = _calls_made(_adopt)
MINT_CALLS = _calls_made(_mint)

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
@pytest.mark.parametrize('failing_call', range(1, ADOPT_CALLS + 1))
def test_adopting_the_seed_heals_from_a_failure_at_any_call(
    failing_call: int, when: str, api: FakeApi, memory_kit: KdbxStore, faulty: Callable[[int | None, str], Faulty]
) -> None:
    value = _seed(api)
    _ = faulty(failing_call, when)

    with pytest.raises(Interrupted):
        _ = cloudflare.adopt_seed(token=value, seeds=memory_kit, seed_entry=SEED_ENTRY)
    # Nothing was minted, so the only thing a crash can leave is a half-
    # written row -- which the kit must not have.
    _kit_never_lies(memory_kit, api)

    _ = faulty(None, when)
    token_id = cloudflare.adopt_seed(token=value, seeds=memory_kit, seed_entry=SEED_ENTRY)

    _kit_never_lies(memory_kit, api)
    assert memory_kit.get(SEED_ENTRY, attribute='UserName') == token_id


@pytest.mark.parametrize('when', CRASH_POINTS)
@pytest.mark.parametrize('failing_call', range(1, MINT_CALLS + 1))
def test_minting_a_token_heals_from_a_failure_at_any_call(
    failing_call: int, when: str, api: FakeApi, faulty: Callable[[int | None, str], Faulty]
) -> None:
    value = _seed(api)
    _ = faulty(failing_call, when)

    with pytest.raises(Interrupted):
        _ = cloudflare.mint_token(cloudflare.Session.authorize(value), STACK_TOKEN, [ZONE_POLICY])

    _ = faulty(None, when)
    token_id, _ = cloudflare.mint_token(cloudflare.Session.authorize(value), STACK_TOKEN, [ZONE_POLICY])

    # One token of that name stands, and it is the one the caller was handed:
    # a token minted by the run that died is a live permission whose value
    # nobody holds, and the re-run is what removes it.
    assert _named(api, STACK_TOKEN) == [token_id]
