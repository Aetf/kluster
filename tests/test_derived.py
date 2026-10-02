"""§3's rows end to end: minted from a seed, delivered into the slot the row names.

Against fakes of the three platforms and a recorded `pulumi`, because what is
under test is the shape of the procedure — mint, prove, push, prove, retire —
and its idempotence. Both are properties of this repository rather than of any
provider.

The order of the last two is a property in its own right, and every provider
here has a case pinning it: a push that fails leaves the predecessor live,
because until the push returns the freshly minted credential exists in this
process alone. The recorded `pulumi` refuses a read-back on demand, which is
one of the ways `stack.fill` fails for real.

The OCI tenancy is the fake in `oci_tenancy`, imported rather than rebuilt:
one fake per platform, in a module of its own that every suite minting against
the platform imports. What it encodes matters here — the identity domain
serves the self-service endpoints to anyone who authenticates and the
administrative ones only to a domain administrator, which the seed is not, so
a mint for somebody else's user is served by the legacy shim.
"""

# The SDK ships no stubs; the same waiver `oci_iam.py` itself carries.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import ipaddress
import shutil
from pathlib import Path

import b2_api
import pytest
from cloudflare_api import ACCOUNT_ID, FakeApi, console_seed
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fake_pulumi import RecordedPulumi
from memory_kit import MemoryKit
from oci_conventions import with_recorded_compartment, with_tenancy_ocid, with_unrecorded_compartment
from oci_tenancy import KEY_LISTINGS, ROOT_USER, TENANCY, Tenancy

from kluster import conventions
from kluster.lib import config as lib_config
from kluster.lib import stack_environment
from kluster.lib import workstation as lib_workstation
from kluster.lib.state_backend import settings as appliance_settings
from kluster.scripts.credentials import (
    age,
    b2,
    cloudflare,
    derived,
    entries,
    escrow,
    masters,
    oci_iam,
    pki,
    pulumi_config,
)

# Aliased: `slots` is the name a fixture below gives the workstation's
# `.credentials/` directory, and the map is a different thing entirely.
from kluster.scripts.credentials import slots as slot_map
from kluster.scripts.credentials.kdbx import KdbxStore

needs_age = pytest.mark.skipif(shutil.which(age.BINARY) is None, reason='age is not on PATH (mise x -- ...)')

STACK = derived.ZONES_STACK
COMPARTMENT = 'ocid1.compartment.oc1..physical'


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(cloudflare.requests, 'get', fake.get)
    monkeypatch.setattr(cloudflare.requests, 'request', fake.request)
    for name in conventions.ALL_ZONES:
        _ = fake.add_zone(name)
    return fake


def _with_cloudflare_account(monkeypatch: pytest.MonkeyPatch, account_id: str) -> None:
    """Make `account_id` the account `conventions` records, for one test.

    The convention is one frozen structure, so it is replaced whole rather than
    reached into -- the same way `oci_conventions` puts the tenancy into
    another state.
    """
    monkeypatch.setattr(conventions, 'CLOUDFLARE_ACCOUNT', conventions.CloudflareAccount(account_id=account_id))


@pytest.fixture(autouse=True)
def recorded_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the fake platform's account the one `conventions` records.

    The zones mint holds the account it minted in against that fact, so every
    row below runs against a `conventions` that agrees with the fake — the one
    test that wants them to disagree undoes this for itself.
    """
    _with_cloudflare_account(monkeypatch, ACCOUNT_ID)


@pytest.fixture
def kit(api: FakeApi) -> KdbxStore:
    store = MemoryKit()
    _ = cloudflare.adopt_seed(token=console_seed(api), seeds=store, seed_entry=derived.CLOUDFLARE_SEED_ENTRY)
    return store


@pytest.fixture
def stack() -> tuple[pulumi_config.Stack, RecordedPulumi]:
    runner = RecordedPulumi()
    return pulumi_config.Stack(name=STACK, directory=pulumi_config.project_dir(), run=runner), runner


def _live(api: FakeApi) -> list[str]:
    return [str(token['id']) for token in api.tokens.values() if token['name'] == cloudflare.ZONES.name]


def test_the_token_lands_in_the_stack_config_and_nothing_else_does(
    api: FakeApi, kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack

    derived.cloudflare_zones(kit, stacks=[slot])

    # One key: the provider's credential. The account whose zones it may touch
    # is discovered on the way, but it is a fact `conventions` already holds,
    # so it is proven rather than delivered.
    (token_id,) = _live(api)
    assert list(runner.config) == [derived.API_TOKEN_KEY]
    assert runner.config[derived.API_TOKEN_KEY] in api.values
    assert api.values[runner.config[derived.API_TOKEN_KEY]] == token_id


def test_a_seed_from_another_account_is_refused_before_anything_is_minted(
    api: FakeApi, kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi], monkeypatch: pytest.MonkeyPatch
) -> None:
    slot, runner = stack
    _with_cloudflare_account(monkeypatch, 'some-other-account')

    with pytest.raises(masters.CredentialRejected, match='CLOUDFLARE_ACCOUNT'):
        derived.cloudflare_zones(kit, stacks=[slot])

    # A kit re-seeded from another Cloudflare account, or an identifier written
    # down wrong: either way the token would be for zones the stack does not
    # declare into, and the stack would keep naming the account it does.
    assert derived.API_TOKEN_KEY not in runner.config
    # Nothing reaches the slot, and nothing is left at the provider either: a
    # token minted into a foreign account by a run that then refused is a live
    # permission this register does not record and nobody knows to revoke.
    assert _live(api) == []


def test_the_stack_is_created_when_the_backend_has_none(
    kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack

    derived.cloudflare_zones(kit, stacks=[slot])

    # A workstation that has never selected this stack is the ordinary case at
    # bring-up, so the push cannot assume one exists.
    assert runner.stacks == [STACK]


def _inits(runner: RecordedPulumi) -> list[list[str]]:
    return [args for args in runner.invocations if args[:2] == ['stack', 'init']]


def test_a_stack_no_program_declares_is_refused_naming_the_census(api: FakeApi, kit: KdbxStore) -> None:
    runner = RecordedPulumi()
    slot = pulumi_config.Stack(name='dsn', directory=pulumi_config.project_dir(), run=runner)

    with pytest.raises(pulumi_config.SlotRefused, match=r'dsn.*k8s-base') as refusal:
        derived.cloudflare_zones(kit, stacks=[slot])

    # A misspelled stack would otherwise be created in the backend and
    # filled, with the real stack's token retired by name on the way. Refused
    # before the seed is opened: no `pulumi` runs, nothing is minted.
    assert all(name in str(refusal.value) for name in conventions.STACK_NAMES.names())
    assert runner.invocations == []
    assert _live(api) == []


APPS = conventions.STACK_NAMES.apps


@pytest.fixture
def apps_stack() -> tuple[pulumi_config.Stack, RecordedPulumi]:
    """`apps`, which exists: the operator's `pulumi stack init` brought it up."""
    runner = RecordedPulumi(stacks=[APPS])
    return pulumi_config.Stack(name=APPS, directory=pulumi_config.project_dir(), run=runner), runner


