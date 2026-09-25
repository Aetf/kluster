"""The drill credentials: two mints, five carriers, one Environment.

Against the fake tenancy in `oci_tenancy`, holding the drill compartment
`conventions` is made to record, the fake B2 account in `b2_api`, file
listing included, and a `gh` that runs nothing and remembers what it was
handed, because what is under test is the composite:
that the OCI key confined to the drill compartment and the B2 key confined to
the dump prefix both land, as the carriers the register names, in the ops
repository's `drill` Environment and nowhere else -- and that each half keeps
the order every mint here keeps, push then retire, independently of the other.

The exposure bounds the register states are held here as facts about what
was minted: the B2 key's grant is the read-only role on the writer's prefix,
the OCI policy is one statement over the drill compartment, and no argument
of the mint can widen either.
"""

# The SDK ships no stubs; the same waiver `oci_iam.py` itself carries.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
import requests
from fake_gh import RecordedGh
from memory_kit import MemoryKit
from b2_api import FakeApi as B2Api
from oci_tenancy import ROOT_USER, TENANCY, Tenancy

from oci_conventions import with_recorded_compartment, with_tenancy_ocid, with_unrecorded_compartment
from kluster import conventions
from kluster.scripts.credentials import b2, derived, masters, oci_iam
from kluster.scripts.credentials.github_secrets import Forge, Slot
from kluster.scripts.credentials.kdbx import KdbxStore
from kluster.scripts.credentials.masters import CredentialRejected
from kluster.scripts.credentials.pulumi_config import SlotRefused
from kluster.scripts.state_backend import settings as appliance_settings

OPS_REPOSITORY = conventions.forge.OPS.full_name
DRILL_ENVIRONMENT = conventions.forge.DRILL.name
DRILL_NAME = f'{conventions.CLUSTER_NAME}-{conventions.DRILL}'
ELSEWHERE = 'ocid1.tenancy.oc1..elsewhere'
#: The OCID the drill compartment is recorded against here: the test's own,
#: so no case depends on whether the live entry has one yet.
DRILL_COMPARTMENT = 'ocid1.compartment.oc1..drill-recorded'

#: Every event both fakes see, in the order the mint caused them: `('gh',
#: <secret name>)` for a push, `('b2', <api>)` for a B2 call. The order of
#: a push against the retirement it precedes is a property of the composite,
#: and the two fakes have no clock of their own.
Timeline = list[tuple[str, str]]


@pytest.fixture
def timeline() -> Timeline:
    return []


@dataclass
class TimedGh:
    """The recorded `gh`, stamping every push on the timeline and refusing the ones a test names.

    A refusal is what `run_gh` raises when `gh secret set` exits non-zero --
    a token without the scope, an Environment that is not there -- and it is
    the failure a rotation can meet: the name is already listed from the run
    before, so a push that silently dropped would read as landed, and the
    failure worth modelling is the one the tool reports.
    """

    gh: RecordedGh
    timeline: Timeline
    #: The secret names whose push fails.
    refuses: set[str] = field(default_factory=set[str])

    def __call__(self, args: Sequence[str], *, token: str, stdin: str | None) -> str:
        if list(args[:2]) == ['secret', 'set']:
            self.timeline.append(('gh', args[2]))
            if args[2] in self.refuses:
                raise SlotRefused(f'`gh secret set {args[2]}` failed: HTTP 403')
        return self.gh(args, token=token, stdin=stdin)


@pytest.fixture
def gh() -> RecordedGh:
    return RecordedGh()


@pytest.fixture
def sink(gh: RecordedGh, timeline: Timeline) -> TimedGh:
    return TimedGh(gh, timeline)


@pytest.fixture
def forge(sink: TimedGh) -> Forge:
    return Forge(token='admin-token', run=sink)


