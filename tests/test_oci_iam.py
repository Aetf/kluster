"""The OCI seed: the IAM objects it needs, the row it writes, and its rotation.

IAM is driven against a fake tenancy rather than a real one — the point being
tested is the shape of what the minter does, which is fixed, rather than what
Oracle does with it. The kit half is a real KeePass file, because the row
shape (§2) is the other half of the same decision.
"""

# The SDK ships no stubs; the same waiver `oci_iam.py` itself carries.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import oci
import pytest
from conftest import MemoryKit
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kluster.scripts.credentials import entries, masters, oci_iam
from kluster.scripts.credentials.kdbx import KdbxStore

PASSWORD = 'kit-password'
TENANCY = 'ocid1.tenancy.oc1..tenancy'
ROOT_USER = 'ocid1.user.oc1..root'
SEED_ENTRY = entries.SEEDS['oci'].entry


@dataclass
class Response:
    data: Any


@dataclass
class Named:
    """A stand-in for the SDK models that only ever carry an id and a name."""

    id: str
    name: str
    statements: list[str] = field(default_factory=list[str])


@dataclass
class Key:
    fingerprint: str


@dataclass
class FakeIdentity:
    """One tenancy's IAM, remembering what was done to it."""

    groups: dict[str, Named] = field(default_factory=dict[str, Named])
    users: dict[str, Named] = field(default_factory=dict[str, Named])
    policies: dict[str, Named] = field(default_factory=dict[str, Named])
    memberships: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    keys: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    #: Every key the fake has ever seen uploaded, as fingerprint -> public PEM.
    uploaded: dict[str, str] = field(default_factory=dict[str, str])

    def list_groups(self, compartment_id: str, name: str | None = None) -> Response:
        return Response([group for group in self.groups.values() if name in (None, group.name)])

    def create_group(self, details: Any) -> Response:
        group = Named(id=f'ocid1.group.oc1..{details.name}', name=details.name)
        self.groups[group.id] = group
        return Response(group)

    def list_users(self, compartment_id: str, name: str | None = None) -> Response:
        return Response([user for user in self.users.values() if name in (None, user.name)])

    def create_user(self, details: Any) -> Response:
        # An identity-domains tenancy refuses a user without a primary email
        # (IdcsConversionError), so the fake does too.
        if not getattr(details, 'email', None):
            raise RuntimeError('the primary email must be specified')
        user = Named(id=f'ocid1.user.oc1..{details.name}', name=details.name)
        self.users[user.id] = user
        return Response(user)

    def list_user_group_memberships(self, compartment_id: str, user_id: str, group_id: str) -> Response:
        return Response([(user_id, group_id)] if (user_id, group_id) in self.memberships else [])

    def add_user_to_group(self, details: Any) -> Response:
        self.memberships.add((details.user_id, details.group_id))
        return Response(None)

    def list_policies(self, compartment_id: str) -> Response:
        return Response(list(self.policies.values()))

    def create_policy(self, details: Any) -> Response:
        policy = Named(id=f'ocid1.policy.oc1..{details.name}', name=details.name, statements=list(details.statements))
        self.policies[policy.id] = policy
        return Response(policy)

    def update_policy(self, policy_id: str, details: Any) -> Response:
        policy = self.policies[policy_id]
        policy.statements = list(details.statements)
        return Response(policy)

    def upload_api_key(self, user_id: str, details: Any) -> Response:
        # The real service caps a user at three keys (quota.limit.exceeded).
        if len(self.keys.get(user_id, [])) >= oci_iam.KEY_QUOTA:
            raise oci.exceptions.ServiceError(
                status=400,
                code='IdcsConversionError',
                headers=dict[str, str](),
                message='You can not create ApiKey as maximum quota limit of 3 has been reached.',
            )
        assigned = oci_iam.fingerprint_of_public(details.key)
        self.keys.setdefault(user_id, []).append(assigned)
        self.uploaded[assigned] = details.key
        return Response(Key(fingerprint=assigned))

    def list_api_keys(self, user_id: str) -> Response:
        return Response([Key(fingerprint=value) for value in self.keys.get(user_id, [])])

    def delete_api_key(self, user_id: str, key_fingerprint: str) -> Response:
        self.keys[user_id] = [value for value in self.keys.get(user_id, []) if value != key_fingerprint]
        return Response(None)


