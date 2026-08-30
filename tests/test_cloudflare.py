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

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests
from cloudflare_api import ACCOUNT_ID, MINTING_POLICY, ZONE_POLICY, FakeApi, console_seed
from memory_kit import MemoryKit

from kluster import conventions
from kluster.scripts.credentials import cloudflare, entries, lifecycle
from kluster.scripts.credentials.kdbx import KdbxStore
from kluster.scripts.credentials.masters import CredentialRejected

PASSWORD = 'kit-password'
SEED_ENTRY = entries.SEEDS['cloudflare'].entry

#: The name a per-stack token is minted under, in these tests.
STACK_TOKEN = 'kluster-dns'


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
    """A console-made seed, in an account that has at least one zone.

    Adoption proves the seed can list zones, and an account with no zone in it
    answers that listing the same way a blind seed does. Giving the account a
    zone is what makes the refusal below about the token.
    """
    if not api.zones:
        _ = api.add_zone(conventions.ZONE_PRIMARY)
    return console_seed(api)


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


def test_a_seed_that_cannot_see_zones_is_refused_at_adoption(api: FakeApi, kit: KdbxStore) -> None:
    value = _seed(api)
    api.seed_sees_zones = False

    # The minting template alone authenticates and mints, so `require_minting`
    # accepts it; the first zone-scoped mint is where it would otherwise fail,
    # long after the console visit that fixes it is over. The refusal names the
    # permission and says the fix is a new token rather than an edited one.
    with pytest.raises(CredentialRejected, match=cloudflare.ZONE_VISIBILITY_PERMISSION):
        _ = cloudflare.adopt_seed(token=value, seeds=kit, seed_entry=SEED_ENTRY)
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

    minted = cloudflare.mint_token(session, STACK_TOKEN, [ZONE_POLICY])
    token_id, value = minted.token_id, minted.value

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


def test_minting_retires_the_predecessor_as_the_minting_token(api: FakeApi) -> None:
    session = cloudflare.Session.authorize(_seed(api))
    previous = cloudflare.mint_token(session, STACK_TOKEN, [ZONE_POLICY]).token_id
    orphan = api.values[api.add(STACK_TOKEN, [ZONE_POLICY])]

    current = cloudflare.mint_token(session, STACK_TOKEN, [ZONE_POLICY]).token_id

    # Two tokens to delete, and only the seed can delete either: listing and
    # deleting tokens are token permissions, which a minted §3 token may not
    # carry, so a retirement signed with the new token is refused outright.
    # Rotating a §3 token is this same re-run.
    assert _named(api, STACK_TOKEN) == [current]
    assert previous not in api.tokens
    assert orphan not in api.tokens


def test_a_minted_token_may_not_manage_tokens_at_all(api: FakeApi) -> None:
    session = cloudflare.Session.authorize(_seed(api))
    value = cloudflare.mint_token(session, STACK_TOKEN, [ZONE_POLICY]).value

    # The platform rule this module is shaped around, seen from the other side:
    # the class of token the seed is allowed to mint cannot even enumerate
    # tokens, so nothing in a §3 credential's own procedure may try to.
    with pytest.raises(CredentialRejected, match='Unauthorized to access requested resource'):
        _ = cloudflare.Session.authorize(value).tokens()


def _estate(api: FakeApi) -> dict[str, str]:
    """The zones the register scopes the provider token to, in the fake account."""
    return {name: api.add_zone(name) for name in conventions.ALL_ZONES}


def test_the_zones_token_is_scoped_to_exactly_the_estate_zones(api: FakeApi) -> None:
    zone_ids = _estate(api)
    api.add_zone('someone-elses.example')
    session = cloudflare.Session.authorize(_seed(api))

    minted = cloudflare.mint_zone_token(session, name=STACK_TOKEN, zones=conventions.ALL_ZONES)

    # One policy: record edit on the estate's zones by id, the read the
    # provider needs beside it, and no token permission — the only class the
    # platform mints. A zone that is not the estate's is not in it.
    (policy,) = list[dict[str, Any]](api.posted[-1]['policies'])
    assert set(policy['resources']) == {f'{cloudflare.ZONE_RESOURCE}.{zone_id}' for zone_id in zone_ids.values()}
    assert policy['permission_groups'] == [{'id': 'dns-write'}, {'id': 'zone-read'}]
    assert minted.account_id == ACCOUNT_ID
    assert minted.zone_ids == zone_ids