@pytest.fixture
def tenancy(monkeypatch: pytest.MonkeyPatch) -> Tenancy:
    """The fake account, `conventions` recording it and its drill compartment, and the compartment in it.

    The state the installation is in once the drill has been minted for: the
    mint adopts the recorded compartment and creates none. The path that
    creates it and announces the OCID is `test_oci_iam`'s.
    """
    with_tenancy_ocid(monkeypatch, TENANCY)
    fake = Tenancy()
    fake.identity.hold(with_recorded_compartment(monkeypatch, conventions.DRILL, DRILL_COMPARTMENT))
    return fake


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, timeline: Timeline) -> B2Api:
    fake = B2Api()

    def timed_post(url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: int) -> requests.Response:
        timeline.append(('b2', url.rsplit('/', 1)[-1]))
        return fake.post(url, json=json, headers=headers, timeout=timeout)

    monkeypatch.setattr(b2.requests, 'get', fake.get)
    monkeypatch.setattr(b2.requests, 'post', timed_post)
    monkeypatch.setattr(
        conventions,
        'B2_ACCOUNT',
        conventions.B2Account(region=conventions.B2_ACCOUNT.region, account_id=fake.master.key_id),
    )
    return fake


@pytest.fixture
def kit(tenancy: Tenancy, api: B2Api) -> KdbxStore:
    """A kit holding both seeds, created the way a bring-up creates them."""
    store = MemoryKit()
    oci_root = masters.Credential(
        root=masters.ROOTS['oci'],
        values={'tenancy': TENANCY, 'user': ROOT_USER, 'private-key': oci_iam.generate_key().private_pem},
    )
    _ = oci_iam.create_seed(root=oci_root, seeds=store, seed_entry=derived.OCI_SEED_ENTRY, connect=tenancy)
    b2_root = masters.Credential(
        root=masters.ROOTS['b2'], values={'account-id': api.master.key_id, 'key': api.master.secret}
    )
    _ = b2.create_seed(root=b2_root, seeds=store, seed_entry=derived.B2_SEED_ENTRY)
    return store


@pytest.fixture
def bucket_id(kit: KdbxStore) -> str:
    """The dump bucket, converged the way `state-backend provision` converges it."""
    session = b2.Session.from_entry(kit, derived.B2_SEED_ENTRY)
    return b2.ensure_bucket(
        session,
        appliance_settings.B2_BUCKET,
        prefix=appliance_settings.B2_PREFIX,
        retention_days=appliance_settings.B2_RETENTION_DAYS,
    )


def _mint(kit: KdbxStore, forge: Forge, tenancy: Tenancy, *, only: str | None = None) -> dict[str, str]:
    return derived.drill_credentials(kit, forge, only=only, connect=tenancy)


def _pushed(gh: RecordedGh, name: str) -> str:
    return gh.values[(OPS_REPOSITORY, DRILL_ENVIRONMENT, name)]


def _drill_user(tenancy: Tenancy) -> str:
    return next(user.id for user in tenancy.identity.users.values() if user.name == DRILL_NAME)


def test_the_five_carriers_land_in_the_drill_environment_and_nothing_else_does(
    kit: KdbxStore, forge: Forge, gh: RecordedGh, tenancy: Tenancy, bucket_id: str
) -> None:
    _ = _mint(kit, forge, tenancy)

    # The addresses are the slot map's own, as the register advertises them:
    # every carrier an Environment secret of the ops repository's `drill`, none
    # a repository secret, none in the deployment repository.
    assert list(gh.values) == [
        (OPS_REPOSITORY, DRILL_ENVIRONMENT, slot.name) for slot in (*derived.DRILL_OCI_SLOTS, *derived.DRILL_B2_SLOTS)
    ]
    assert all(
        ['secret', 'set', name, '--repo', OPS_REPOSITORY, '--env', DRILL_ENVIRONMENT] in gh.invocations
        for _, _, name in gh.values
    )


def test_the_oci_carriers_are_one_signing_configuration(
    kit: KdbxStore, forge: Forge, gh: RecordedGh, tenancy: Tenancy, bucket_id: str
) -> None:
    delivered = _mint(kit, forge, tenancy)

    # A provider cannot sign with two of the three: the fingerprint is computed
    # from the key that was pushed, the user is the one the tenancy holds that
    # key for, and the PEM ends at its end marker -- the value a workflow
    # writes to a file is the value as pushed.
    private = _pushed(gh, 'DRILL_OCI_PRIVATE_KEY')
    user = _pushed(gh, 'DRILL_OCI_USER_OCID')
    assert _pushed(gh, 'DRILL_OCI_FINGERPRINT') == oci_iam.fingerprint(private)
    assert oci_iam.fingerprint(private) in tenancy.identity.keys[user]
    assert user == _drill_user(tenancy) == delivered[derived.DRILL_OCI_HALF]
    assert private == private.strip()