@dataclass
class Tenancy:
    """A connect function that hands every caller the same fake IAM.

    It records who connected with which key, which is how "the minted key was
    verified by using it" is checked rather than assumed.
    """

    identity: FakeIdentity = field(default_factory=FakeIdentity)
    connections: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def __call__(self, tenancy: str, user: str, private_key_pem: str) -> FakeIdentity:
        self.connections.append((user, oci_iam.fingerprint(private_key_pem)))
        return self.identity


@pytest.fixture
def kit(tmp_path: Path) -> KdbxStore:
    return KdbxStore.create(tmp_path / 'kit.kdbx', PASSWORD)


@pytest.fixture
def tenancy() -> Tenancy:
    return Tenancy()


@pytest.fixture
def root() -> masters.Credential:
    private_pem, _ = oci_iam.generate_key()
    return masters.Credential(
        root=masters.ROOTS['oci'],
        values={'tenancy': TENANCY, 'user': ROOT_USER, 'private-key': private_pem},
    )


def test_the_fingerprint_is_the_one_oci_computes() -> None:
    # Colon-grouped MD5 of the public key in DER, and a function of the key
    # alone -- which is why it is never stored (§2).
    private_pem, public_pem = oci_iam.generate_key()

    computed = oci_iam.fingerprint(private_pem)

    assert computed == oci_iam.fingerprint_of_public(public_pem)
    assert len(computed) == 47
    assert computed.count(':') == 15


def test_creating_the_seed_builds_the_user_group_and_policy(
    kit: KdbxStore, tenancy: Tenancy, root: masters.Credential
) -> None:
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    identity = tenancy.identity
    assert [group.name for group in identity.groups.values()] == [oci_iam.SEED_NAME]
    assert [user.name for user in identity.users.values()] == [oci_iam.SEED_NAME]
    assert (user_id, next(iter(identity.groups))) in identity.memberships
    # The policy is the whole of what the seed may do, so it is worth being a
    # literal comparison rather than a substring.
    assert [policy.statements for policy in identity.policies.values()] == [list(oci_iam.STATEMENTS)]


def test_the_row_is_the_shape_the_register_specifies(
    kit: KdbxStore, tenancy: Tenancy, root: masters.Credential
) -> None:
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # UserName is the public half, as every row; the PEM is a file, so it is
    # an attachment; the tenancy has nowhere else to go, so it is a protected
    # custom attribute (§2).
    assert kit.get(SEED_ENTRY, attribute='UserName') == user_id
    assert kit.attachments(SEED_ENTRY) == [entries.OCI_KEY_ATTACHMENT]
    assert kit.attribute(SEED_ENTRY, entries.OCI_TENANCY_ATTRIBUTE) == TENANCY
    assert kit.attachment(SEED_ENTRY, entries.OCI_KEY_ATTACHMENT).startswith(b'-----BEGIN PRIVATE KEY-----')
    # The password field stays empty rather than holding a second copy of the
    # key, which a listing would reveal.
    assert kit.describe(SEED_ENTRY)['UserName'] == user_id
    assert kit.get(SEED_ENTRY) == ''


def test_the_stored_row_is_what_the_minter_reads_back(
    kit: KdbxStore, tenancy: Tenancy, root: masters.Credential
) -> None:
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    tenancy_ocid, stored_user, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)

    assert (tenancy_ocid, stored_user) == (TENANCY, user_id)
    assert oci_iam.fingerprint(private_pem) in tenancy.identity.keys[user_id]


def test_the_minted_key_is_verified_by_being_used(kit: KdbxStore, tenancy: Tenancy, root: masters.Credential) -> None:
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    _, _, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)
    # A key that the control plane accepted but the signing path refuses is
    # the failure this catches, so the second connection is as the new key.
    assert tenancy.connections[-1] == (user_id, oci_iam.fingerprint(private_pem))


