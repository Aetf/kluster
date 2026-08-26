"""§3's rows end to end: minted from the seed, delivered into a stack's config.

Against the fake Cloudflare API and a recorded `pulumi`, because what is under
test is the shape of the procedure — mint, prove, push, prove — and its
idempotence. Both are properties of this repository rather than of either
platform.
"""

from __future__ import annotations

import pytest
from cloudflare_api import ACCOUNT_ID, FakeApi, console_seed
from fake_pulumi import RecordedPulumi
from memory_kit import MemoryKit

from kluster import conventions
from kluster.scripts.credentials import cloudflare, derived, pulumi_config
from kluster.scripts.credentials.kdbx import KdbxStore

STACK = derived.ZONES_STACK


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(cloudflare.requests, 'get', fake.get)
    monkeypatch.setattr(cloudflare.requests, 'request', fake.request)
    for name in conventions.ALL_ZONES:
        _ = fake.add_zone(name)
    return fake


@pytest.fixture
def kit(api: FakeApi) -> KdbxStore:
    store = MemoryKit()
    _ = cloudflare.adopt_seed(token=console_seed(api), seeds=store, seed_entry=derived.SEED_ENTRY)
    return store


@pytest.fixture
def stack() -> tuple[pulumi_config.Stack, RecordedPulumi]:
    runner = RecordedPulumi()
    return pulumi_config.Stack(name=STACK, directory=pulumi_config.project_dir(), run=runner), runner


def _live(api: FakeApi) -> list[str]:
    return [str(token['id']) for token in api.tokens.values() if token['name'] == derived.ZONES_TOKEN_NAME]


def test_the_token_and_the_account_land_in_the_stack_config(
    api: FakeApi, kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack

    account = derived.cloudflare_zones(kit, stack=slot)

    # One command, both keys: the provider's credential and the account whose
    # zones it may touch, the second discovered on the way to the first.
    (token_id,) = _live(api)
    assert runner.config[derived.API_TOKEN_KEY] in api.values
    assert api.values[runner.config[derived.API_TOKEN_KEY]] == token_id
    assert runner.config[derived.ACCOUNT_KEY] == account == ACCOUNT_ID


def test_the_stack_is_created_when_the_backend_has_none(
    kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack

    _ = derived.cloudflare_zones(kit, stack=slot)

    # A workstation that has never selected this stack is the ordinary case at
    # bring-up, so the push cannot assume one exists.
    assert runner.stacks == [STACK]


def test_the_minted_token_never_touches_the_kit(
    kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack

    _ = derived.cloudflare_zones(kit, stack=slot)

    # Rule 2: the offline store is not a staging area. The kit holds the seed
    # it held before, and the minted value exists only in the slot.
    assert kit.entries() == [derived.SEED_ENTRY]
    assert runner.config[derived.API_TOKEN_KEY] != kit.get(derived.SEED_ENTRY)


def test_a_re_run_rotates_the_row_and_leaves_one_live_token(
    api: FakeApi, kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack
    _ = derived.cloudflare_zones(kit, stack=slot)
    first = runner.config[derived.API_TOKEN_KEY]

    _ = derived.cloudflare_zones(kit, stack=slot)

    # Rotation is a re-run, not a second procedure: the predecessor is retired
    # once its successor is verified, and the slot names the survivor.
    second = runner.config[derived.API_TOKEN_KEY]
    assert second != first
    assert _live(api) == [api.values[second]]


def test_a_push_that_fails_is_healed_by_running_it_again(
    api: FakeApi, kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack
    runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused):
        _ = derived.cloudflare_zones(kit, stack=slot)

    # The interrupted run left a live token nobody holds; the re-run mints its
    # successor, retires it, and fills the slot, which is why a failed stage is
    # re-run rather than repaired by hand.
    runner.corrupts = False
    _ = derived.cloudflare_zones(kit, stack=slot)
    assert _live(api) == [api.values[runner.config[derived.API_TOKEN_KEY]]]