def test_the_oci_key_is_confined_to_the_drill_compartment_and_is_its_own_principal(
    kit: KdbxStore, forge: Forge, tenancy: Tenancy, bucket_id: str
) -> None:
    _ = _mint(kit, forge, tenancy)

    # The compartment is the recorded one, adopted rather than made again, and
    # the policy is one statement over it: what the key may touch is that
    # boundary, and what the boundary holds is the bound.
    assert list(tenancy.identity.compartments) == [DRILL_COMPARTMENT]
    assert [policy.statements for policy in tenancy.identity.policies.values() if policy.name == DRILL_NAME] == [
        [f'Allow group {DRILL_NAME} to manage all-resources in compartment id {DRILL_COMPARTMENT}']
    ]
    # A principal beside the seed's, not a widening of it.
    assert sorted(user.name for user in tenancy.identity.users.values()) == [DRILL_NAME, oci_iam.SEED_NAME]


def test_the_b2_carriers_are_the_read_only_key_on_the_dump_prefix(
    kit: KdbxStore, forge: Forge, gh: RecordedGh, tenancy: Tenancy, api: B2Api, bucket_id: str
) -> None:
    delivered = _mint(kit, forge, tenancy)

    # The pair is one credential, and the key it names is the role whole:
    # list and read on the writer's prefix of the dump bucket, nothing else.
    key_id = _pushed(gh, 'DRILL_B2_KEY_ID')
    minted = api.keys[key_id]
    assert key_id == delivered[derived.DRILL_B2_HALF]
    assert _pushed(gh, 'DRILL_B2_KEY') == minted.secret
    assert (minted.name, minted.capabilities, minted.bucket_id, minted.name_prefix) == (
        b2.DRILL_READ_NAME,
        b2.DRILL_READ_CAPABILITIES,
        bucket_id,
        b2.dumps(bucket_id).name_prefix,
    )
    # Proven by the one act the drill performs, as the new key: the listing
    # was served to it, not to the seed, and of the prefix the role names.
    assert api.listings == [(key_id, bucket_id, b2.DUMP_PREFIX)]


def test_each_half_retires_only_after_its_carriers_are_in_the_listing(
    kit: KdbxStore,
    forge: Forge,
    gh: RecordedGh,
    tenancy: Tenancy,
    api: B2Api,
    bucket_id: str,
    timeline: Timeline,
) -> None:
    first_user_keys = len(tenancy.identity.keys.get(DRILL_NAME, []))
    _ = _mint(kit, forge, tenancy)
    first_b2 = _pushed(gh, 'DRILL_B2_KEY_ID')
    timeline.clear()

    _ = _mint(kit, forge, tenancy)

    # The B2 predecessor goes only once both of its carriers are pushed, and
    # after the OCI half is entirely done: a run that failed between the two
    # halves leaves the reader the Environment names still live.
    assert ('b2', 'b2_delete_key') in timeline
    assert timeline.index(('b2', 'b2_delete_key')) > timeline.index(('gh', 'DRILL_B2_KEY'))
    assert timeline.index(('gh', 'DRILL_B2_KEY_ID')) > timeline.index(('gh', 'DRILL_OCI_PRIVATE_KEY'))
    assert first_b2 not in api.keys
    assert api.named(b2.DRILL_READ_NAME) == [_pushed(gh, 'DRILL_B2_KEY_ID')]
    # And the OCI half ends with the one key this run minted.
    assert tenancy.identity.keys[_drill_user(tenancy)] == [oci_iam.fingerprint(_pushed(gh, 'DRILL_OCI_PRIVATE_KEY'))]
    assert first_user_keys == 0


