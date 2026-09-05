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

The OCI tenancy is the fake `test_oci_iam` drives, imported rather than
rebuilt: one fake per platform, and the module that owns the API is where it
lives. What it encodes matters here — the identity domain serves the
self-service endpoints to anyone who authenticates and the administrative ones
only to a domain administrator, which the seed is not, so a mint for somebody
else's user is served by the legacy shim.
"""

# The SDK ships no stubs; the same waiver `oci_iam.py` itself carries.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import shutil
from pathlib import Path

import b2_api
import oci
import pytest
from cloudflare_api import ACCOUNT_ID, FakeApi, console_seed
from fake_pulumi import RecordedPulumi
from memory_kit import MemoryKit
from test_oci_iam import ROOT_USER, TENANCY, Named, Tenancy

from oci_conventions import with_compartment, with_tenancy_ocid
from kluster import conventions
from kluster.scripts.credentials import (
    b2,
    cloudflare,
    derived,
    entries,
    masters,
    oci_iam,
    oci_slot,
    pulumi_config,
    workstation,
)

# Aliased: `slots` is the name a fixture below gives the workstation's
# `.credentials/` directory, and the map is a different thing entirely.
from kluster.scripts.credentials import slots as slot_map
from kluster.scripts.credentials.kdbx import KdbxStore

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

    derived.cloudflare_zones(kit, stack=slot)

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
        derived.cloudflare_zones(kit, stack=slot)

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

    derived.cloudflare_zones(kit, stack=slot)

    # A workstation that has never selected this stack is the ordinary case at
    # bring-up, so the push cannot assume one exists.
    assert runner.stacks == [STACK]


def test_the_minted_token_never_touches_the_kit(
    kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack

    derived.cloudflare_zones(kit, stack=slot)

    # Rule 2: the offline store is not a staging area. The kit holds the seed
    # it held before, and the minted value exists only in the slot.
    assert kit.entries() == [derived.CLOUDFLARE_SEED_ENTRY]
    assert runner.config[derived.API_TOKEN_KEY] != kit.get(derived.CLOUDFLARE_SEED_ENTRY)


def test_a_re_run_rotates_the_row_and_leaves_one_live_token(
    api: FakeApi, kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack
    derived.cloudflare_zones(kit, stack=slot)
    first = runner.config[derived.API_TOKEN_KEY]

    derived.cloudflare_zones(kit, stack=slot)

    # Rotation is a re-run, not a second procedure: the predecessor is retired
    # once its successor is verified and the slot has taken it, and the slot
    # names the survivor.
    second = runner.config[derived.API_TOKEN_KEY]
    assert second != first
    assert _live(api) == [api.values[second]]


@pytest.fixture
def live_project(tmp_path: Path) -> tuple[pulumi_config.Stack, str]:
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
    stack = pulumi_config.Stack(
        name=STACK,
        directory=project,
        env={
            'PULUMI_HOME': str(tmp_path / 'home'),
            'PULUMI_BACKEND_URL': state.as_uri(),
            'PULUMI_CONFIG_PASSPHRASE': 'probe-passphrase',
            'PULUMI_SKIP_UPDATE_CHECK': 'true',
        },
    )
    return stack, name


def test_the_token_lands_where_the_program_reads_it(
    kit: KdbxStore, live_project: tuple[pulumi_config.Stack, str]
) -> None:
    slot, project = live_project

    derived.cloudflare_zones(kit, stack=slot)

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
    derived.cloudflare_zones(kit, stack=slot)
    (predecessor,) = _live(api)
    runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused):
        derived.cloudflare_zones(kit, stack=slot)

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
    derived.cloudflare_zones(kit, stack=slot)
    assert _live(api) == [api.values[runner.config[derived.API_TOKEN_KEY]]]


def test_a_push_that_fails_is_healed_by_running_it_again(
    api: FakeApi, kit: KdbxStore, stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, runner = stack
    runner.corrupts = True

    with pytest.raises(pulumi_config.SlotRefused):
        derived.cloudflare_zones(kit, stack=slot)

    # The interrupted run left a live token nobody holds; the re-run mints its
    # successor, retires it, and fills the slot, which is why a failed stage is
    # re-run rather than repaired by hand.
    runner.corrupts = False
    derived.cloudflare_zones(kit, stack=slot)
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
    derived.cloudflare_zones(kit, stack=zones_slot)

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


@pytest.fixture
def physical_stack() -> tuple[pulumi_config.Stack, RecordedPulumi]:
    runner = RecordedPulumi()
    return (
        pulumi_config.Stack(name=derived.PHYSICAL_STACK, directory=pulumi_config.project_dir(), run=runner),
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
    intended = conventions.OCI_TENANCY.compartments[conventions.PHYSICAL]
    with_compartment(monkeypatch, conventions.Compartment(consumer=intended.consumer, name=intended.name))

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


@pytest.fixture
def recorded_compartment(tenancy: Tenancy) -> None:
    """The `physical` compartment as `conventions` records it, present in the fake.

    The state the installation is in once a consumer has been minted for: the
    OCID is committed, so a mint that names no compartment of its own adopts
    that one. A case about what happens after the compartment is settled does
    not have to say any of this.
    """
    intended = conventions.OCI_TENANCY.compartments[conventions.PHYSICAL]
    assert intended.ocid is not None
    tenancy.identity.compartments[intended.ocid] = Named(id=intended.ocid, name=intended.name)


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
    oci_kit: KdbxStore, tenancy: Tenancy, physical_stack: tuple[pulumi_config.Stack, RecordedPulumi]
) -> None:
    slot, _ = physical_stack
    # The post-record state `conventions` carries today: the OCID is written,
    # so the mint must find that compartment and act in it, creating nothing.
    intended = conventions.OCI_TENANCY.compartments[conventions.PHYSICAL]
    assert intended.ocid is not None
    tenancy.identity.compartments[intended.ocid] = Named(id=intended.ocid, name=intended.name)

    _ = derived.oci_physical(oci_kit, stack=slot, connect=tenancy)

    name = f'{conventions.CLUSTER_NAME}-{derived.PHYSICAL_STACK}'
    assert list(tenancy.identity.compartments) == [intended.ocid]
    assert [policy.statements for policy in tenancy.identity.policies.values() if policy.name == name] == [
        [f'Allow group {name} to manage all-resources in compartment id {intended.ocid}']
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
    assert tenancy.domain_connections[-1][1] == user


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
    # one key rather than accumulating towards `oci_iam.KEY_QUOTA`.
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


# -- the appliance's key, which is a workstation slot rather than a stack ----


@pytest.fixture
def slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`.credentials/` somewhere that is not this checkout.

    The slots are repo-relative by design (§4.4), and a suite that wrote into
    the checkout it runs from would leave a placeholder credential where a
    real one belongs.
    """
    directory = tmp_path / '.credentials'
    monkeypatch.setattr(workstation, 'directory', lambda: directory)
    return directory


