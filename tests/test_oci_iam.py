"""The OCI seed: the IAM objects it needs, the row it writes, and its rotation.

IAM is driven against a fake tenancy rather than a real one — the point being
tested is the shape of what the minter does, which is fixed, rather than what
Oracle does with it. The kit half is a real KeePass file, because the row
shape (§2) is the other half of the same decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

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