def _retirements(api: FakeApi) -> list[str]:
    """The token ids the run deleted, in order, one entry per delete."""
    return [path.removeprefix('/user/tokens/') for method, path in api.calls if method == 'DELETE']


def test_one_mint_leaves_one_live_token_in_every_stack_and_retires_its_predecessor_once(
    api: FakeApi,
    kit: KdbxStore,
    stack: tuple[pulumi_config.Stack, RecordedPulumi],
    apps_stack: tuple[pulumi_config.Stack, RecordedPulumi],
) -> None:
    """`dns` and `apps` hold one token, and the run retires what it supersedes after both hold it.

    Retirement matches on the token's name, so a mint per stack would leave
    the first stack holding a token the second stack's mint had deleted.
    """
    (dns_slot, dns_runner), (apps_slot, apps_runner) = stack, apps_stack
    derived.cloudflare_zones(kit, stacks=[dns_slot, apps_slot])
    (predecessor,) = _live(api)
    before = len(api.calls)

    derived.cloudflare_zones(kit, stacks=[dns_slot, apps_slot])

    held = dns_runner.config[derived.API_TOKEN_KEY]
    assert apps_runner.config[derived.API_TOKEN_KEY] == held
    assert _live(api) == [api.values[held]]
    assert [path for method, path in api.calls[before:] if method == 'DELETE'] == [f'/user/tokens/{predecessor}']


def test_a_write_that_fails_in_the_second_stack_retires_nothing(
    api: FakeApi,
    kit: KdbxStore,
    stack: tuple[pulumi_config.Stack, RecordedPulumi],
    apps_stack: tuple[pulumi_config.Stack, RecordedPulumi],
) -> None:
    """The stack the run did not reach keeps a token that still works.

    The first stack took the successor; the second refused it. Retiring at
    that point would delete the token `apps` is still reading, so nothing is
    retired, and the next run that gets through both stacks reconciles.
    """
    (dns_slot, _), (apps_slot, apps_runner) = stack, apps_stack
    derived.cloudflare_zones(kit, stacks=[dns_slot, apps_slot])
    (predecessor,) = _live(api)
    apps_runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused):
        derived.cloudflare_zones(kit, stacks=[dns_slot, apps_slot])

    assert _retirements(api) == []
    assert predecessor in _live(api)