def test_a_push_that_fails_leaves_the_predecessors_live_and_the_other_half_unminted(
    kit: KdbxStore, forge: Forge, gh: RecordedGh, sink: TimedGh, tenancy: Tenancy, api: B2Api, bucket_id: str
) -> None:
    _ = _mint(kit, forge, tenancy)
    user = _drill_user(tenancy)
    delivered_oci = oci_iam.fingerprint(_pushed(gh, 'DRILL_OCI_PRIVATE_KEY'))
    delivered_b2 = _pushed(gh, 'DRILL_B2_KEY_ID')
    sink.refuses = {'DRILL_OCI_USER_OCID'}

    with pytest.raises(SlotRefused, match='failed'):
        _ = _mint(kit, forge, tenancy)

    # The OCI push failed first, so its successor stands beside the key the
    # Environment holds -- swept first, the drill would sign with a key the
    # tenancy has deleted -- and the B2 half never ran: its one live key is
    # the one delivered before.
    assert delivered_oci in tenancy.identity.keys[user]
    assert len(tenancy.identity.keys[user]) == 2
    assert api.named(b2.DRILL_READ_NAME) == [delivered_b2]

    sink.refuses = set()
    _ = _mint(kit, forge, tenancy)

    # Re-running is the repair: one key each, and the carriers name them.
    assert tenancy.identity.keys[user] == [oci_iam.fingerprint(_pushed(gh, 'DRILL_OCI_PRIVATE_KEY'))]
    assert api.named(b2.DRILL_READ_NAME) == [_pushed(gh, 'DRILL_B2_KEY_ID')]


def test_a_b2_push_that_fails_leaves_the_reader_the_environment_holds_live(
    kit: KdbxStore, forge: Forge, gh: RecordedGh, sink: TimedGh, tenancy: Tenancy, api: B2Api, bucket_id: str
) -> None:
    _ = _mint(kit, forge, tenancy, only=derived.DRILL_B2_HALF)
    delivered = _pushed(gh, 'DRILL_B2_KEY_ID')
    # The second carrier: the id landed and the key did not, which is the
    # half-delivered pair the order below exists for.
    sink.refuses = {'DRILL_B2_KEY'}

    with pytest.raises(SlotRefused, match='failed'):
        _ = _mint(kit, forge, tenancy, only=derived.DRILL_B2_HALF)

    # B2 discloses the secret once, so between the mint and the push the
    # successor exists in this process alone; retired first, the Environment
    # would name a key the account has deleted. The key the Environment can
    # still authenticate as is the one delivered before.
    standing = api.named(b2.DRILL_READ_NAME)
    assert delivered in standing
    assert len(standing) == 2
    assert api.keys[delivered].secret == _pushed(gh, 'DRILL_B2_KEY')


def test_a_push_the_listing_does_not_show_ends_the_run_before_the_next_carrier(
    kit: KdbxStore, forge: Forge, gh: RecordedGh, tenancy: Tenancy, api: B2Api, bucket_id: str
) -> None:
    gh.forgets = True

    with pytest.raises(SlotRefused, match='listing does not show it'):
        _ = _mint(kit, forge, tenancy)

    # Verified carrier by carrier: the first push not showing in the listing
    # ends the run there, so nothing after it is pushed and no B2 key is
    # minted -- a set the drill cannot sign with rather than one that signs
    # as the wrong user.
    assert list(gh.values) == [(OPS_REPOSITORY, DRILL_ENVIRONMENT, 'DRILL_OCI_USER_OCID')]
    assert not api.named(b2.DRILL_READ_NAME)


def test_only_mints_one_half_and_leaves_the_other_untouched(
    kit: KdbxStore, forge: Forge, gh: RecordedGh, tenancy: Tenancy, api: B2Api, bucket_id: str
) -> None:
    oci_only = _mint(kit, forge, tenancy, only=derived.DRILL_OCI_HALF)

    assert list(oci_only) == [derived.DRILL_OCI_HALF]
    assert [name for _, _, name in gh.values] == [slot.name for slot in derived.DRILL_OCI_SLOTS]
    assert not api.named(b2.DRILL_READ_NAME)

    b2_only = _mint(kit, forge, tenancy, only=derived.DRILL_B2_HALF)

    # The OCI key minted above is untouched: one key on the user, and the
    # three OCI carriers as they were.
    assert list(b2_only) == [derived.DRILL_B2_HALF]
    assert len(tenancy.identity.keys[_drill_user(tenancy)]) == 1
    assert [name for _, _, name in gh.values] == [
        slot.name for slot in (*derived.DRILL_OCI_SLOTS, *derived.DRILL_B2_SLOTS)
    ]