def test_creating_twice_reuses_the_iam_objects(
    kit: KdbxStore, tenancy: Tenancy, root: masters.Credential, tmp_path: Path
) -> None:
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    second = KdbxStore.create(tmp_path / 'second.kdbx', PASSWORD)

    _ = oci_iam.create_seed(root=root, seeds=second, seed_entry=SEED_ENTRY, connect=tenancy)

    # Repair after a lost kit re-mints the key, not the identity around it:
    # a second user would need a second policy and would drift from §2.
    assert len(tenancy.identity.users) == 1
    assert len(tenancy.identity.groups) == 1
    assert len(tenancy.identity.policies) == 1


def test_a_drifted_policy_is_put_back(kit: KdbxStore, tenancy: Tenancy, root: masters.Credential) -> None:
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    policy = next(iter(tenancy.identity.policies.values()))
    policy.statements = ['Allow group kluster-seed to manage all-resources in tenancy']

    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # The statements are the register's, not whatever a console visit left.
    assert policy.statements == list(oci_iam.STATEMENTS)


def test_rotation_replaces_the_key_and_retires_the_predecessor(
    kit: KdbxStore, tenancy: Tenancy, root: masters.Credential, tmp_path: Path
) -> None:
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    _, _, before = oci_iam.load_seed(kit, SEED_ENTRY)
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)

    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, into=successor, connect=tenancy)

    # One key on the user afterwards, and it is the new one: the predecessor
    # goes only after the successor is stored and verified.
    assert tenancy.identity.keys[user_id] == [current]
    assert current != oci_iam.fingerprint(before)
    # §4.2: the retired kit is left byte-for-byte as it was.
    assert oci_iam.load_seed(kit, SEED_ENTRY)[2] == before
    assert oci_iam.load_seed(successor, SEED_ENTRY)[1] == user_id


def test_rotation_defaults_to_the_database_it_read(kit: KdbxStore, tenancy: Tenancy, root: masters.Credential) -> None:
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    _, _, before = oci_iam.load_seed(kit, SEED_ENTRY)

    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # Rotating one seed on its own (`seed oci rotate`) writes back into the
    # kit; only a whole-kit rotation writes a new file.
    _, _, after = oci_iam.load_seed(kit, SEED_ENTRY)
    assert after != before
    assert oci_iam.fingerprint(after) == current


@dataclass
class LaggingIdentity(FakeIdentity):
    """An identity endpoint the new key has not reached yet.

    Listing keys as the freshly minted user fails with 401 for a while, the
    way the real service does before the key propagates.
    """

    lag: int = 2
    denied: int = 0

    def list_api_keys(self, user_id: str) -> Response:
        # What lags is the freshly uploaded key, so listing an empty user
        # (the pre-mint quota check) answers normally.
        if self.keys.get(user_id) and self.denied < self.lag:
            self.denied += 1
            raise oci.exceptions.ServiceError(
                status=401,
                code='NotAuthenticated',
                headers=dict[str, str](),
                message='required information was not provided',
            )
        return super().list_api_keys(user_id)


def test_verification_outwaits_key_propagation(
    kit: KdbxStore, root: masters.Credential, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenancy = Tenancy(identity=LaggingIdentity())
    naps: list[float] = []
    monkeypatch.setattr(oci_iam.time, 'sleep', naps.append)

    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # Each denial cost one interval of patience, and no more.
    assert naps == [oci_iam.PROPAGATION_INTERVAL] * 2
    _, _, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)
    assert oci_iam.fingerprint(private_pem) in tenancy.identity.keys[user_id]


def test_a_persistent_401_is_raised_when_the_deadline_passes(
    kit: KdbxStore, root: masters.Credential, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenancy = Tenancy(identity=LaggingIdentity(lag=10_000))
    clock = iter(float(tick) for tick in range(0, 10_000, 60))
    monkeypatch.setattr(oci_iam.time, 'monotonic', lambda: next(clock))

    def no_nap(_interval: float) -> None:
        return None

    monkeypatch.setattr(oci_iam.time, 'sleep', no_nap)

    with pytest.raises(oci.exceptions.ServiceError):
        _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)