def test_a_stack_that_does_not_exist_refuses_the_mint_before_anything_is_created(
    api: FakeApi, kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    dns_slot, dns_runner = stack
    runner = RecordedPulumi()
    slot = pulumi_config.Stack(name=APPS, directory=pulumi_config.project_dir(), run=runner)

    with pytest.raises(pulumi_config.SlotRefused, match='apps stack does not exist'):
        derived.cloudflare_zones(kit, stacks=[dns_slot, slot])

    # The row's own stack is created by its first mint, which is bring-up;
    # any other stack is brought up on its own, and a mint aimed there fills
    # it or refuses -- and refuses before the row's own stack is created or
    # anything is minted, so a refusal leaves nothing half-delivered.
    assert _inits(runner) == [] and _inits(dns_runner) == []
    assert runner.stacks == [] and dns_runner.stacks == []
    assert _live(api) == []


def test_a_mint_with_no_stack_to_deliver_into_is_refused(api: FakeApi, kit: KdbxStore) -> None:
    with pytest.raises(pulumi_config.SlotRefused, match='names no stack'):
        derived.cloudflare_zones(kit, stacks=[])

    assert _live(api) == []


def test_the_minted_token_never_touches_the_kit(
    kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack

    derived.cloudflare_zones(kit, stacks=[slot])

    # Rule 2: the offline store is not a staging area. The kit holds the seed
    # it held before, and the minted value exists only in the slot.
    assert kit.entries() == [derived.CLOUDFLARE_SEED_ENTRY]
    assert runner.config[derived.API_TOKEN_KEY] != kit.get(derived.CLOUDFLARE_SEED_ENTRY)


def test_a_re_run_rotates_the_row_and_leaves_one_live_token(
    api: FakeApi, kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack
    derived.cloudflare_zones(kit, stacks=[slot])
    first = runner.config[derived.API_TOKEN_KEY]

    derived.cloudflare_zones(kit, stacks=[slot])

    # Rotation is a re-run, not a second procedure: the predecessor is retired
    # once its successor is verified and the slot has taken it, and the slot
    # names the survivor.
    second = runner.config[derived.API_TOKEN_KEY]
    assert second != first
    assert _live(api) == [api.values[second]]


@pytest.fixture
def live_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[pulumi_config.Stack, str]:
    """A stack in a throwaway project driven by the real CLI, and the project's name.

    The name is deliberately not this repository's: what the test pins is that
    the key lands in whatever namespace the project happens to have, which is
    the namespace `pulumi.Config()` resolves against inside the program.
    """
    if shutil.which('pulumi') is None:
        pytest.skip('the pinned pulumi CLI is not on PATH')
    project = tmp_path / 'project'
    project.mkdir()
    name = 'namespace-probe'
    _ = (project / 'Pulumi.yaml').write_text(f'name: {name}\nruntime: nodejs\ndescription: namespace probe\n')
    state = tmp_path / 'state'
    state.mkdir()
    # A home of its own, in the ambient environment: it is not part of what a
    # `credentials` run knows, and `run_pulumi` overlays the stack's own
    # variables on whatever is already there.
    monkeypatch.setenv('PULUMI_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('PULUMI_SKIP_UPDATE_CHECK', 'true')
    stack = pulumi_config.Stack(
        name=STACK,
        directory=project,
        # The passphrase and the URL go in as the shape a `credentials` run
        # builds, so the probe exercises the resolution the real callers use:
        # the stack picks its own passphrase out of this by its own name.
        environment=pulumi_config.BackendEnvironment(passphrase='probe-passphrase', url=state.as_uri()),
    )
    return stack, name


def test_the_token_lands_where_the_program_reads_it(
    kit: KdbxStore, live_project: tuple[pulumi_config.Stack, str]
) -> None:
    slot, project = live_project

    derived.cloudflare_zones(kit, stacks=[slot])

    # The consumer asks `pulumi.Config().require_secret('cloudflareApiToken')`,
    # which resolves under the project's name. A key this command spelled a
    # namespace into itself would sit next to that one and never be read, so
    # the push hands the CLI a bare key and lets it apply the namespace -- and
    # the namespace it applies is this project's, not the provider package's.
    committed = (slot.directory / f'Pulumi.{STACK}.yaml').read_text()
    assert f'{project}:{derived.API_TOKEN_KEY}:' in committed
    assert 'cloudflare:' not in committed


def test_a_push_that_fails_leaves_the_token_the_stack_already_holds_live(
    api: FakeApi, kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack
    derived.cloudflare_zones(kit, stacks=[slot])
    (predecessor,) = _live(api)
    runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused):
        derived.cloudflare_zones(kit, stacks=[slot])

    # Cloudflare shows a token's value once, so between the mint and the push
    # the successor exists in this process and nowhere else. Retired first, the
    # run would end with the `dns` stack naming a token the account has deleted
    # and the working one gone with the process; retired last, the failure
    # costs a re-run. Two tokens stand, and the live one is the one the stack
    # is still holding.
    assert predecessor in _live(api)
    assert len(_live(api)) == 2

    # The strays are reconciled by the row itself: retirement matches on the
    # token name rather than on a recorded predecessor, so the next run that
    # gets as far as its push deletes everything the failed ones left.
    runner.corrupts = False
    derived.cloudflare_zones(kit, stacks=[slot])
    assert _live(api) == [api.values[runner.config[derived.API_TOKEN_KEY]]]


def test_a_push_that_fails_is_healed_by_running_it_again(
    api: FakeApi, kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack
    runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused):
        derived.cloudflare_zones(kit, stacks=[slot])

    # The interrupted run left a live token nobody holds; the re-run mints its
    # successor, retires it, and fills the slot, which is why a failed stage is
    # re-run rather than repaired by hand.
    runner.corrupts = False
    derived.cloudflare_zones(kit, stacks=[slot])
    assert _live(api) == [api.values[runner.config[derived.API_TOKEN_KEY]]]


# -- the gateway's own ACME token: the second token from the same seed --


def _gateway_live(api: FakeApi) -> list[str]:
    return [str(token['id']) for token in api.tokens.values() if token['name'] == cloudflare.GATEWAY_ACME.name]


def _vhost_zone(name: str) -> str:
    """The zone a vhost name is served under, as `conventions` spells the zones."""
    matches = [zone for zone in conventions.ALL_ZONES if name == zone or name.endswith(f'.{zone}')]
    assert len(matches) == 1, f'{name} is served under {len(matches)} zones this program declares'
    return matches[0]


def test_the_token_scope_is_the_zone_set_the_gateway_vhosts_need() -> None:
    # The mint's scope is stated in `derived.py` rather than imported from the
    # gateway module, which would drag the Pulumi SDKs into
    # `credentials --help`. This is what holds the two equal: a vhost moved to
    # another zone fails here rather than at a renewal on the device months
    # later, and a zone left in the set after its last vhost leaves fails too.
    vhosts = [
        conventions.gateway.VHOST_CONTROLLER,
        *(service.vhost for service in conventions.gateway.RESOLVERS),
        # The names served for applications that have not migrated. They are in
        # a zone of their own, so they widen the scope while any of them
        # remains and narrow it again when the census empties.
        *(vhost.host for vhost in conventions.gateway.LEGACY_VHOSTS),
    ]
    served = {_vhost_zone(name) for name in vhosts if name is not None}

    assert served == set(derived.GATEWAY_ACME_ZONES)


def test_the_gateway_token_lands_in_the_stack_config_and_sees_only_its_own_zone(
    api: FakeApi, kit: KdbxStore, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack

    token_id = derived.cloudflare_gateway_acme(kit, stack=slot)

    # The credential the device answers a DNS-01 challenge with, and nothing
    # beside it: caddy signs with the token and never names an account.
    assert _gateway_live(api) == [token_id]
    assert api.values[runner.config[derived.GATEWAY_ACME_KEY]] == token_id
    assert list(runner.config) == [derived.GATEWAY_ACME_KEY]
    delivered = cloudflare.Session.authorize(runner.config[derived.GATEWAY_ACME_KEY])
    scoped = {zone.name for zone in delivered.zones()}
    assert scoped == set(derived.GATEWAY_ACME_ZONES)
    # Narrower than the provider token's on purpose: the device holding this
    # one is the machine the cluster cannot re-seal.
    assert scoped < set(conventions.ALL_ZONES)


def test_the_gateway_row_is_refused_in_another_account_before_anything_is_minted(
    api: FakeApi,
    kit: KdbxStore,
    physical_stack: tuple[pulumi_config.Stack, RecordedPulumi],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot, runner = physical_stack
    _with_cloudflare_account(monkeypatch, 'some-other-account')

    with pytest.raises(masters.CredentialRejected, match='CLOUDFLARE_ACCOUNT'):
        _ = derived.cloudflare_gateway_acme(kit, stack=slot)

    # This row delivers no account identifier and its consumer names none, but
    # the check belongs to the mint rather than to a row, so it is held to the
    # recorded account exactly as the zones row is -- and leaves nothing live
    # in an account this installation does not own.
    assert runner.config == {}
    assert _gateway_live(api) == []


def test_the_two_cloudflare_rows_are_separate_credentials(
    api: FakeApi,
    kit: KdbxStore,
    stack: tuple[pulumi_config.Stack, RecordedPulumi],
    physical_stack: tuple[pulumi_config.Stack, RecordedPulumi],
) -> None:
    zones_slot, zones_runner = stack
    gateway_slot, gateway_runner = physical_stack
    derived.cloudflare_zones(kit, stacks=[zones_slot])

    _ = derived.cloudflare_gateway_acme(kit, stack=gateway_slot)

    # Two issuers that have to survive each other's outage do not share a
    # credential, so minting one must not disturb the other: retirement matches
    # on the token name, and the two rows carry different ones.
    assert zones_runner.config[derived.API_TOKEN_KEY] != gateway_runner.config[derived.GATEWAY_ACME_KEY]
    assert len(_live(api)) == 1
    assert len(_gateway_live(api)) == 1


def test_a_re_run_rotates_the_gateway_token_and_leaves_one_live(
    api: FakeApi, kit: KdbxStore, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack
    first = derived.cloudflare_gateway_acme(kit, stack=slot)

    second = derived.cloudflare_gateway_acme(kit, stack=slot)

    # Rotation is a re-run: the predecessor is retired once its successor is
    # verified and the slot has taken it, and the slot names the survivor.
    assert second != first
    assert _gateway_live(api) == [second]
    assert api.values[runner.config[derived.GATEWAY_ACME_KEY]] == second


def test_the_minted_gateway_token_never_touches_the_kit(
    kit: KdbxStore, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack

    _ = derived.cloudflare_gateway_acme(kit, stack=slot)

    # Rule 2 again: the kit holds the seed it held before, and the minted value
    # exists only in the slot.
    assert kit.entries() == [derived.CLOUDFLARE_SEED_ENTRY]
    assert runner.config[derived.GATEWAY_ACME_KEY] != kit.get(derived.CLOUDFLARE_SEED_ENTRY)


def test_a_gateway_push_that_fails_is_healed_by_running_it_again(
    api: FakeApi, kit: KdbxStore, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack
    runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused):
        _ = derived.cloudflare_gateway_acme(kit, stack=slot)

    # The interrupted run left a live token nobody holds; the re-run mints its
    # successor, retires it, and fills the slot, which is why a failed stage is
    # re-run rather than repaired by hand.
    runner.corrupts = False
    _ = derived.cloudflare_gateway_acme(kit, stack=slot)
    assert _gateway_live(api) == [api.values[runner.config[derived.GATEWAY_ACME_KEY]]]


# -- the OCI rows: a user, a group, a policy and the key that signs as them --


#: What a run against `physical` needs on top of the defaults: that stack is
#: encrypted under a passphrase of its own, and is refused where none is given
#: (`pulumi_config.PHYSICAL`).
PHYSICAL_EQUIPPED = pulumi_config.BackendEnvironment(physical=lambda: 'the-physical-passphrase')


@pytest.fixture
def physical_stack() -> tuple[pulumi_config.Stack, RecordedPulumi]:
    runner = RecordedPulumi()
    return (
        pulumi_config.Stack(
            name=derived.PHYSICAL_STACK,
            directory=pulumi_config.project_dir(),
            environment=PHYSICAL_EQUIPPED,
            run=runner,
        ),
        runner,
    )


@pytest.fixture
def tenancy(monkeypatch: pytest.MonkeyPatch) -> Tenancy:
    """The fake account, and `conventions` recording it as this program's own.

    The mint holds the account it authenticated against against the one
    `conventions` names, so a suite that drives a fake account has to be that
    account for the ordinary path to be the one under test.
    """
    with_tenancy_ocid(monkeypatch, TENANCY)
    return Tenancy()


@pytest.fixture
def oci_kit(tenancy: Tenancy) -> KdbxStore:
    """A kit holding the OCI seed, created the way a bring-up creates it."""
    store = MemoryKit()
    private_pem = oci_iam.generate_key().private_pem
    root = masters.Credential(
        root=masters.ROOTS['oci'],
        values={'tenancy': TENANCY, 'user': ROOT_USER, 'private-key': private_pem},
    )
    _ = oci_iam.create_seed(root=root, seeds=store, seed_entry=derived.OCI_SEED_ENTRY, connect=tenancy)
    return store


def _named(tenancy: Tenancy, name: str) -> str:
    """The OCID of the user of that name, which the mint is expected to create."""
    return next(user.id for user in tenancy.identity.users.values() if user.name == name)


def test_the_physical_stack_gets_the_signing_configuration_a_provider_needs(
    oci_kit: KdbxStore, tenancy: Tenancy, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack

    user = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    # A provider cannot sign with two of the three: the fingerprint is
    # computed from the key that was pushed.
    assert runner.config[derived.OCI_USER_KEY] == user
    private = runner.config[derived.OCI_PRIVATE_KEY_KEY]
    assert runner.config[derived.OCI_FINGERPRINT_KEY] == oci_iam.fingerprint(private)
    assert oci_iam.fingerprint(private) in tenancy.identity.keys[user]
    # And nothing else. Which account the key acts in and where inside it are
    # conventions rather than config keys -- the tenancy OCID and the region
    # are permanent per account and the compartment is a boundary this program
    # decides -- so the delivery restates none of them.
    assert 'ociTenancyOcid' not in runner.config
    assert 'compartmentId' not in runner.config
    assert not [key for key in runner.config if 'egion' in key]
    # Nor does any of it land in a provider's own namespace: the stack program
    # builds that provider from these keys (rfc-002 §8.1).
    assert not [key for key in runner.config if ':' in key]


def test_every_part_of_the_signing_configuration_is_a_secret(
    oci_kit: KdbxStore, tenancy: Tenancy, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack

    _ = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    # Which channel each key takes is the assertion, not merely that the value
    # arrived. All three go in encrypted: the key obviously, the fingerprint
    # because it identifies the key, and the user OCID because it is the class
    # of fact the kit itself keeps protected. Nothing about this credential is
    # plain, which is why the plain half is empty rather than merely small.
    secret = [args[2] for args in runner.invocations if args[:2] == ['config', 'set'] and '--secret' in args]
    plain = [args[2] for args in runner.invocations if args[:2] == ['config', 'set'] and '--secret' not in args]
    assert set(secret) == {
        derived.OCI_USER_KEY,
        derived.OCI_FINGERPRINT_KEY,
        derived.OCI_PRIVATE_KEY_KEY,
    }
    assert plain == []


def test_the_compartment_comes_from_conventions_when_no_flag_names_one(
    oci_kit: KdbxStore,
    tenancy: Tenancy,
    physical_stack: tuple[pulumi_config.Stack, RecordedPulumi],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot, _ = physical_stack
    # The pre-record state: `conventions` names the compartment but holds no
    # OCID yet, which is every consumer's shape before its first mint.
    intended = with_unrecorded_compartment(monkeypatch, conventions.PHYSICAL)

    _ = derived.oci_physical(oci_kit, stack=slot, connect=tenancy)

    # The ordinary bring-up names no compartment: `conventions` does, and the
    # mint creates the one this consumer has no compartment for yet.
    created = next(iter(tenancy.identity.compartments.values()))
    name = f'{conventions.CLUSTER_NAME}-{derived.PHYSICAL_STACK}'
    assert created.name == intended.name
    assert [policy.statements for policy in tenancy.identity.policies.values() if policy.name == name] == [
        [f'Allow group {name} to manage all-resources in compartment id {created.id}']
    ]


#: An account this program does not declare into, in the form a stale record
#: presents it: everything the seed mints signs for one tenancy while
#: `conventions` names another.
ELSEWHERE = 'ocid1.tenancy.oc1..elsewhere'


#: The OCID the `physical` compartment is recorded against here: the test's
#: own, so no case depends on what the live entry records.
RECORDED_COMPARTMENT = 'ocid1.compartment.oc1..physical-recorded'


@pytest.fixture
def recorded_compartment(tenancy: Tenancy, monkeypatch: pytest.MonkeyPatch) -> conventions.Compartment:
    """The `physical` compartment recorded in `conventions`, and present in the fake.

    The state the installation is in once a consumer has been minted for: the
    OCID is committed, so a mint that names no compartment of its own adopts
    that one. A case about what happens after the compartment is settled does
    not have to say any of this.
    """
    recorded = with_recorded_compartment(monkeypatch, conventions.PHYSICAL, RECORDED_COMPARTMENT)
    tenancy.identity.hold(recorded)
    return recorded


@pytest.mark.usefixtures('recorded_compartment')
def test_the_mint_delivers_a_key_that_signs_for_the_account_conventions_records(
    oci_kit: KdbxStore, tenancy: Tenancy, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack

    user = derived.oci_physical(oci_kit, stack=slot, connect=tenancy)

    # The tenancy is proved rather than copied: it reaches no config key, and
    # the credential is delivered because the proof held.
    assert runner.config[derived.OCI_USER_KEY] == user


@pytest.mark.usefixtures('recorded_compartment')
def test_a_key_that_signs_for_another_account_is_refused_before_anything_is_created(
    oci_kit: KdbxStore,
    tenancy: Tenancy,
    physical_stack: tuple[pulumi_config.Stack, RecordedPulumi],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot, runner = physical_stack
    with_tenancy_ocid(monkeypatch, ELSEWHERE)

    # Both accounts are named, because which of the two is stale is the
    # operator's question and neither one alone answers it.
    users, compartments = dict(tenancy.identity.users), dict(tenancy.identity.compartments)

    with pytest.raises(oci_iam.CredentialRejected, match=f'{TENANCY}.*{ELSEWHERE}'):
        _ = derived.oci_physical(oci_kit, stack=slot, connect=tenancy)

    assert runner.config == {}
    # Nothing reaches the slot, and nothing is left in the tenancy either: a
    # user, a group, a policy and a live signing key made in a foreign account
    # by a run that then refused are permissions nobody knows to revoke.
    assert tenancy.identity.users == users
    assert tenancy.identity.compartments == compartments


def test_a_drill_tenancy_is_not_held_against_the_account_conventions_records(
    oci_kit: KdbxStore,
    tenancy: Tenancy,
    physical_stack: tuple[pulumi_config.Stack, RecordedPulumi],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot, runner = physical_stack
    with_tenancy_ocid(monkeypatch, ELSEWHERE)

    # A run that names its own compartment is pointed at a tenancy none of
    # these names describe, which is the escape `ensure_compartment` already
    # takes as given.
    user = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    assert runner.config[derived.OCI_USER_KEY] == user


def test_the_config_keys_the_mint_writes_are_the_ones_the_map_promises(
    oci_kit: KdbxStore, tenancy: Tenancy, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack

    _ = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    # §3's machine-readable half says where this row lands (`slots.py`), and a
    # push that fills a key the map does not name -- or leaves one it does --
    # is a register that has stopped describing the system.
    promised = {
        target.key
        for target in slot_map.ROWS[derived.OCI_PHYSICAL_ROW].targets
        if isinstance(target, slot_map.PulumiConfig)
    }
    assert set(runner.config) == promised


def test_the_recorded_compartment_is_adopted_not_recreated(
    oci_kit: KdbxStore,
    tenancy: Tenancy,
    physical_stack: tuple[pulumi_config.Stack, RecordedPulumi],
    recorded_compartment: conventions.Compartment,
) -> None:
    slot, _ = physical_stack

    _ = derived.oci_physical(oci_kit, stack=slot, connect=tenancy)

    # The post-record state: the OCID is written, so the mint must find that
    # compartment and act in it, creating nothing.
    name = f'{conventions.CLUSTER_NAME}-{derived.PHYSICAL_STACK}'
    assert list(tenancy.identity.compartments) == [recorded_compartment.ocid]
    assert [policy.statements for policy in tenancy.identity.policies.values() if policy.name == name] == [
        [f'Allow group {name} to manage all-resources in compartment id {recorded_compartment.ocid}']
    ]


def test_the_per_stack_identity_is_confined_to_the_compartment_it_names(
    oci_kit: KdbxStore, tenancy: Tenancy, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, _ = physical_stack

    user = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    # One name for the user, its group and its policy, and the policy is the
    # whole of what the key may do — a compartment rather than a verb list.
    name = f'{conventions.CLUSTER_NAME}-{derived.PHYSICAL_STACK}'
    group = next(candidate for candidate in tenancy.identity.groups.values() if candidate.name == name)
    assert (user, group.id) in tenancy.identity.memberships
    assert [policy.statements for policy in tenancy.identity.policies.values() if policy.name == name] == [
        [f'Allow group {name} to manage all-resources in compartment id {COMPARTMENT}']
    ]
    # The seed's own objects are untouched beside them: a per-stack mint adds
    # a principal, it does not widen the one that made it.
    assert sorted(user.name for user in tenancy.identity.users.values()) == [name, oci_iam.SEED_NAME]


def test_the_seed_mints_for_a_user_that_is_not_its_own(
    oci_kit: KdbxStore, tenancy: Tenancy, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, _ = physical_stack

    user = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    # The domain's administrative half needs domain-admin rights the seed does
    # not hold, so creating somebody else's user falls through to the legacy
    # shim — the bidirectional fallback §2 describes, on the path that needs it
    # most. The sweep afterwards runs as the minted key, whose self-service
    # endpoints authorize on authentication alone.
    assert 'CreateUser' in tenancy.identity.shim_calls
    assert 'list_my_api_keys' in tenancy.policy.served
    # …and the key that verified and swept is the minted one, signing as the
    # user it was minted for rather than as the seed.
    assert tenancy.made(KEY_LISTINGS)[-1].user == user


def test_the_minted_oci_key_never_touches_the_kit(
    oci_kit: KdbxStore, tenancy: Tenancy, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack

    _ = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    # Rule 2 again: the kit holds the seed it held before, and the key the
    # stack runs on exists in the slot and nowhere else.
    assert oci_kit.entries() == [derived.OCI_SEED_ENTRY]
    seed_pem = oci_kit.attachment(derived.OCI_SEED_ENTRY, entries.OCI_KEY_ATTACHMENT).decode()
    assert runner.config[derived.OCI_PRIVATE_KEY_KEY] != seed_pem


def test_a_re_run_rotates_the_key_and_reuses_the_identity(
    oci_kit: KdbxStore, tenancy: Tenancy, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack
    user = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)
    first = runner.config[derived.OCI_PRIVATE_KEY_KEY]

    again = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    # Rotating the row is re-running the command: the same principal, a new
    # key, and the predecessor retired once the successor is verified and the
    # slot has taken it — so a run that gets that far leaves the user holding
    # one key rather than accumulating toward `oci_iam.KEY_QUOTA`.
    assert again == user
    second = runner.config[derived.OCI_PRIVATE_KEY_KEY]
    assert second != first
    assert tenancy.identity.keys[user] == [oci_iam.fingerprint(second)]
    assert len(tenancy.identity.users) == len(tenancy.identity.groups) == len(tenancy.identity.policies) == 2


def test_an_oci_push_that_fails_leaves_the_key_the_stack_already_holds_live(
    oci_kit: KdbxStore, tenancy: Tenancy, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack
    user = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)
    delivered = oci_iam.fingerprint(runner.config[derived.OCI_PRIVATE_KEY_KEY])
    runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused):
        _ = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    # The private half of an OCI key is generated here and never returned by
    # the service, so between the mint and the push it exists in this process
    # alone. Swept first, this run would end with the `physical` stack holding
    # a key the tenancy has deleted; swept last, the stack's key is untouched
    # and the stray is what the next run clears.
    assert delivered in tenancy.identity.keys[user]
    assert len(tenancy.identity.keys[user]) == 2

    runner.corrupts = False
    _ = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    assert tenancy.identity.keys[user] == [oci_iam.fingerprint(runner.config[derived.OCI_PRIVATE_KEY_KEY])]


def test_pushes_that_keep_failing_are_refused_by_name_rather_than_filling_the_quota(
    oci_kit: KdbxStore, tenancy: Tenancy, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack
    user = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)
    runner.corrupts = True
    for _ in range(oci_iam.KEY_QUOTA - 1):
        with pytest.raises(pulumi_config.SlotRefused):
            _ = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    with pytest.raises(oci_iam.CredentialRejected, match='the quota'):
        _ = derived.oci_physical(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    # What a deferred retirement costs on the unhappy path is credentials of
    # this name accumulating at the provider, and OCI is where that has an end:
    # a user holds three API keys, so the run that would exceed it stops and
    # lists them instead. The stranded keys are named where an operator can act
    # on them rather than minted past in silence, and the one the stack holds
    # is still among them.
    assert len(tenancy.identity.keys[user]) == oci_iam.KEY_QUOTA
    assert oci_iam.fingerprint(runner.config[derived.OCI_PRIVATE_KEY_KEY]) in tenancy.identity.keys[user]


# -- the appliance's key, into the `state-backend` stack ----------------------


def test_the_appliance_key_lands_in_the_state_backend_stack(
    oci_kit: KdbxStore, tenancy: Tenancy, state_backend_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = state_backend_stack

    user = derived.oci_state_backend(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    # The three values the stack program builds its OCI provider from, each a
    # secret, the fingerprint the key's own; where the appliance may act is a
    # convention the program reads, so nothing of the account is written.
    assert user == _named(tenancy, f'{conventions.CLUSTER_NAME}-{conventions.STATE_BACKEND}')
    assert runner.config[derived.OCI_USER_KEY] == user
    key = runner.config[derived.OCI_PRIVATE_KEY_KEY]
    assert runner.config[derived.OCI_FINGERPRINT_KEY] == oci_iam.fingerprint(key)
    assert tenancy.identity.keys[user] == [oci_iam.fingerprint(key)]
    written = {derived.OCI_USER_KEY, derived.OCI_FINGERPRINT_KEY, derived.OCI_PRIVATE_KEY_KEY}
    assert set(runner.config) == written
    assert {args[2] for args in runner.invocations if args[:2] == ['config', 'set'] and '--secret' in args} == written


def test_the_appliance_row_is_refused_in_another_account_before_anything_is_created(
    oci_kit: KdbxStore,
    tenancy: Tenancy,
    state_backend_stack: tuple[pulumi_config.Stack, RecordedPulumi],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot, runner = state_backend_stack
    with_tenancy_ocid(monkeypatch, ELSEWHERE)
    # The state before the appliance's first mint, where a run that reached
    # the compartment step would create one, so the empty tenancy below tells
    # a refusal from a late one.
    _ = with_unrecorded_compartment(monkeypatch, conventions.STATE_BACKEND)
    users, policies = dict(tenancy.identity.users), dict(tenancy.identity.policies)

    # No compartment is named, so this is the ordinary path rather than the
    # drill: the check fires before the compartment is even looked up.
    with pytest.raises(oci_iam.CredentialRejected, match=f'{TENANCY}.*{ELSEWHERE}'):
        _ = derived.oci_state_backend(oci_kit, stack=slot, connect=tenancy)

    # The row whose key builds the appliance the whole installation's state
    # lives on.
    assert runner.config == {}
    assert tenancy.identity.users == users
    assert tenancy.identity.policies == policies
    assert tenancy.identity.compartments == {}


def test_the_appliance_row_is_its_own_principal(
    oci_kit: KdbxStore, tenancy: Tenancy, state_backend_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, _ = state_backend_stack
    _ = derived.oci_state_backend(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy)

    # Two §3 OCI rows, two principals: the appliance's stack and the physical
    # stack are separate consumers, so a compromise of either is confined to
    # its own compartment.
    name = f'{conventions.CLUSTER_NAME}-{conventions.STATE_BACKEND}'
    assert sorted(user.name for user in tenancy.identity.users.values()) == [oci_iam.SEED_NAME, name]
    assert [policy.statements for policy in tenancy.identity.policies.values() if policy.name == name] == [
        [f'Allow group {name} to manage all-resources in compartment id {COMPARTMENT}']
    ]


# -- the B2 management key --------------------------------------------------


@pytest.fixture
def b2_api_fake(monkeypatch: pytest.MonkeyPatch) -> b2_api.FakeApi:
    fake = b2_api.FakeApi()
    monkeypatch.setattr(b2.requests, 'get', fake.get)
    monkeypatch.setattr(b2.requests, 'post', fake.post)
    # Every B2 mint proves the account before it writes, so the fake platform
    # has to be the account `conventions` records for the ordinary path to be
    # the one under test.
    monkeypatch.setattr(
        conventions,
        'B2_ACCOUNT',
        conventions.B2Account(region=conventions.B2_ACCOUNT.region, account_id=b2_api.ACCOUNT_ID),
    )
    return fake


@pytest.fixture
def b2_kit(b2_api_fake: b2_api.FakeApi) -> KdbxStore:
    store = MemoryKit()
    root = masters.Credential(
        root=masters.ROOTS['b2'],
        values={'account-id': b2_api_fake.master.key_id, 'key': b2_api_fake.master.secret},
    )
    _ = b2.create_seed(root=root, seeds=store, seed_entry=derived.B2_SEED_ENTRY)
    return store


def test_the_management_key_lands_in_the_stack_config_with_its_id(
    b2_api_fake: b2_api.FakeApi, b2_kit: KdbxStore, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack

    key_id = derived.b2_management(b2_kit, stack=slot)

    # Both halves, because a B2 credential is a pair: the id names the key and
    # the key is the secret, and neither authenticates on its own.
    assert runner.config[derived.B2_KEY_ID_KEY] == key_id
    assert b2_api_fake.keys[key_id].secret == runner.config[derived.B2_KEY_KEY]
    # Bucket, key and lifecycle administration, and no file capability at all:
    # what manages the backup buckets cannot read a byte out of them.
    assert b2_api_fake.keys[key_id].capabilities == b2.CAPABILITIES


def test_the_management_key_never_touches_the_kit(
    b2_kit: KdbxStore, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, _ = physical_stack

    _ = derived.b2_management(b2_kit, stack=slot)

    assert b2_kit.entries() == [derived.B2_SEED_ENTRY]


def test_a_management_push_that_fails_leaves_the_key_the_stack_already_holds_live(
    b2_api_fake: b2_api.FakeApi, b2_kit: KdbxStore, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack
    delivered = derived.b2_management(b2_kit, stack=slot)
    runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused):
        _ = derived.b2_management(b2_kit, stack=slot)

    # B2 discloses a key's secret once, at creation, so between the mint and
    # the push the successor exists in this process alone. Retired first, this
    # run would end with the `physical` stack naming a key the account has
    # deleted; retired last, the pair the stack holds still authenticates.
    assert delivered in b2_api_fake.named(b2.MANAGEMENT.name)
    assert len(b2_api_fake.named(b2.MANAGEMENT.name)) == 2

    runner.corrupts = False
    healed = derived.b2_management(b2_kit, stack=slot)

    # Retirement matches on the key's name rather than on a recorded
    # predecessor, so one successful run clears every stray a failed one left.
    assert b2_api_fake.named(b2.MANAGEMENT.name) == [healed]


def test_a_re_run_rotates_the_management_key(
    b2_api_fake: b2_api.FakeApi, b2_kit: KdbxStore, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = physical_stack
    first = derived.b2_management(b2_kit, stack=slot)

    second = derived.b2_management(b2_kit, stack=slot)

    # One live key of that name afterwards, and the slot names it: the seed
    # signs the retirement and survives it, so the predecessor really goes.
    assert second != first
    assert b2_api_fake.named(b2.MANAGEMENT.name) == [second]
    assert runner.config[derived.B2_KEY_ID_KEY] == second


# -- the `state-backend` stack's configuration -------------------------------


def _state_backend_stack(checkout: Path, *, initialized: bool = True) -> tuple[pulumi_config.Stack, RecordedPulumi]:
    """The `state-backend` stack over `checkout`, with its first checkpoint laid out unless `initialized` is false."""
    if initialized:
        path = checkout / stack_environment.CHECKPOINTS / '.pulumi' / 'stacks' / 'kluster-py'
        path.mkdir(parents=True)
        _ = (path / f'{derived.STATE_BACKEND_STACK}.json').write_text('{}')
    runner = RecordedPulumi(stacks=[derived.STATE_BACKEND_STACK])
    environment = pulumi_config.BackendEnvironment(operator=lambda: 'an-operator-passphrase')
    return pulumi_config.Stack(
        name=derived.STATE_BACKEND_STACK, directory=checkout, environment=environment, run=runner
    ), runner


@pytest.fixture
def state_backend_stack(tmp_path: Path) -> tuple[pulumi_config.Stack, RecordedPulumi]:
    return _state_backend_stack(tmp_path)


def test_the_appliance_management_key_lands_in_its_stack_under_a_role_of_its_own(
    b2_api_fake: b2_api.FakeApi,
    b2_kit: KdbxStore,
    physical_stack: tuple[pulumi_config.Stack, RecordedPulumi],
    state_backend_stack: tuple[pulumi_config.Stack, RecordedPulumi],
) -> None:
    physical, _ = physical_stack
    slot, runner = state_backend_stack
    physicals = derived.b2_management(b2_kit, stack=physical)

    key_id = derived.b2_state_backend_management(b2_kit, stack=slot)

    assert runner.config[derived.B2_KEY_ID_KEY] == key_id
    assert b2_api_fake.keys[key_id].secret == runner.config[derived.B2_KEY_KEY]
    assert b2_api_fake.keys[key_id].capabilities == b2.CAPABILITIES
    # A name of its own, so this mint's retirement leaves the key the
    # `physical` stack holds live, and that stack's re-run leaves this one.
    assert b2_api_fake.named(b2.STATE_BACKEND_MANAGEMENT.name) == [key_id]
    assert b2_api_fake.named(b2.MANAGEMENT.name) == [physicals]
    _ = derived.b2_management(b2_kit, stack=physical)
    assert b2_api_fake.named(b2.STATE_BACKEND_MANAGEMENT.name) == [key_id]


def test_a_re_run_rotates_the_appliance_management_key(
    b2_api_fake: b2_api.FakeApi, b2_kit: KdbxStore, state_backend_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = state_backend_stack
    first = derived.b2_state_backend_management(b2_kit, stack=slot)

    second = derived.b2_state_backend_management(b2_kit, stack=slot)

    assert second != first
    assert b2_api_fake.named(b2.STATE_BACKEND_MANAGEMENT.name) == [second]
    assert runner.config[derived.B2_KEY_ID_KEY] == second


@needs_age
def test_the_state_backend_rows_refuse_a_stack_with_no_checkpoint_before_anything_is_minted(
    b2_api_fake: b2_api.FakeApi,
    b2_kit: KdbxStore,
    oci_kit: KdbxStore,
    tenancy: Tenancy,
    vault_in_hand: escrow.Vault,
    tmp_path: Path,
) -> None:
    # The stack's first `stack init` is the driver's, which checks the working
    # copy and leaves the checkpoint for the operator to land; a delivery that
    # created it would skip both.
    slot, runner = _state_backend_stack(tmp_path, initialized=False)
    _ = escrow.generate(vault_in_hand, escrow.CA)
    public_file = tmp_path / 'host-key.txt'
    users = dict(tenancy.identity.users)

    for deliver in (
        lambda: derived.oci_state_backend(oci_kit, stack=slot, compartment_id=COMPARTMENT, connect=tenancy),
        lambda: derived.b2_state_backend_management(b2_kit, stack=slot),
        lambda: derived.state_backend_server(vault_in_hand, stack=slot),
        lambda: derived.state_backend_host_key(stack=slot, public_file=public_file),
    ):
        with pytest.raises(
            pulumi_config.SlotRefused, match=f'operator-stack {derived.STATE_BACKEND_STACK} pulumi stack init'
        ):
            deliver()

    assert b2_api_fake.named(b2.STATE_BACKEND_MANAGEMENT.name) == []
    assert tenancy.identity.users == users
    assert runner.invocations == []
    assert not public_file.exists()


def test_a_value_in_the_clear_that_opens_with_dashes_reaches_the_config(
    state_backend_stack: tuple[pulumi_config.Stack, RecordedPulumi],
) -> None:
    # A certificate opens with `-----BEGIN`, which the CLI reads as a flag
    # when it is an argument; standard input is the one way it lands.
    slot, runner = state_backend_stack
    certificate = '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----'

    slot.set(derived.CA_CERT_KEY, certificate)

    assert runner.config[derived.CA_CERT_KEY] == certificate


@needs_age
def test_the_server_certificate_chains_to_the_escrowed_ca_and_names_the_address(
    state_backend_stack: tuple[pulumi_config.Stack, RecordedPulumi], vault_in_hand: escrow.Vault
) -> None:
    slot, runner = state_backend_stack
    authority = pki.Authority.from_pem(escrow.generate(vault_in_hand, escrow.CA))

    issued = derived.state_backend_server(vault_in_hand, stack=slot)

    # The key a secret, the two certificates in the clear, and the three one
    # issuance: the key opens the certificate the config holds.
    secret_keys = {args[2] for args in runner.invocations if args[:2] == ['config', 'set'] and '--secret' in args}
    assert secret_keys == {derived.SERVER_KEY_KEY}
    assert runner.config[derived.SERVER_KEY_KEY] == issued.key_pem.decode().strip()
    certificate = x509.load_pem_x509_certificate(runner.config[derived.SERVER_CERT_KEY].encode())
    ca = x509.load_pem_x509_certificate(runner.config[derived.CA_CERT_KEY].encode())
    key = serialization.load_pem_private_key(runner.config[derived.SERVER_KEY_KEY].encode(), password=None)
    assert key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) == certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    certificate.verify_directly_issued_by(ca)
    assert ca.public_key() == authority.key.public_key()
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address(appliance_settings.ADDRESS)]


def test_the_committed_public_half_is_the_configured_host_key(
    state_backend_stack: tuple[pulumi_config.Stack, RecordedPulumi], tmp_path: Path
) -> None:
    slot, runner = state_backend_stack
    public_file = tmp_path / 'host-key.txt'

    public = derived.state_backend_host_key(stack=slot, public_file=public_file)

    configured = serialization.load_ssh_private_key(runner.config[derived.HOST_KEY_KEY].encode(), password=None)
    assert isinstance(configured, Ed25519PrivateKey)
    derived_public = (
        configured.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    (on_file,) = lib_config.lines(public_file, 'the host key')
    assert on_file == derived_public == public
    assert public.startswith('ssh-ed25519 ')


def test_a_host_key_the_config_refused_leaves_no_public_half(
    state_backend_stack: tuple[pulumi_config.Stack, RecordedPulumi], tmp_path: Path
) -> None:
    # Config before file: a file naming a key the configuration does not hold
    # would pin a box nobody can reach.
    slot, runner = state_backend_stack
    runner.corrupts = True
    public_file = tmp_path / 'host-key.txt'

    with pytest.raises(pulumi_config.SlotRefused):
        _ = derived.state_backend_host_key(stack=slot, public_file=public_file)

    assert not public_file.exists()


# -- the backup recipients file ---------------------------------------------


@pytest.fixture
def vault_in_hand(tmp_path: Path) -> escrow.Vault:
    kit = MemoryKit()
    registry = escrow.Registry.open(tmp_path / 'escrow')
    _ = escrow.init(kit, registry)
    return escrow.Vault.open(kit, registry)


@needs_age
def test_generate_writes_the_recipient_of_the_escrowed_identity(vault_in_hand: escrow.Vault, tmp_path: Path) -> None:
    (label, *_) = escrow.backup_labels()
    recipients_file = tmp_path / 'backup-recipients.txt'

    drawn = derived.backup_age_recipient(vault_in_hand, label, recipients_file=recipients_file)

    assert vault_in_hand.registry.generations(label) == [1]
    assert drawn == age.recipient(vault_in_hand.recover(label))
    assert derived.backup_recipients(recipients_file) == {label: drawn}
    assert derived.backup_recipients_problems(vault_in_hand.registry, recipients_file) == []


@needs_age
def test_generate_over_an_escrowed_generation_writes_its_recipient_and_draws_nothing(
    vault_in_hand: escrow.Vault, tmp_path: Path
) -> None:
    # The generations escrowed before the file existed: the label holds one
    # identity for its lifetime, so the recipient is recovered, not drawn.
    (label, *_) = escrow.backup_labels()
    escrowed = escrow.generate(vault_in_hand, label)
    recipients_file = tmp_path / 'backup-recipients.txt'

    written = derived.backup_age_recipient(vault_in_hand, label, recipients_file=recipients_file)

    assert vault_in_hand.registry.generations(label) == [1]
    assert written == age.recipient(escrowed)
    assert derived.backup_recipients(recipients_file) == {label: written}


@needs_age
def test_generate_replaces_a_line_that_disagrees_with_the_escrow(vault_in_hand: escrow.Vault, tmp_path: Path) -> None:
    (label, *_) = escrow.backup_labels()
    recipients_file = tmp_path / 'backup-recipients.txt'
    _ = recipients_file.write_text(f'{label} {age.generate().public}\n')

    written = derived.backup_age_recipient(vault_in_hand, label, recipients_file=recipients_file)

    assert derived.backup_recipients(recipients_file) == {label: written}
    assert written == age.recipient(vault_in_hand.recover(label))


#: Recipients files that differ from an escrow holding the window's first
#: generation, as templates: `{label}` is that generation, `{other}` one the
#: escrow does not hold, `{a}` and `{b}` two recipients.
DIFFERING = {
    'missing-generation': ('{other} {a}\n', 'names no recipient for'),
    'generation-not-escrowed': ('{label} {a}\n{other} {b}\n', 'which the escrow does not hold'),
    'duplicate': ('{label} {a}\n{label} {b}\n', 'a second time'),
    'no-recipient': ('{label}\n', 'line 1 of'),
    'not-a-recipient': ('{label} age1notarecipient\n', 'is not an age recipient'),
}


@needs_age
@pytest.mark.parametrize(('template', 'named'), DIFFERING.values(), ids=DIFFERING.keys())
def test_check_refuses_a_recipients_file_that_differs_from_the_escrow(
    vault_in_hand: escrow.Vault, tmp_path: Path, template: str, named: str
) -> None:
    (label, *_) = escrow.backup_labels()
    _ = escrow.generate(vault_in_hand, label)
    other = f'{escrow.BACKUP}/{appliance_settings.AGE_GENERATION + 1}'
    recipients_file = tmp_path / 'backup-recipients.txt'
    _ = recipients_file.write_text(
        template.format(label=label, other=other, a=age.generate().public, b=age.generate().public)
    )

    problems = derived.backup_recipients_problems(vault_in_hand.registry, recipients_file)

    assert problems
    assert any(named in problem for problem in problems), problems


@needs_age
def test_check_never_prints_a_private_key_pasted_into_the_recipients_file(
    vault_in_hand: escrow.Vault, tmp_path: Path
) -> None:
    (label, *_) = escrow.backup_labels()
    secret = age.generate().secret
    recipients_file = tmp_path / 'backup-recipients.txt'
    _ = recipients_file.write_text(f'{label} {secret}\n')

    problems = derived.backup_recipients_problems(vault_in_hand.registry, recipients_file)

    assert problems
    assert all(secret.removeprefix(age.SECRET_PREFIX) not in problem for problem in problems)


def test_check_refuses_while_no_recipients_file_exists(vault_in_hand: escrow.Vault, tmp_path: Path) -> None:
    # The stack renders the box's recipients from the file and refuses to plan
    # without it, so its absence is a problem the check names, with the
    # command that writes it.
    (problem,) = derived.backup_recipients_problems(vault_in_hand.registry, tmp_path / 'backup-recipients.txt')

    assert 'backup-recipients.txt' in problem
    assert f'credentials derived {escrow.row_name(escrow.backup_labels()[0])} generate' in problem


def test_check_refuses_while_no_host_key_file_exists(tmp_path: Path) -> None:
    # The stack renders the box against the committed public half, and `ssh`
    # pins the box to it.
    (problem,) = derived.host_key_problems(tmp_path / 'host-key.txt')

    assert 'host-key.txt' in problem
    assert f'credentials derived {derived.STATE_BACKEND_HOST_KEY_ROW} generate' in problem


def test_check_reads_a_committed_host_key_as_one_ed25519_line(tmp_path: Path) -> None:
    path = tmp_path / 'host-key.txt'
    _ = path.write_text('ssh-rsa AAAAB3NzaC1yc2E= someone\n')
    assert derived.host_key_problems(path) != []

    public = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    )
    _ = path.write_text(public.decode() + '\n')
    assert derived.host_key_problems(path) == []


def test_a_value_in_the_clear_that_does_not_read_back_is_refused(tmp_path: Path) -> None:
    # The read-back is the whole proof a plain value landed, as it is for a
    # secret: the file changes either way.
    slot, runner = _state_backend_stack(tmp_path)
    runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused, match='does not read back'):
        slot.set(derived.CA_CERT_KEY, '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----')


def test_a_file_to_commit_outside_the_checkout_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An installed package resolves the target inside that installation,
    # where no commit picks the file up.
    monkeypatch.setattr(lib_workstation, 'repo_root', lambda: tmp_path / 'checkout')

    with pytest.raises(lib_workstation.WorkstationError, match='is not in the checkout'):
        _ = derived.committed_target(tmp_path / 'installed' / 'host-key.txt')

    inside = tmp_path / 'checkout' / 'host-key.txt'
    assert derived.committed_target(inside) == inside


@needs_age
def test_generate_drops_a_generation_outside_the_window(vault_in_hand: escrow.Vault, tmp_path: Path) -> None:
    (label, *_) = escrow.backup_labels()
    outside = f'{escrow.BACKUP}/{appliance_settings.AGE_GENERATION + 1}'
    recipients_file = tmp_path / 'backup-recipients.txt'
    _ = recipients_file.write_text(f'{outside} {age.generate().public}\n')

    written = derived.backup_age_recipient(vault_in_hand, label, recipients_file=recipients_file)

    assert derived.backup_recipients(recipients_file) == {label: written}


@needs_age
def test_a_recipients_file_that_refuses_stops_generate_before_anything_is_drawn(
    vault_in_hand: escrow.Vault, tmp_path: Path
) -> None:
    (label, *_) = escrow.backup_labels()
    recipients_file = tmp_path / 'backup-recipients.txt'
    _ = recipients_file.write_text(f'{label}\n')

    with pytest.raises(age.AgeError, match='line 1 of'):
        _ = derived.backup_age_recipient(vault_in_hand, label, recipients_file=recipients_file)

    assert vault_in_hand.registry.generations(label) == []