def test_the_appliance_key_lands_in_a_configuration_the_sdk_reads(
    oci_kit: KdbxStore, tenancy: Tenancy, slots: Path
) -> None:
    written = derived.oci_state_backend(oci_kit, compartment_id=COMPARTMENT, connect=tenancy)

    # The slot is an SDK configuration file because the SDK is the whole of the
    # reader: what proves the push is that `from_file` accepts what it wrote.
    assert written == slots / oci_slot.DIRECTORY / conventions.STATE_BACKEND / oci_slot.CONFIG
    config = oci.config.from_file(str(written))
    oci.config.validate_config(config)  # pyright: ignore[reportUnknownMemberType]
    user = _named(tenancy, f'{conventions.CLUSTER_NAME}-{conventions.STATE_BACKEND}')
    assert (config['tenancy'], config['user'], config['region']) == (TENANCY, user, conventions.OCI_TENANCY.region)
    # The credential and nothing else: where the appliance may act is a
    # convention its provisioner reads, so a copy here could only go stale.
    assert 'compartment-id' not in config


def test_the_appliance_row_is_refused_in_another_account_before_anything_is_created(
    oci_kit: KdbxStore, tenancy: Tenancy, slots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_tenancy_ocid(monkeypatch, ELSEWHERE)
    users, policies = dict(tenancy.identity.users), dict(tenancy.identity.policies)

    # No compartment is named, so this is the ordinary path rather than the
    # drill: the check fires before the compartment is even looked up.
    with pytest.raises(oci_iam.CredentialRejected, match=f'{TENANCY}.*{ELSEWHERE}'):
        _ = derived.oci_state_backend(oci_kit, connect=tenancy)

    # The first place in a bring-up where the check can fire, and the row whose
    # key builds the appliance the whole installation's state lives on.
    assert not slots.exists()
    assert tenancy.identity.users == users
    assert tenancy.identity.policies == policies
    assert tenancy.identity.compartments == {}


def test_the_appliance_key_is_a_file_only_its_owner_can_read(oci_kit: KdbxStore, tenancy: Tenancy, slots: Path) -> None:
    written = derived.oci_state_backend(oci_kit, compartment_id=COMPARTMENT, connect=tenancy)

    # Named absolutely, because the SDK expands nothing (§4.4), and `0600`
    # under a `0700` directory like every other slot.
    config = oci.config.from_file(str(written))
    key = Path(str(config['key_file']))
    assert key.is_absolute()
    assert oci_iam.fingerprint(key.read_text()) == config['fingerprint']
    assert key.stat().st_mode & 0o777 == 0o600
    assert key.parent.stat().st_mode & 0o777 == 0o700


def test_a_checkout_path_with_a_percent_in_it_is_still_written(
    oci_kit: KdbxStore, tenancy: Tenancy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The slot is a configuration file, and a configuration parser reads `%` as
    # the start of a substitution unless told otherwise. Nothing chooses the
    # path this file sits at, so a checkout under one would fail the write --
    # after the key it describes is already live in the tenancy.
    awkward = tmp_path / 'build%20one' / '.credentials'
    monkeypatch.setattr(workstation, 'directory', lambda: awkward)

    written = derived.oci_state_backend(oci_kit, compartment_id=COMPARTMENT, connect=tenancy)

    key = Path(str(oci.config.from_file(str(written))['key_file']))
    assert key.read_text().startswith('-----BEGIN PRIVATE KEY-----')


def test_the_appliance_row_is_its_own_principal(oci_kit: KdbxStore, tenancy: Tenancy, slots: Path) -> None:
    _ = derived.oci_state_backend(oci_kit, compartment_id=COMPARTMENT, connect=tenancy)

    # Two §3 OCI rows, two principals: the appliance provisioner and the
    # physical stack are separate consumers, so a compromise of either is
    # confined to its own compartment.
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