def test_a_zone_that_names_no_account_is_refused(api: FakeApi) -> None:
    zones = _estate(api)
    api.zones[conventions.ZONE_FAMILY[0]]['account'] = {}
    session = cloudflare.Session.authorize(_seed(api))

    # The empty string satisfies "the zones are all in one account" and is
    # then written into a stack's committed configuration as the account id,
    # where nothing ever refuses it.
    with pytest.raises(CredentialRejected, match='no account id'):
        _ = cloudflare.mint_zone_token(session, name=STACK_TOKEN, zones=list(zones))
    assert not _named(api, STACK_TOKEN)


def test_a_zone_the_seed_cannot_see_is_refused_before_anything_is_minted(api: FakeApi) -> None:
    zones = _estate(api)
    del api.zones[conventions.ZONE_FAMILY[0]]
    session = cloudflare.Session.authorize(_seed(api))

    # A token scoped to five of six zones would manage records everywhere but
    # one and say nothing about it, so resolution is where the run stops.
    with pytest.raises(CredentialRejected, match=conventions.ZONE_FAMILY[0]):
        _ = cloudflare.mint_zone_token(session, name=STACK_TOKEN, zones=list(zones))
    assert not _named(api, STACK_TOKEN)


def test_a_seed_without_zone_read_says_so_rather_than_minting_an_empty_scope(api: FakeApi) -> None:
    _ = _estate(api)
    api.seed_sees_zones = False
    session = cloudflare.Session.authorize(_seed(api))

    with pytest.raises(CredentialRejected, match='no zone read permission'):
        _ = cloudflare.mint_zone_token(session, name=STACK_TOKEN, zones=conventions.ALL_ZONES)


def test_a_token_narrower_than_it_was_asked_for_never_reaches_a_slot(api: FakeApi) -> None:
    zone_ids = _estate(api)
    api.withholds_zone = zone_ids[conventions.ZONE_PRIMARY]
    session = cloudflare.Session.authorize(_seed(api))

    # The mint call reports what it created; the check asks the credential
    # itself what it can reach, which is the thing the consumer depends on.
    with pytest.raises(CredentialRejected, match=conventions.ZONE_PRIMARY):
        _ = cloudflare.mint_zone_token(session, name=STACK_TOKEN, zones=conventions.ALL_ZONES)


def test_a_wrong_scope_mint_leaves_the_working_predecessor_standing(api: FakeApi) -> None:
    zone_ids = _estate(api)
    session = cloudflare.Session.authorize(_seed(api))
    working = cloudflare.mint_zone_token(session, name=STACK_TOKEN, zones=conventions.ALL_ZONES)
    api.withholds_zone = zone_ids[conventions.ZONE_PRIMARY]

    with pytest.raises(CredentialRejected, match=conventions.ZONE_PRIMARY):
        _ = cloudflare.mint_zone_token(session, name=STACK_TOKEN, zones=conventions.ALL_ZONES)

    # Scope is confirmed before anything is retired, so a run that mints a
    # credential it cannot use costs nothing that still works. What it does
    # leave behind is a same-named stray, and the next run that gets a good
    # token deletes it by name -- which is how a failed run is recovered from.
    assert working.token_id in api.tokens
    api.withholds_zone = None

    replacement = cloudflare.mint_zone_token(session, name=STACK_TOKEN, zones=conventions.ALL_ZONES)

    assert _named(api, STACK_TOKEN) == [replacement.token_id]


def test_a_renamed_permission_group_is_named_rather_than_guessed(api: FakeApi) -> None:
    _ = _estate(api)
    api.groups['dns-write'] = {**api.groups['dns-write'], 'name': 'DNS Edit'}
    session = cloudflare.Session.authorize(_seed(api))

    # The ids are account data and the names are the register's, so a rename
    # on the platform's side has to surface as a refusal rather than as a
    # policy missing a permission.
    with pytest.raises(CredentialRejected, match='DNS Write'):
        _ = cloudflare.mint_zone_token(session, name=STACK_TOKEN, zones=conventions.ALL_ZONES)


def test_an_account_larger_than_one_page_is_listed_whole(api: FakeApi) -> None:
    expected = {f'filler-{index}.example' for index in range(cloudflare.PAGE_SIZE + 3)}
    for name in expected:
        _ = api.add_zone(name)
    session = cloudflare.Session.authorize(_seed(api))

    # A listing that stopped at the first page would report a token as unable
    # to see zones it can see, and the scope check is built on this call.
    assert {str(zone['name']) for zone in session.zones()} == expected


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
    token_id = cloudflare.mint_token(cloudflare.Session.authorize(value), STACK_TOKEN, [ZONE_POLICY]).token_id

    # One token of that name stands, and it is the one the caller was handed:
    # a token minted by the run that died is a live permission whose value
    # nobody holds, and the re-run is what removes it.
    assert _named(api, STACK_TOKEN) == [token_id]