def test_an_orphaned_key_is_swept_when_the_seed_is_recreated(
    kit: KdbxStore, tenancy: Tenancy, root: masters.Credential, tmp_path: Path
) -> None:
    # A run that died between upload and store: the key exists at OCI, its
    # private half exists nowhere.
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    second = KdbxStore.create(tmp_path / 'second.kdbx', PASSWORD)

    user_id = oci_iam.create_seed(root=root, seeds=second, seed_entry=SEED_ENTRY, connect=tenancy)

    # Only the key whose private half was just stored survives; a user holds
    # at most three keys, so orphans would eventually block minting.
    _, _, private_pem = oci_iam.load_seed(second, SEED_ENTRY)
    assert tenancy.identity.keys[user_id] == [oci_iam.fingerprint(private_pem)]


@dataclass
class RefusingIdentity(FakeIdentity):
    """A tenancy whose IDCS layer refuses key deletion outright."""

    def delete_api_key(self, user_id: str, key_fingerprint: str) -> Response:
        raise oci.exceptions.ServiceError(
            status=401, code='IdcsConversionError', headers=dict[str, str](), message='Client is unauthorized. null'
        )


def test_a_refused_sweep_does_not_fail_the_bring_up(kit: KdbxStore, root: masters.Credential, tmp_path: Path) -> None:
    tenancy = Tenancy(identity=RefusingIdentity())
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    second = KdbxStore.create(tmp_path / 'second.kdbx', PASSWORD)

    # The seed is stored and returned; the orphan stays behind as a console
    # errand rather than a crash.
    user_id = oci_iam.create_seed(root=root, seeds=second, seed_entry=SEED_ENTRY, connect=tenancy)

    _, _, private_pem = oci_iam.load_seed(second, SEED_ENTRY)
    assert oci_iam.fingerprint(private_pem) in tenancy.identity.keys[user_id]
    assert len(tenancy.identity.keys[user_id]) == 2


def test_the_sweep_runs_as_the_seed_itself(kit: KdbxStore, root: masters.Credential, tmp_path: Path) -> None:
    tenancy = Tenancy()
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    second = KdbxStore.create(tmp_path / 'second.kdbx', PASSWORD)

    user_id = oci_iam.create_seed(root=root, seeds=second, seed_entry=SEED_ENTRY, connect=tenancy)

    # The deleting connection is the seed's own key, not the account root's:
    # an identity-domains tenancy allows self-management and refuses the root.
    _, _, private_pem = oci_iam.load_seed(second, SEED_ENTRY)
    assert tenancy.connections[-1] == (user_id, oci_iam.fingerprint(private_pem))
    assert tenancy.identity.keys[user_id] == [oci_iam.fingerprint(private_pem)]


def test_a_refused_sweep_does_not_fail_rotation_either(
    kit: KdbxStore, root: masters.Credential, tmp_path: Path
) -> None:
    tenancy = Tenancy(identity=RefusingIdentity())
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # The successor is minted and stored; the predecessor survives as a
    # console errand rather than blocking the rotation.
    _, _, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)
    assert oci_iam.fingerprint(private_pem) == current


def test_rotation_sweeps_as_the_successor_not_the_predecessor(
    kit: KdbxStore, tenancy: Tenancy, root: masters.Credential
) -> None:
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # Deleting with the predecessor's own session would saw off the key it
    # signs with mid-sweep; the deleting connection must be the successor.
    assert tenancy.connections[-1] == (user_id, current)
    assert tenancy.identity.keys[user_id] == [current]


def test_rotation_makes_room_before_minting(kit: KdbxStore, tenancy: Tenancy, root: masters.Credential) -> None:
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    # Fill the quota with orphans behind the stored key's back.
    for _ in range(oci_iam.KEY_QUOTA - 1):
        _, public_pem = oci_iam.generate_key()
        _ = tenancy.identity.upload_api_key(user_id, oci.identity.models.CreateApiKeyDetails(key=public_pem))

    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # The orphans were swept to make room, the successor minted, the
    # predecessor retired: exactly one key stands.
    assert tenancy.identity.keys[user_id] == [current]