def test_a_half_the_drill_does_not_have_is_refused_before_anything_is_minted(
    kit: KdbxStore, forge: Forge, gh: RecordedGh, tenancy: Tenancy, api: B2Api, bucket_id: str
) -> None:
    with pytest.raises(SlotRefused, match='no half of the drill credentials'):
        _ = _mint(kit, forge, tenancy, only='cloudflare')

    assert gh.values == {}
    assert not api.named(b2.DRILL_READ_NAME)
    assert DRILL_NAME not in {user.name for user in tenancy.identity.users.values()}


@pytest.mark.parametrize('only', [None, derived.DRILL_B2_HALF], ids=['both-halves', 'b2-only'])
def test_no_dump_bucket_is_refused_before_a_key_is_minted(
    kit: KdbxStore, forge: Forge, gh: RecordedGh, tenancy: Tenancy, api: B2Api, only: str | None
) -> None:
    mints = api.calls.count('b2_create_key')
    users, keys = dict(tenancy.identity.users), {user: list(held) for user, held in tenancy.identity.keys.items()}
    compartments = dict(tenancy.identity.compartments)

    # No `bucket_id` fixture: the appliance has never been provisioned, so the
    # bucket the reader would be confined to does not exist and there is
    # nothing for a drill to read. Creating one here would mint a reader over
    # an empty prefix and report success. On the default run the refusal
    # fires before the OCI half too: a precondition the run can know before
    # minting is checked before anything is minted, so nothing is created on
    # either platform and nothing is rotated behind a refusal that says
    # nothing about it.
    with pytest.raises(SlotRefused, match='state-backend provision'):
        _ = _mint(kit, forge, tenancy, only=only)

    assert api.calls.count('b2_create_key') == mints
    assert gh.values == {}
    assert tenancy.identity.users == users
    assert tenancy.identity.keys == keys
    assert tenancy.identity.compartments == compartments


def test_a_seed_from_another_tenancy_is_refused_before_anything_is_created(
    kit: KdbxStore,
    forge: Forge,
    gh: RecordedGh,
    tenancy: Tenancy,
    api: B2Api,
    bucket_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_tenancy_ocid(monkeypatch, ELSEWHERE)
    # The state before the first mint, where a run that reached the
    # compartment step would create one: only there does "refused before
    # anything is created" tell a refusal from a late one.
    _ = with_unrecorded_compartment(monkeypatch, conventions.DRILL)
    tenancy.identity.compartments.clear()
    before = dict(tenancy.identity.users)

    # There is no `--compartment` to drop this check with: the drill
    # compartment is a recorded name in the recorded tenancy, and a seed for
    # any other account is refused while the refusal still costs nothing.
    with pytest.raises(CredentialRejected, match=f'{TENANCY}.*{ELSEWHERE}'):
        _ = _mint(kit, forge, tenancy)

    assert tenancy.identity.users == before
    assert tenancy.identity.compartments == {}
    assert gh.values == {}
    assert not api.named(b2.DRILL_READ_NAME)


def test_the_secrets_reach_no_log_line(
    kit: KdbxStore, forge: Forge, gh: RecordedGh, tenancy: Tenancy, bucket_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    _ = _mint(kit, forge, tenancy)

    # The log at the level an operator runs it at names every slot and
    # neither secret; checked against the values the fake was handed, so a
    # leak of either is caught rather than one particular spelling.
    assert _pushed(gh, 'DRILL_OCI_PRIVATE_KEY') not in caplog.text
    assert _pushed(gh, 'DRILL_B2_KEY') not in caplog.text
    for slot in (*derived.DRILL_OCI_SLOTS, *derived.DRILL_B2_SLOTS):
        assert str(slot) in caplog.text


def test_the_carriers_are_the_map_row_s_sinks() -> None:
    # One census of the five addresses: the mint pushes to the values the map
    # row targets, so the register cannot advertise a carrier the mint does
    # not fill, and the mint cannot fill one the register does not name.
    from kluster.scripts.credentials import slots

    assert slots.ROWS[derived.DRILL_CREDENTIALS_ROW].sinks == (*derived.DRILL_OCI_SLOTS, *derived.DRILL_B2_SLOTS)
    assert all(
        isinstance(slot, Slot) and slot.name.startswith(f'{DRILL_ENVIRONMENT.upper()}_')
        for slot in derived.DRILL_OCI_SLOTS + derived.DRILL_B2_SLOTS
    )