def test_a_full_user_no_key_of_which_can_go_names_the_errand(
    kit: KdbxStore, root: masters.Credential, tmp_path: Path
) -> None:
    tenancy = Tenancy(identity=RefusingIdentity())
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    for _ in range(oci_iam.KEY_QUOTA - 1):
        _, public_pem = oci_iam.generate_key()
        _ = tenancy.identity.upload_api_key(user_id, oci.identity.models.CreateApiKeyDetails(key=public_pem))

    # The sweep is refused wholesale, so rotation must refuse to mint -- with
    # the fingerprints in hand, not a bare quota 400.
    with pytest.raises(oci_iam.CredentialRejected, match='delete the superseded ones in the console'):
        _ = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)


class Interrupted(RuntimeError):
    """A run that stopped mid-flight, the way a lost process or a 500 does."""


class Interruptible:
    """A tenancy that counts remote calls and stops the run at the k-th.

    Two ways to stop, because they leave different worlds behind: `before`
    never reaches the service, `after` lets the service act and loses the
    answer -- which is the one that strands a key whose private half the
    caller never got to store.
    """

    def __init__(self, identity: FakeIdentity, *, fail_at: int | None = None, when: str = 'after') -> None:
        self.identity: FakeIdentity = identity
        self.fail_at: int | None = fail_at
        self.when: str = when
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.identity, name)

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            fatal = len(self.calls) == self.fail_at
            if fatal and self.when == 'before':
                raise Interrupted(f'call {self.fail_at} ({name}) never reached the service')
            result = target(*args, **kwargs)
            if fatal:
                raise Interrupted(f'call {self.fail_at} ({name}) reached the service; the answer was lost')
            return result

        return counted


@dataclass
class FaultyTenancy:
    """`Tenancy`, with every client it hands out wired through one interrupter."""

    identity: FakeIdentity = field(default_factory=FakeIdentity)
    connections: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    fail_at: int | None = None
    when: str = 'after'
    faulty: Interruptible | None = None

    def __post_init__(self) -> None:
        self.faulty = Interruptible(self.identity, fail_at=self.fail_at, when=self.when)

    def __call__(self, tenancy: str, user: str, private_key_pem: str) -> Interruptible:
        self.connections.append((user, oci_iam.fingerprint(private_key_pem)))
        assert self.faulty is not None
        return self.faulty

    @property
    def counted(self) -> int:
        assert self.faulty is not None
        return len(self.faulty.calls)


def _kit_never_lies(kit: KdbxStore, identity: FakeIdentity) -> None:
    """Whatever the kit holds must be a key the tenancy would accept.

    The invariant an interrupted run is most likely to break: a row written
    before the key works, or left behind after the key was deleted, is a kit
    that answers a question wrongly rather than not at all.
    """
    if not kit.has(SEED_ENTRY):
        return
    _, user_id, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)
    assert oci_iam.fingerprint(private_pem) in identity.keys.get(user_id, []), 'the kit holds a key the tenancy has not'


def _keys_are_bounded(identity: FakeIdentity) -> None:
    """No user may collect more keys than it can hold; the quota is three."""
    for user_id, held in identity.keys.items():
        assert len(held) <= oci_iam.KEY_QUOTA, f'{user_id} holds {len(held)} keys'


def _cheap_key() -> tuple[str, str]:
    """A key pair the sweep can afford, as (private PEM, public PEM).

    Short, and generated here rather than by `generate_key`, because the
    sweep's cost is dominated by *reading* a PEM back: the fingerprint is a
    function of the key and is recomputed on every use (§2), so the sweep
    parses one a few hundred times. A production key is 2048 bits
    (`oci_iam.KEY_SIZE`) and that is what the rest of this file mints; what a
    fault sweep is about is the order of the calls around a key, not the
    arithmetic inside one.
    """
    private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    return (
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )


#: Generated once and cycled. Eight is more than any single test draws, so
#: the keys one test sees are all distinct.
_KEY_POOL = [_cheap_key() for _ in range(8)]


@pytest.fixture
def pooled_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = itertools.cycle(_KEY_POOL)
    monkeypatch.setattr(oci_iam, 'generate_key', lambda: next(keys))


def _fresh_root() -> masters.Credential:
    private_pem, _ = oci_iam.generate_key()
    return masters.Credential(
        root=masters.ROOTS['oci'],
        values={'tenancy': TENANCY, 'user': ROOT_USER, 'private-key': private_pem},
    )


def _create(tenancy: FaultyTenancy, kit: KdbxStore) -> None:
    _ = oci_iam.create_seed(root=_fresh_root(), seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)


def _rotate(tenancy: FaultyTenancy, kit: KdbxStore) -> None:
    _ = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)


def _calls_made(operation: Callable[[FaultyTenancy, KdbxStore], None], *, prepared: bool) -> int:
    """How many remote calls one uninterrupted run of `operation` makes.

    Measured rather than written down, so the sweep below covers exactly the
    calls the operation makes today and widens by itself when it grows one.
    """
    tenancy = FaultyTenancy()
    kit = MemoryKit()
    if prepared:
        _create(tenancy, kit)
    before = tenancy.counted
    operation(tenancy, kit)
    return tenancy.counted - before


CREATE_CALLS = _calls_made(_create, prepared=False)
ROTATE_CALLS = _calls_made(_rotate, prepared=True)

#: Both ways a run can stop at call k: without the service having acted, and
#: with the service having acted and the answer lost on the way back. The
#: second is the one that strands a key nobody holds the private half of.
CRASH_POINTS = 'before', 'after'


def _survived(tenancy: FaultyTenancy, kit: KdbxStore) -> None:
    """The invariants an interrupted run has to leave standing.

    Checked after the crash and again after the re-run, because a kit that
    lies is just as wrong while the operator is still deciding whether to
    re-run it.
    """
    _kit_never_lies(kit, tenancy.identity)
    _keys_are_bounded(tenancy.identity)


@pytest.mark.parametrize('when', CRASH_POINTS)
@pytest.mark.parametrize('failing_call', range(1, CREATE_CALLS + 1))
def test_creating_the_seed_heals_from_a_failure_at_any_call(failing_call: int, when: str, pooled_keys: None) -> None:
    identity = FakeIdentity()
    kit = MemoryKit()
    crashed = FaultyTenancy(identity=identity, fail_at=failing_call, when=when)

    with pytest.raises(Interrupted):
        _create(crashed, kit)
    _survived(crashed, kit)

    # 'Idempotent by probing' (docs/credentials.md) means exactly this: the
    # repair is the same command, with nothing remembered about where it
    # stopped.
    healed = FaultyTenancy(identity=identity)
    _create(healed, kit)

    _survived(healed, kit)
    _, user_id, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)
    # And the orphan a lost run left behind is gone, not merely tolerated:
    # the user holds one key, the one the kit holds the private half of.
    assert identity.keys[user_id] == [oci_iam.fingerprint(private_pem)]


@pytest.mark.parametrize('when', CRASH_POINTS)
@pytest.mark.parametrize('failing_call', range(1, ROTATE_CALLS + 1))
def test_rotating_the_seed_heals_from_a_failure_at_any_call(failing_call: int, when: str, pooled_keys: None) -> None:
    identity = FakeIdentity()
    kit = MemoryKit()
    _create(FaultyTenancy(identity=identity), kit)
    crashed = FaultyTenancy(identity=identity, fail_at=failing_call, when=when)

    with pytest.raises(Interrupted):
        _rotate(crashed, kit)
    # A rotation interrupted anywhere leaves a working seed: whichever key the
    # kit holds afterwards, predecessor or successor, still authenticates.
    _survived(crashed, kit)

    healed = FaultyTenancy(identity=identity)
    _rotate(healed, kit)

    _survived(healed, kit)
    _, user_id, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)
    assert identity.keys[user_id] == [oci_iam.fingerprint(private_pem)]
