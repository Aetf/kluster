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
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import oci
import pytest
from memory_kit import MemoryKit
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kluster import conventions
from kluster.scripts.credentials import entries, masters, oci_iam
from kluster.scripts.credentials.kdbx import KdbxStore

PASSWORD = 'kit-password'
TENANCY = 'ocid1.tenancy.oc1..tenancy'
ROOT_USER = 'ocid1.user.oc1..root'
DOMAIN_URL = 'https://idcs-000.identity.oraclecloud.com:443'
SEED_ENTRY = entries.SEEDS['oci'].entry

#: How many policies one unfiltered `list_policies` answers with. The real
#: service pages; this is the smallest page that makes a walk and a filtered
#: lookup behave differently.
POLICY_PAGE = 1


@dataclass
class Response:
    data: Any


@dataclass
class Named:
    """A stand-in for the SDK models that only ever carry an id and a name.

    A user or a group in a tenancy with identity domains has two identifiers,
    not one: the OCID every API speaks (`id`, which is what the legacy service
    calls it) and the SCIM id the domains API addresses its own resources by.
    Both are minted here whichever API created the resource, because the real
    service does the same — the domain is where these live, and the legacy
    call is a shim over it.
    """

    id: str
    name: str
    statements: list[str] = field(default_factory=list[str])
    handle: str = ''
    #: Compartments alone are deleted asynchronously and keep their name while
    #: they go, which is what makes "adopt the one of this name" a question
    #: about state as well as about the name.
    lifecycle_state: str = 'ACTIVE'


def _named(kind: str, name: str) -> Named:
    return Named(id=f'ocid1.{kind}.oc1..{name}', name=name, handle=f'{kind}-{name}-scim-id')


@dataclass
class Key:
    fingerprint: str


@dataclass
class DomainSummary:
    """One identity domain, as `list_domains` describes it."""

    url: str
    display_name: str
    type: str = 'DEFAULT'


@dataclass
class DomainKey:
    """An API key as the domains API names it: by its own id, not its fingerprint."""

    id: str
    fingerprint: str


@dataclass
class DomainKeys:
    """The SCIM list envelope `list_my_api_keys` answers with."""

    resources: list[DomainKey]


@dataclass
class DomainMember:
    """One member of a group, as SCIM carries it: inside the group itself."""

    value: str


@dataclass
class DomainResource:
    """A user or a group as the domains API returns it, under both its names."""

    id: str
    ocid: str
    members: list[DomainMember] | None = None


@dataclass
class DomainResources:
    """The SCIM list envelope a domains search answers with."""

    resources: list[DomainResource]


def _refused(what: str) -> oci.exceptions.ServiceError:
    """What the domains API answers a caller it will not serve.

    No `code` at all, the way a live domains refusal comes back: the status
    and the message are the whole of what it says.
    """
    return oci.exceptions.ServiceError(status=401, code=None, headers=dict[str, str](), message=what)


def _filter_value(expression: str) -> str:
    """The literal out of a SCIM `attribute eq "value"` filter."""
    matched = re.search(r'"([^"]*)"', expression)
    assert matched is not None, f'not a filter this fake understands: {expression}'
    return matched.group(1)


def _shim_refusal(endpoint: str) -> oci.exceptions.ServiceError:
    """What the legacy conversion shim answers when it will not convert.

    401 `IdcsConversionError` with a message that names nothing: the refusal
    a live bring-up met on `DeleteApiKey` every time and on `ListApiKeys`
    intermittently, minutes after the same endpoint and the same credential
    had served the identical call.
    """
    return oci.exceptions.ServiceError(
        status=401,
        code='IdcsConversionError',
        headers=dict[str, str](),
        message=f'Client is unauthorized. null ({endpoint})',
    )


#: Every legacy endpoint that is a conversion shim over the identity domain,
#: under the name the shim answers with. These are the calls the domains API
#: also serves, and therefore the ones a refusal must be survivable on.
#: `ListPolicies`, `CreatePolicy`, `UpdatePolicy`, `ListCompartments`,
#: `CreateCompartment` and `ListDomains` are absent because they are IAM's own
#: concepts: no domain endpoint answers them, so a refusal there has nowhere to
#: fall back to.
SHIMMED_ENDPOINTS = (
    'ListUsers',
    'CreateUser',
    'ListGroups',
    'CreateGroup',
    'ListUserGroupMemberships',
    'AddUserToGroup',
    'UploadApiKey',
    'ListApiKeys',
    'DeleteApiKey',
)

#: Every identity-domains operation this module makes, under the SDK's name
#: for it. Each one has a legacy counterpart, which is what makes a refusal on
#: any single one survivable rather than fatal.
DOMAIN_OPERATIONS = (
    'list_groups',
    'create_group',
    'list_users',
    'create_user',
    'get_group',
    'patch_group',
    'list_api_keys',
    'create_api_key',
    'delete_api_key',
    'list_my_api_keys',
    'create_my_api_key',
    'delete_my_api_key',
)


@dataclass
class DomainPolicy:
    """What the identity domain refuses, and for how long.

    Refusals are per operation because that is how they were met live: one
    endpoint answering while its neighbour on the same host and the same
    credential does not.
    """

    #: Operations refused every time.
    always: frozenset[str] = frozenset()
    #: Operations refused the first time and taken afterwards -- the shape
    #: that makes a one-shot caller give up on a call that would have worked.
    once: set[str] = field(default_factory=set[str])
    #: Every operation the domain actually served, in order. Which endpoint
    #: answered is the whole of what these tests are about, and it is not
    #: visible in the tenancy state afterwards: both APIs write the same fact.
    served: list[str] = field(default_factory=list[str])

    def check(self, operation: str) -> None:
        if operation in self.always:
            raise _refused(f'the identity domain does not serve {operation} here')
        if operation in self.once:
            self.once.discard(operation)
            raise _refused('The required information to complete authentication was not provided.')
        self.served.append(operation)


@dataclass
class FakeIdentity:
    """One tenancy's IAM, remembering what was done to it."""

    groups: dict[str, Named] = field(default_factory=dict[str, Named])
    users: dict[str, Named] = field(default_factory=dict[str, Named])
    policies: dict[str, Named] = field(default_factory=dict[str, Named])
    #: The tenancy's own children, which is the only level this program makes
    #: one at.
    compartments: dict[str, Named] = field(default_factory=dict[str, Named])
    memberships: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    keys: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    #: Every key the fake has ever seen uploaded, as fingerprint -> public PEM.
    uploaded: dict[str, str] = field(default_factory=dict[str, str])
    #: Every shim-converted endpoint this tenancy was asked for, in order.
    #: Which of the two services answered a call is not visible in the tenancy
    #: state afterwards -- both write the same fact -- so it is recorded here.
    shim_calls: list[str] = field(default_factory=list[str])

    def check_shim(self, endpoint: str) -> None:
        """Note a call to an endpoint the identity domain also serves.

        One guard rather than nine overrides, because the set of
        shim-converted endpoints is one fact about the service:
        `ShimlessIdentity` and `FlakyShim` refuse here, and a test that asserts
        a call never reached the shim reads `shim_calls`.
        """
        self.shim_calls.append(endpoint)

    def list_groups(self, compartment_id: str, name: str | None = None) -> Response:
        self.check_shim('ListGroups')
        return Response([group for group in self.groups.values() if name in (None, group.name)])

    def create_group(self, details: Any) -> Response:
        self.check_shim('CreateGroup')
        group = _named('group', details.name)
        self.groups[group.id] = group
        return Response(group)

    def list_users(self, compartment_id: str, name: str | None = None) -> Response:
        self.check_shim('ListUsers')
        return Response([user for user in self.users.values() if name in (None, user.name)])

    def create_user(self, details: Any) -> Response:
        self.check_shim('CreateUser')
        # An identity-domains tenancy refuses a user without a primary email
        # (IdcsConversionError), so the fake does too.
        if not getattr(details, 'email', None):
            raise RuntimeError('the primary email must be specified')
        user = _named('user', details.name)
        self.users[user.id] = user
        return Response(user)

    def list_user_group_memberships(self, compartment_id: str, user_id: str, group_id: str) -> Response:
        self.check_shim('ListUserGroupMemberships')
        return Response([(user_id, group_id)] if (user_id, group_id) in self.memberships else [])

    def add_user_to_group(self, details: Any) -> Response:
        self.check_shim('AddUserToGroup')
        self.memberships.add((details.user_id, details.group_id))
        return Response(None)

    def list_policies(self, compartment_id: str, name: str | None = None) -> Response:
        """The tenancy's policies, and only one page of them when unfiltered.

        The service pages every listing and a single call hands back one page,
        so a caller that walks instead of filtering sees the beginning of the
        tenancy rather than the whole of it. One policy per page here, which is
        the smallest shape that tells the two callers apart.
        """
        found = [policy for policy in self.policies.values() if name in (None, policy.name)]
        return Response(found if name is not None else found[:POLICY_PAGE])

    def create_policy(self, details: Any) -> Response:
        # Policy names are unique within a compartment: a create that means
        # "make sure this exists" is answered with a 409, not with a second
        # policy, so a lookup that missed an existing one fails the run.
        if any(policy.name == details.name for policy in self.policies.values()):
            raise oci.exceptions.ServiceError(
                status=409,
                code='NameAlreadyExists',
                headers=dict[str, str](),
                message=f'policy {details.name} already exists',
            )
        policy = Named(id=f'ocid1.policy.oc1..{details.name}', name=details.name, statements=list(details.statements))
        self.policies[policy.id] = policy
        return Response(policy)

    def update_policy(self, policy_id: str, details: Any) -> Response:
        policy = self.policies[policy_id]
        policy.statements = list(details.statements)
        return Response(policy)

    def list_compartments(self, compartment_id: str, name: str | None = None) -> Response:
        """The children of one compartment, filtered by name where one is given.

        Deleted compartments are listed like any other: the service keeps them
        visible while they go, so telling them apart is the caller's job.
        """
        return Response([found for found in self.compartments.values() if name in (None, found.name)])

    def create_compartment(self, details: Any) -> Response:
        # A compartment name is unique among the children of one compartment,
        # exactly as a policy name is: a create that means "make sure this
        # exists" is answered with a 409, not with a second compartment, so a
        # lookup that missed an existing one fails the run. A name released by
        # a completed deletion is free again, which is why the state matters.
        if any(
            found.name == details.name and found.lifecycle_state != 'DELETED' for found in self.compartments.values()
        ):
            raise oci.exceptions.ServiceError(
                status=409,
                code='NameAlreadyExists',
                headers=dict[str, str](),
                message=f'compartment {details.name} already exists',
            )
        made = _named('compartment', details.name)
        self.compartments[made.id] = made
        return Response(made)

    def register_key(self, user_id: str, public_pem: str) -> str:
        """Put a key on a user, whichever endpoint asked. Returns the fingerprint.

        One rule for all three ways in (the legacy upload, the domain's
        administrative create, the domain's self-service create), because the
        quota is a property of the user rather than of the endpoint.
        """
        # The real service caps a user at three keys (quota.limit.exceeded).
        if len(self.keys.get(user_id, [])) >= oci_iam.KEY_QUOTA:
            raise oci.exceptions.ServiceError(
                status=400,
                code='IdcsConversionError',
                headers=dict[str, str](),
                message='You can not create ApiKey as maximum quota limit of 3 has been reached.',
            )
        assigned = oci_iam.fingerprint_of_public(public_pem)
        self.keys.setdefault(user_id, []).append(assigned)
        self.uploaded[assigned] = public_pem
        return assigned

    def by_handle(self, handle: str, among: dict[str, Named]) -> Named | None:
        for candidate in among.values():
            if candidate.handle == handle:
                return candidate
        return None

    def upload_api_key(self, user_id: str, details: Any) -> Response:
        self.check_shim('UploadApiKey')
        return Response(Key(fingerprint=self.register_key(user_id, details.key)))

    def list_api_keys(self, user_id: str) -> Response:
        self.check_shim('ListApiKeys')
        return Response([Key(fingerprint=value) for value in self.keys.get(user_id, [])])

    def delete_api_key(self, user_id: str, key_fingerprint: str) -> Response:
        self.check_shim('DeleteApiKey')
        self.check_delete_flake()
        self.keys[user_id] = [value for value in self.keys.get(user_id, []) if value != key_fingerprint]
        return Response(None)

    def list_domains(self, compartment_id: str) -> Response:
        return Response([DomainSummary(url=DOMAIN_URL, display_name='Default')])

    def check_read_lag(self) -> None:
        """Overridden by `LaggingIdentity`: a fresh key lags on every endpoint."""
        return None

    def check_delete_flake(self) -> None:
        """Overridden by `FlakyDeletes`: a delete refused now and taken later."""
        return None


@dataclass
class FakeDomain:
    """The identity-domains endpoint, as one authenticated user sees it.

    A separate object from `FakeIdentity` because it is a separate service on
    a separate endpoint, over the same tenancy state: what the legacy shim
    shows and what the domain shows are two views of one account, which is
    why this fake writes into `identity` rather than keeping a store of its
    own.

    It enforces the two authorization rules that decide which endpoint a call
    may use. The self-service (`Me`) endpoints take no user id at all, so they
    can only ever act on `user_id` — a fake that accepted one could not tell a
    correct caller from an incorrect one. The administrative endpoints take an
    explicit resource and are refused unless the caller holds domain-admin
    rights, which here means being the account root.
    """

    identity: FakeIdentity
    user_id: str
    admin: bool = False
    policy: DomainPolicy = field(default_factory=DomainPolicy)

    @staticmethod
    def key_id(key_fingerprint: str) -> str:
        return f'apikey-{key_fingerprint}'

    def _administrative(self, operation: str) -> None:
        if not self.admin:
            raise _refused(f'{operation} needs domain administrator rights')
        self.policy.check(operation)

    # -- the self-service half ---------------------------------------------

    def list_my_api_keys(self) -> Response:
        self.identity.check_read_lag()
        self.policy.check('list_my_api_keys')
        held = self.identity.keys.get(self.user_id, [])
        return Response(DomainKeys(resources=[DomainKey(id=self.key_id(value), fingerprint=value) for value in held]))

    def delete_my_api_key(self, my_api_key_id: str) -> Response:
        self.identity.check_delete_flake()
        self.policy.check('delete_my_api_key')
        held = self.identity.keys.get(self.user_id, [])
        remaining = [value for value in held if self.key_id(value) != my_api_key_id]
        if remaining == held:
            raise oci.exceptions.ServiceError(
                status=404, code='NotFound', headers=dict[str, str](), message=f'no api key {my_api_key_id}'
            )
        self.identity.keys[self.user_id] = remaining
        return Response(None)

    def create_my_api_key(self, my_api_key: Any) -> Response:
        self.policy.check('create_my_api_key')
        assert oci_iam.API_KEY_SCHEMA in my_api_key.schemas, 'a SCIM payload names its own schema'
        return Response(Key(fingerprint=self.identity.register_key(self.user_id, my_api_key.key)))

    # -- the administrative half -------------------------------------------

    def list_groups(self, filter: str) -> Response:  # noqa: A002 -- the SDK's parameter name
        self._administrative('list_groups')
        wanted = _filter_value(filter)
        return Response(
            DomainResources(
                resources=[
                    DomainResource(id=group.handle, ocid=group.id)
                    for group in self.identity.groups.values()
                    if group.name == wanted
                ]
            )
        )

    def create_group(self, group: Any) -> Response:
        self._administrative('create_group')
        assert oci_iam.GROUP_SCHEMA in group.schemas, 'a SCIM payload names its own schema'
        made = _named('group', group.display_name)
        self.identity.groups[made.id] = made
        return Response(DomainResource(id=made.handle, ocid=made.id))

    def list_users(self, filter: str) -> Response:  # noqa: A002 -- the SDK's parameter name
        self._administrative('list_users')
        wanted = _filter_value(filter)
        return Response(
            DomainResources(
                resources=[
                    DomainResource(id=user.handle, ocid=user.id)
                    for user in self.identity.users.values()
                    if user.name == wanted
                ]
            )
        )

    def create_user(self, user: Any) -> Response:
        self._administrative('create_user')
        assert oci_iam.USER_SCHEMA in user.schemas, 'a SCIM payload names its own schema'
        # The domain demands more of a user than IAM does, and refusing here
        # is the whole reason the legacy CreateUser could not be used: it has
        # nowhere to put either of these.
        if not (user.name and user.name.family_name):
            raise _refused('the family name is required')
        addresses: list[Any] = list(user.emails or [])
        if not any(address.primary for address in addresses):
            raise _refused('a primary email address is required')
        made = _named('user', user.user_name)
        self.identity.users[made.id] = made
        return Response(DomainResource(id=made.handle, ocid=made.id))

    def get_group(self, group_id: str, attributes: str) -> Response:
        self._administrative('get_group')
        group = self.identity.by_handle(group_id, self.identity.groups)
        if group is None:
            raise oci.exceptions.ServiceError(
                status=404, code='NotFound', headers=dict[str, str](), message=f'no group {group_id}'
            )
        assert 'members' in attributes, 'membership is only returned when it is asked for'
        members = [
            DomainMember(value=user.handle)
            for user in self.identity.users.values()
            if (user.id, group.id) in self.identity.memberships
        ]
        return Response(DomainResource(id=group.handle, ocid=group.id, members=members))

    def patch_group(self, group_id: str, patch_op: Any) -> Response:
        self._administrative('patch_group')
        group = self.identity.by_handle(group_id, self.identity.groups)
        assert group is not None, f'no group {group_id}'
        for operation in patch_op.operations:
            assert (operation.op, operation.path) == (oci.identity_domains.models.Operations.OP_ADD, 'members')
            for member in operation.value:
                user = self.identity.by_handle(str(member['value']), self.identity.users)
                if user is None:
                    # A member named by an OCID rather than by the SCIM id the
                    # domain assigned: the shape a legacy-made principal has.
                    raise _refused(f'no user {member["value"]} in this domain')
                self.identity.memberships.add((user.id, group.id))
        return Response(None)

    def create_api_key(self, api_key: Any) -> Response:
        self._administrative('create_api_key')
        assert oci_iam.API_KEY_SCHEMA in api_key.schemas, 'a SCIM payload names its own schema'
        user = self.identity.by_handle(str(api_key.user.value), self.identity.users)
        if user is None:
            raise _refused(f'no user {api_key.user.value} in this domain')
        return Response(Key(fingerprint=self.identity.register_key(user.id, api_key.key)))

    def list_api_keys(self, filter: str) -> Response:  # noqa: A002 -- the SDK's parameter name
        """Any user's keys, named by the OCID in the filter.

        The administrative listing, which is what lets a session read a user
        that is not its own without the legacy shim. Only `user.ocid eq` is
        understood, because it is the only filter the caller has the input
        for: the row carries an OCID and the SCIM id would itself need a
        lookup.
        """
        self._administrative('list_api_keys')
        self.identity.check_read_lag()
        held = self.identity.keys.get(_filter_value(filter), [])
        return Response(DomainKeys(resources=[DomainKey(id=self.key_id(value), fingerprint=value) for value in held]))

    def delete_api_key(self, api_key_id: str) -> Response:
        """Retire any user's key. The id names the key, so no subject is given."""
        self._administrative('delete_api_key')
        self.identity.check_delete_flake()
        for user_id, held in self.identity.keys.items():
            remaining = [value for value in held if self.key_id(value) != api_key_id]
            if remaining != held:
                self.identity.keys[user_id] = remaining
                return Response(None)
        raise oci.exceptions.ServiceError(
            status=404, code='NotFound', headers=dict[str, str](), message=f'no api key {api_key_id}'
        )


@dataclass
class Tenancy:
    """A connect function that hands every caller the same fake IAM.

    It records who connected with which key, which is how "the minted key was
    verified by using it" is checked rather than assumed. Domain connections
    are recorded apart from legacy ones, since which service a call went to
    is the whole of one defect.
    """

    identity: FakeIdentity = field(default_factory=FakeIdentity)
    connections: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    domain_connections: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    #: What this tenancy's domain refuses, and to whom. Domain-admin rights
    #: belong to the account root; every other caller gets the self-service
    #: half and nothing more.
    policy: DomainPolicy = field(default_factory=DomainPolicy)

    def __call__(
        self, tenancy: str, user: str, private_key_pem: str, *, domain_url: str | None = None
    ) -> FakeIdentity | FakeDomain:
        if domain_url is not None:
            self.domain_connections.append((domain_url, user, oci_iam.fingerprint(private_key_pem)))
            return FakeDomain(identity=self.identity, user_id=user, admin=user == ROOT_USER, policy=self.policy)
        self.connections.append((user, oci_iam.fingerprint(private_key_pem)))
        return self.identity


@pytest.fixture
def kit(tmp_path: Path) -> KdbxStore:
    return KdbxStore.create(tmp_path / 'kit.kdbx', PASSWORD)


@pytest.fixture
def tenancy() -> Tenancy:
    return Tenancy()


@pytest.fixture(autouse=True)
def unhurried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every bounded wait in this module happens instantly.

    What those waits wait for is a remote service catching up, which a fake
    has no way of not having done. The clock still moves -- one interval per
    reading -- so a deadline is still reachable and a test can still assert
    that one was reached; only the sleeping is skipped. A test that cares how
    many intervals were spent patches `sleep` again on top of this.
    """
    ticks = itertools.count(0.0, oci_iam.PROPAGATION_INTERVAL)

    def no_nap(_interval: float) -> None:
        return None

    monkeypatch.setattr(oci_iam.time, 'sleep', no_nap)
    monkeypatch.setattr(oci_iam.time, 'monotonic', lambda: next(ticks))


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


def test_a_policy_the_first_page_does_not_show_is_still_found(
    kit: KdbxStore, tenancy: Tenancy, root: masters.Credential
) -> None:
    # A tenancy holds more policies than one listing returns, and the seed's
    # need not be among the first of them. Walking the listing rather than
    # asking for the name reports an existing policy as absent, and creating it
    # again is a 409 rather than a second policy -- so a re-run that is
    # supposed to converge fails instead, on a tenancy that had merely grown.
    _ = tenancy.identity.create_policy(
        type('Details', (), {'name': 'someone-elses-policy', 'statements': ['Allow group other to read all-resources']})
    )
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    assert sorted(policy.name for policy in tenancy.identity.policies.values()) == [
        oci_iam.SEED_NAME,
        'someone-elses-policy',
    ]


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


# -- the compartment a consumer's key is confined to -------------------------


@pytest.fixture
def seeded(kit: KdbxStore, tenancy: Tenancy, root: masters.Credential) -> KdbxStore:
    """A kit holding the OCI seed, which is what a mint reads."""
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    return kit


def _compartments(tenancy: Tenancy) -> list[str]:
    return sorted(found.name for found in tenancy.identity.compartments.values())


def _policy(tenancy: Tenancy, name: str) -> list[str]:
    return next(policy.statements for policy in tenancy.identity.policies.values() if policy.name == name)


def test_a_mint_creates_the_compartment_conventions_names_for_the_consumer(seeded: KdbxStore, tenancy: Tenancy) -> None:
    intended = conventions.OCI_COMPARTMENTS[conventions.PHYSICAL]

    _ = oci_iam.mint_api_key(seeded, consumer=conventions.PHYSICAL, seed_entry=SEED_ENTRY, connect=tenancy)

    # Nothing had to exist first: the boundary is named in `conventions`, and
    # the run that mints the key confined to it is the run that makes it.
    assert _compartments(tenancy) == [intended.name]
    created = next(iter(tenancy.identity.compartments.values()))
    name = f'{conventions.CLUSTER_NAME}-{conventions.PHYSICAL}'
    assert _policy(tenancy, name) == [f'Allow group {name} to manage all-resources in compartment id {created.id}']


def test_the_ocid_of_a_new_compartment_is_announced_as_the_line_to_commit(
    seeded: KdbxStore, tenancy: Tenancy, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _ = oci_iam.mint_api_key(seeded, consumer=conventions.PHYSICAL, seed_entry=SEED_ENTRY, connect=tenancy)

    # The OCID is a site fact, and the file that carries it is committed: the
    # stack reads it there and refuses until it is written, so the run that
    # learns it says so rather than leaving it in a console it created.
    created = next(iter(tenancy.identity.compartments.values()))
    assert created.id in caplog.text
    assert 'conventions.OCI_COMPARTMENTS' in caplog.text


def test_a_compartment_that_is_already_there_is_adopted_by_name(seeded: KdbxStore, tenancy: Tenancy) -> None:
    _ = oci_iam.mint_api_key(seeded, consumer=conventions.PHYSICAL, seed_entry=SEED_ENTRY, connect=tenancy)
    first = next(iter(tenancy.identity.compartments.values())).id

    _ = oci_iam.mint_api_key(seeded, consumer=conventions.PHYSICAL, seed_entry=SEED_ENTRY, connect=tenancy)

    # Idempotent like the user and the group above it: a second compartment of
    # the same name is not something the service would make anyway -- the
    # create is answered with a 409 -- so re-running the mint has to find the
    # one that is there.
    assert len(tenancy.identity.compartments) == 1
    assert next(iter(tenancy.identity.compartments.values())).id == first


def test_a_compartment_being_deleted_is_not_adopted(seeded: KdbxStore, tenancy: Tenancy) -> None:
    intended = conventions.OCI_COMPARTMENTS[conventions.PHYSICAL]
    going = _named('compartment', intended.name)
    going.lifecycle_state = 'DELETED'
    tenancy.identity.compartments[going.id] = going

    _ = oci_iam.mint_api_key(seeded, consumer=conventions.PHYSICAL, seed_entry=SEED_ENTRY, connect=tenancy)

    # The name survives the compartment while it goes, so adopting on the name
    # alone would confine the key to a boundary on its way out.
    live = [found for found in tenancy.identity.compartments.values() if found.lifecycle_state == 'ACTIVE']
    assert [found.name for found in live] == [intended.name]


def test_a_recorded_compartment_is_used_rather_than_re_created(
    seeded: KdbxStore, tenancy: Tenancy, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _named('compartment', 'kluster')
    tenancy.identity.compartments[recorded.id] = recorded
    monkeypatch.setitem(
        conventions.OCI_COMPARTMENTS,
        conventions.STATE_BACKEND,
        conventions.Compartment(consumer=conventions.STATE_BACKEND, name=recorded.name, ocid=recorded.id),
    )

    minted = oci_iam.mint_api_key(seeded, consumer=conventions.STATE_BACKEND, seed_entry=SEED_ENTRY, connect=tenancy)

    name = f'{conventions.CLUSTER_NAME}-{conventions.STATE_BACKEND}'
    assert _policy(tenancy, name) == [f'Allow group {name} to manage all-resources in compartment id {recorded.id}']
    assert minted.user in tenancy.identity.keys


def test_a_recorded_compartment_the_tenancy_does_not_have_is_refused(
    seeded: KdbxStore, tenancy: Tenancy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        conventions.OCI_COMPARTMENTS,
        conventions.STATE_BACKEND,
        conventions.Compartment(consumer=conventions.STATE_BACKEND, name='kluster', ocid='ocid1.compartment.oc1..gone'),
    )

    # Refused rather than created: a recorded OCID that answers to nothing is a
    # fact gone stale, and making a second compartment beside it would hide
    # that behind a key confined to somewhere the stack does not act.
    with pytest.raises(oci_iam.CredentialRejected, match='none of that name'):
        _ = oci_iam.mint_api_key(seeded, consumer=conventions.STATE_BACKEND, seed_entry=SEED_ENTRY, connect=tenancy)
    assert tenancy.identity.compartments == {}


def test_a_compartment_whose_ocid_disagrees_with_the_mapping_is_refused(
    seeded: KdbxStore, tenancy: Tenancy, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _named('compartment', 'kluster')
    tenancy.identity.compartments[live.id] = live
    monkeypatch.setitem(
        conventions.OCI_COMPARTMENTS,
        conventions.STATE_BACKEND,
        conventions.Compartment(consumer=conventions.STATE_BACKEND, name=live.name, ocid='ocid1.compartment.oc1..old'),
    )

    with pytest.raises(oci_iam.CredentialRejected, match='is stale'):
        _ = oci_iam.mint_api_key(seeded, consumer=conventions.STATE_BACKEND, seed_entry=SEED_ENTRY, connect=tenancy)


def test_a_compartment_named_on_the_command_line_is_taken_as_given(seeded: KdbxStore, tenancy: Tenancy) -> None:
    drill = 'ocid1.compartment.oc1..drill'

    _ = oci_iam.mint_api_key(
        seeded, consumer=conventions.PHYSICAL, compartment_id=drill, seed_entry=SEED_ENTRY, connect=tenancy
    )

    # The override is for a tenancy that is not this estate's, where neither
    # the names nor the OCIDs `conventions` records mean anything -- so nothing
    # is looked up, nothing is created, and nothing is compared.
    assert tenancy.identity.compartments == {}
    name = f'{conventions.CLUSTER_NAME}-{conventions.PHYSICAL}'
    assert _policy(tenancy, name) == [f'Allow group {name} to manage all-resources in compartment id {drill}']


def test_a_mint_converges_the_seeds_own_policy_before_it_acts(seeded: KdbxStore, tenancy: Tenancy) -> None:
    seed_policy = next(policy for policy in tenancy.identity.policies.values() if policy.name == oci_iam.SEED_NAME)
    # A seed whose policy predates the compartment statement: it can still
    # write policy in the tenancy, which is what makes this self-repairing.
    seed_policy.statements = [line for line in oci_iam.STATEMENTS if 'compartments' not in line]

    _ = oci_iam.mint_api_key(seeded, consumer=conventions.PHYSICAL, seed_entry=SEED_ENTRY, connect=tenancy)

    # The converge rides the mint rather than living in a verb of its own: the
    # mint is the one command that needs the statement, and a converge an
    # operator has to remember is one that is forgotten -- with the failure
    # landing as a refusal to create the compartment, mid-run.
    assert seed_policy.statements == list(oci_iam.STATEMENTS)


@dataclass
class LaggingIdentity(FakeIdentity):
    """An identity endpoint the new key has not reached yet.

    Listing keys as the freshly minted user fails with 401 for a while, the
    way the real service does before the key propagates.
    """

    lag: int = 2
    denied: int = 0

    def check_read_lag(self) -> None:
        # What lags is the freshly uploaded key -- on the legacy and the
        # domains endpoint alike -- so an empty user (the pre-mint quota
        # check) answers normally.
        if any(self.keys.values()) and self.denied < self.lag:
            self.denied += 1
            raise oci.exceptions.ServiceError(
                status=401,
                code='NotAuthenticated',
                headers=dict[str, str](),
                message='required information was not provided',
            )

    def list_api_keys(self, user_id: str) -> Response:
        self.check_read_lag()
        return super().list_api_keys(user_id)


def test_verification_outwaits_key_propagation(
    kit: KdbxStore, root: masters.Credential, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One poll consults the domain and then the legacy fallback, so one
    # interval of patience clears two denials.
    tenancy = Tenancy(identity=LaggingIdentity(lag=4))
    naps: list[float] = []
    monkeypatch.setattr(oci_iam.time, 'sleep', naps.append)

    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # Four denials, two endpoints consulted per poll: two intervals, no more.
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
class ShimRefusals(FakeIdentity):
    """A legacy layer that refuses some of the endpoints the domain also serves.

    Which ones is a parameter because live runs have met several shapes of the
    same defect: `DeleteApiKey` refused to everyone every time, `ListApiKeys`
    refused intermittently to the key's own user minutes after serving the
    identical call, and `CreateUser` refused for a field it cannot represent.
    The domains endpoints keep working throughout, which is the whole reason
    those calls go there.
    """

    #: Endpoints refused every time.
    refuses: frozenset[str] = frozenset()
    #: Endpoints refused the first time and taken afterwards -- the shape a
    #: caller that treats one 401 as final gives up on for good.
    refuses_once: set[str] = field(default_factory=set[str])

    def check_shim(self, endpoint: str) -> None:
        super().check_shim(endpoint)
        if endpoint in self.refuses_once:
            self.refuses_once.discard(endpoint)
            raise _shim_refusal(endpoint)
        if endpoint in self.refuses:
            raise _shim_refusal(endpoint)


@dataclass
class RefusingIdentity(ShimRefusals):
    """A tenancy whose legacy identity layer refuses key deletion outright.

    What an identity-domains tenancy does with
    `DELETE /users/{id}/apiKeys/{fingerprint}`: refused for the account root
    and for the key's own user alike, every time. Its domains endpoint still
    works, which is the whole reason deletion goes there.
    """

    refuses: frozenset[str] = frozenset({'DeleteApiKey'})


@dataclass
class UnreadableIdentity(RefusingIdentity):
    """A tenancy whose legacy layer refuses even `list_api_keys` to the seed.

    Observed live: the identical listing worked for hours as the key's own
    user, then answered 401 IdcsConversionError. The domains endpoint kept
    working throughout, which is why listing goes there when a domain is
    known.
    """

    refuses: frozenset[str] = frozenset({'DeleteApiKey', 'ListApiKeys'})


@dataclass
class HiddenDomains(FakeIdentity):
    """A tenancy that will not name its identity domains to this caller.

    Reading them is an administrator's call; a seed whose policy covers
    users, groups and policies need not have it. This is what a kit written
    before the domain attribute meets when it tries to discover one.
    """

    def list_domains(self, compartment_id: str) -> Response:
        raise oci.exceptions.ServiceError(
            status=404,
            code='NotAuthorizedOrNotFound',
            headers=dict[str, str](),
            message='Authorization failed or requested resource not found',
        )


@dataclass
class SealedIdentity(RefusingIdentity, HiddenDomains):
    """Both refusals at once: no legacy delete, and no domain to reach.

    The only state in which a superseded key really cannot be retired, and
    the one the console errand exists for.
    """


@dataclass
class FlakyDeletes(FakeIdentity):
    """A tenancy that refuses a whole delete attempt and takes the next one.

    Observed live: a sweep running as a key minted seconds earlier could not
    delete the key it superseded, and the identical delete for the same
    fingerprint and user succeeded moments later. The refusal carries no
    `code` at all -- status and message are all it says -- which is why a log
    line naming only the code says nothing.

    The count is in calls, not attempts, because one attempt is two calls:
    the domain delete and then the legacy fallback. Two is therefore the
    smallest refusal that is a refusal of the operation rather than of one of
    its endpoints -- and the operation is what the live failure lost.
    """

    refusals: int = 2
    refused: int = 0

    def check_delete_flake(self) -> None:
        if self.refused < self.refusals:
            self.refused += 1
            raise oci.exceptions.ServiceError(
                status=401,
                code=None,
                headers=dict[str, str](),
                message='The required information to complete authentication was not provided.',
            )


def _same_tenancy(identity: FakeIdentity) -> FakeIdentity:
    """The same tenancy, seen by a caller that may read its identity domains.

    State is shared rather than copied: what differs between the two views is
    the caller, not the account.
    """
    return FakeIdentity(
        groups=identity.groups,
        users=identity.users,
        policies=identity.policies,
        memberships=identity.memberships,
        keys=identity.keys,
        uploaded=identity.uploaded,
    )


def test_the_row_records_the_identity_domain(kit: KdbxStore, tenancy: Tenancy, root: masters.Credential) -> None:
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # Discovered with the account root, at the one moment it is in hand, and
    # stored beside the tenancy OCID: rotation retires keys through the
    # domains API and must never need the root to find it.
    assert kit.attribute(SEED_ENTRY, entries.OCI_DOMAIN_ATTRIBUTE) == DOMAIN_URL
    assert oci_iam.load_domain(kit, SEED_ENTRY) == DOMAIN_URL


def test_keys_are_retired_through_the_domain_not_the_legacy_call(
    kit: KdbxStore, root: masters.Credential, tmp_path: Path
) -> None:
    tenancy = Tenancy(identity=RefusingIdentity())
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    second = KdbxStore.create(tmp_path / 'second.kdbx', PASSWORD)

    user_id = oci_iam.create_seed(root=root, seeds=second, seed_entry=SEED_ENTRY, connect=tenancy)

    # The legacy delete is refused for everyone in this tenancy, so the
    # orphan can only have gone through the domain's self-service endpoint --
    # authorized as the seed's own new key.
    _, _, private_pem = oci_iam.load_seed(second, SEED_ENTRY)
    assert tenancy.identity.keys[user_id] == [oci_iam.fingerprint(private_pem)]
    assert tenancy.domain_connections[-1] == (DOMAIN_URL, user_id, oci_iam.fingerprint(private_pem))


def test_rotation_retires_through_the_domain_the_row_names(
    kit: KdbxStore, root: masters.Credential, tmp_path: Path
) -> None:
    tenancy = Tenancy(identity=RefusingIdentity())
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)

    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, into=successor, connect=tenancy)

    # No account root is borrowed: the URL comes off the row, and the
    # predecessor goes even though the legacy call would refuse it.
    assert tenancy.identity.keys[user_id] == [current]
    assert successor.attribute(SEED_ENTRY, entries.OCI_DOMAIN_ATTRIBUTE) == DOMAIN_URL


def test_a_row_written_before_the_domain_finds_it_at_rotation(kit: KdbxStore, root: masters.Credential) -> None:
    hidden = HiddenDomains()
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=Tenancy(identity=hidden))
    assert entries.OCI_DOMAIN_ATTRIBUTE not in kit.attributes(SEED_ENTRY)

    tenancy = Tenancy(identity=_same_tenancy(hidden))
    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # Where the tenancy will tell the seed, no repair is needed: rotation
    # discovers the domain, retires through it, and writes it onto the row.
    assert tenancy.identity.keys[user_id] == [current]
    assert kit.attribute(SEED_ENTRY, entries.OCI_DOMAIN_ATTRIBUTE) == DOMAIN_URL


def test_a_domain_that_cannot_be_discovered_names_the_repair(
    kit: KdbxStore, root: masters.Credential, caplog: pytest.LogCaptureFixture
) -> None:
    tenancy = Tenancy(identity=SealedIdentity())
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    caplog.clear()

    _ = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # The one thing a rotation must not do is require the account root, so a
    # tenancy that will not answer the seed gets a warning naming the errand
    # rather than a failure -- and the successor still stands.
    assert 'credentials seed oci domain' in caplog.text
    assert entries.OCI_DOMAIN_ATTRIBUTE not in kit.attributes(SEED_ENTRY)


def test_the_repair_reads_the_domain_with_the_account_root(kit: KdbxStore, root: masters.Credential) -> None:
    sealed = SealedIdentity()
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=Tenancy(identity=sealed))

    url = oci_iam.adopt_domain(kit, seed_entry=SEED_ENTRY, root=root, connect=Tenancy(identity=_same_tenancy(sealed)))

    # One command, one borrowing of the root, and the row can retire its own
    # keys from then on.
    assert url == DOMAIN_URL
    assert oci_iam.load_domain(kit, SEED_ENTRY) == DOMAIN_URL


def test_a_refused_sweep_does_not_fail_the_bring_up(kit: KdbxStore, root: masters.Credential, tmp_path: Path) -> None:
    tenancy = Tenancy(identity=SealedIdentity())
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
    tenancy = Tenancy(identity=SealedIdentity())
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # The successor is minted and stored; the predecessor survives as a
    # console errand rather than blocking the rotation.
    _, _, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)
    assert oci_iam.fingerprint(private_pem) == current


def test_a_transient_refusal_is_outwaited_instead_of_becoming_an_errand(
    kit: KdbxStore, root: masters.Credential, caplog: pytest.LogCaptureFixture
) -> None:
    flaky = FlakyDeletes()
    tenancy = Tenancy(identity=flaky)
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    caplog.clear()

    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # The refusal really happened, and the predecessor is gone anyway: a
    # sweep that gave up on the first answer would have left it standing and
    # sent an operator to the console for a key the next call would have
    # taken.
    assert flaky.refused == flaky.refusals
    assert flaky.keys[user_id] == [current]
    assert 'delete it in the console' not in caplog.text


def test_a_refusal_that_outlives_the_deadline_is_logged_with_what_it_said(
    kit: KdbxStore, root: masters.Credential, caplog: pytest.LogCaptureFixture
) -> None:
    tenancy = Tenancy(identity=SealedIdentity())
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    caplog.clear()

    _ = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # Status and message, not the code alone: an identity-domains refusal
    # carries no code, so a line naming only that one says nothing about what
    # was refused or why.
    assert 'delete it in the console' in caplog.text
    assert 'HTTP 401' in caplog.text
    assert 'Client is unauthorized' in caplog.text


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
    tenancy = Tenancy(identity=SealedIdentity())
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


@dataclass
class Ledger:
    """One run's remote calls, and where it is to stop.

    Shared by every client a run is handed, because "the k-th call" has to
    mean the k-th call of the run: the legacy service and the identity domain
    are two endpoints of one operation, and a sweep that counted them apart
    would leave the crossing between them uncovered.
    """

    fail_at: int | None = None
    when: str = 'after'
    calls: list[str] = field(default_factory=list[str])


class Interruptible:
    """A client that counts remote calls and stops the run at the k-th.

    Two ways to stop, because they leave different worlds behind: `before`
    never reaches the service, `after` lets the service act and loses the
    answer -- which is the one that strands a key whose private half the
    caller never got to store.
    """

    def __init__(self, target: Any, ledger: Ledger) -> None:
        self.target: Any = target
        self.ledger: Ledger = ledger

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.target, name)

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.ledger.calls.append(name)
            fatal = len(self.ledger.calls) == self.ledger.fail_at
            if fatal and self.ledger.when == 'before':
                raise Interrupted(f'call {self.ledger.fail_at} ({name}) never reached the service')
            result = target(*args, **kwargs)
            if fatal:
                raise Interrupted(f'call {self.ledger.fail_at} ({name}) reached the service; the answer was lost')
            return result

        return counted


@dataclass
class FaultyTenancy:
    """`Tenancy`, with every client it hands out wired through one ledger."""

    identity: FakeIdentity = field(default_factory=FakeIdentity)
    connections: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    fail_at: int | None = None
    when: str = 'after'
    ledger: Ledger = field(default_factory=Ledger)

    def __post_init__(self) -> None:
        self.ledger = Ledger(fail_at=self.fail_at, when=self.when)

    def __call__(
        self, tenancy: str, user: str, private_key_pem: str, *, domain_url: str | None = None
    ) -> Interruptible:
        self.connections.append((user, oci_iam.fingerprint(private_key_pem)))
        if domain_url is not None:
            return Interruptible(FakeDomain(identity=self.identity, user_id=user, admin=user == ROOT_USER), self.ledger)
        return Interruptible(self.identity, self.ledger)

    @property
    def counted(self) -> int:
        return len(self.ledger.calls)


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


@dataclass
class ShimlessIdentity(ShimRefusals):
    """A tenancy whose legacy identity layer answers nothing the domain owns.

    Users, groups, memberships and API keys live in the identity domain, and
    the legacy endpoints for them are a conversion shim over it that a live
    bring-up found unreliable in every direction. This is that tenancy with
    the shim taken away entirely: whatever still works here worked through the
    domain, which is what makes it worth asserting.

    Policies, compartments and `list_domains` are IAM's own concepts rather
    than the domain's, so they keep answering.
    """

    refuses: frozenset[str] = frozenset(SHIMMED_ENDPOINTS)


def test_the_seed_is_built_in_the_identity_domain(kit: KdbxStore, root: masters.Credential) -> None:
    tenancy = Tenancy(identity=ShimlessIdentity())

    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # Nothing the domain owns could have come from the legacy layer here, so
    # the user, the group, the membership and the first key are each proof
    # that the call went to the domain -- named one by one, because each is a
    # separate endpoint that has to be right on its own.
    assert tenancy.policy.served.count('create_user') == 1
    assert tenancy.policy.served.count('create_group') == 1
    assert tenancy.policy.served.count('patch_group') == 1
    assert tenancy.policy.served.count('create_api_key') == 1
    assert [user.name for user in tenancy.identity.users.values()] == [oci_iam.SEED_NAME]
    assert (user_id, next(iter(tenancy.identity.groups))) in tenancy.identity.memberships
    _, _, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)
    assert tenancy.identity.keys[user_id] == [oci_iam.fingerprint(private_pem)]
    # The policy is IAM's own concept, not the domain's, and stays legacy.
    assert [policy.statements for policy in tenancy.identity.policies.values()] == [list(oci_iam.STATEMENTS)]


def test_the_seed_is_reused_rather_than_remade_in_the_domain(kit: KdbxStore, root: masters.Credential) -> None:
    tenancy = Tenancy(identity=ShimlessIdentity())
    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # The domain search is what makes the second run a no-op, so it has to
    # find what the first run made rather than collide with it.
    assert tenancy.policy.served.count('create_user') == 1
    assert tenancy.policy.served.count('create_group') == 1
    assert len(tenancy.identity.users) == len(tenancy.identity.groups) == 1


def test_rotation_registers_the_successor_as_the_seed_itself(kit: KdbxStore, root: masters.Credential) -> None:
    tenancy = Tenancy(identity=ShimlessIdentity())
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # The seed is not a domain administrator, so the administrative create
    # would be refused it and the legacy upload does not exist here: the only
    # way a successor can stand is the self-service endpoint, which takes no
    # subject because it can only ever mean the caller.
    assert tenancy.policy.served.count('create_my_api_key') == 1
    # And the administrative endpoint was used once in the whole story: by the
    # account root at bring-up, for a user that was not its own.
    assert tenancy.policy.served.count('create_api_key') == 1
    assert tenancy.identity.keys[user_id] == [current]


def test_a_tenancy_without_identity_domains_uses_the_legacy_calls(
    kit: KdbxStore, root: masters.Credential, tmp_path: Path
) -> None:
    # `HiddenDomains` answers no domain to anyone, which is also what a
    # tenancy that simply has none looks like from here.
    tenancy = Tenancy(identity=HiddenDomains())

    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)
    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, into=successor, connect=tenancy)

    # Every call fell back to the legacy service and the whole flow still
    # completes: no domain endpoint was ever addressed, and the user, group,
    # membership, key and rotation are all there.
    assert tenancy.domain_connections == []
    assert tenancy.policy.served == []
    assert [user.name for user in tenancy.identity.users.values()] == [oci_iam.SEED_NAME]
    assert [group.name for group in tenancy.identity.groups.values()] == [oci_iam.SEED_NAME]
    assert (user_id, next(iter(tenancy.identity.groups))) in tenancy.identity.memberships
    assert tenancy.identity.keys[user_id] == [current]
    assert entries.OCI_DOMAIN_ATTRIBUTE not in successor.attributes(SEED_ENTRY)


def test_a_domain_that_refuses_a_call_falls_back_to_the_legacy_one(kit: KdbxStore, root: masters.Credential) -> None:
    # The refusal seen live runs both ways: the domain has answered 401 to a
    # call the legacy shim then took. Refusing user creation is the sharpest
    # case, because everything downstream is then named the legacy way.
    tenancy = Tenancy(policy=DomainPolicy(always=frozenset({'create_user'})))

    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    # The user came from the legacy call, so it is known only by its OCID --
    # which the domain will not accept as a group member or as a key's
    # subject, so those fall back in turn and the seed still stands.
    assert [user.name for user in tenancy.identity.users.values()] == [oci_iam.SEED_NAME]
    assert (user_id, next(iter(tenancy.identity.groups))) in tenancy.identity.memberships
    _, _, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)
    assert tenancy.identity.keys[user_id] == [oci_iam.fingerprint(private_pem)]


def test_a_domain_that_refuses_once_is_not_given_up_on(
    kit: KdbxStore, root: masters.Credential, tmp_path: Path
) -> None:
    # The refusal that is not an answer: the identical call, same credential
    # and same subject, taken moments later. A run that treated the first 401
    # as final would leave the tenancy half legacy for good.
    tenancy = Tenancy(policy=DomainPolicy(once={'create_group', 'create_api_key'}))

    _ = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    second = KdbxStore.create(tmp_path / 'second.kdbx', PASSWORD)
    user_id = oci_iam.create_seed(root=root, seeds=second, seed_entry=SEED_ENTRY, connect=tenancy)

    # Both refusals really happened and the legacy call carried the first run;
    # the second run found the domain willing and used it.
    assert tenancy.policy.once == set()
    assert tenancy.policy.served.count('create_api_key') == 1
    _, _, private_pem = oci_iam.load_seed(second, SEED_ENTRY)
    assert tenancy.identity.keys[user_id] == [oci_iam.fingerprint(private_pem)]


def test_the_self_service_listing_is_not_used_for_another_users_keys(tenancy: Tenancy) -> None:
    # `Me` endpoints report the caller and nobody else, so the subject decides
    # which half of the domains API answers. Answering about a different user
    # with the caller's own keys would be worse than failing: the quota check
    # would pass on the wrong user's count and the sweep would retire the
    # wrong keys.
    private_pem, _ = oci_iam.generate_key()
    tenancy.identity.keys[ROOT_USER] = ['the:root:key']
    tenancy.identity.keys['ocid1.user.oc1..kluster-seed'] = ['the:seed:key']
    iam = oci_iam.Iam.authorize(TENANCY, ROOT_USER, private_pem, connect=tenancy, domain_url=DOMAIN_URL)

    assert iam.api_keys('ocid1.user.oc1..kluster-seed') == ['the:seed:key']
    assert iam.api_keys(ROOT_USER) == ['the:root:key']
    # Administrative for somebody else, self-service for the caller, and the
    # legacy shim for neither.
    assert tenancy.policy.served == ['list_api_keys', 'list_my_api_keys']
    assert tenancy.identity.shim_calls == []


def test_another_users_keys_are_read_through_the_domain_not_the_shim(kit: KdbxStore, root: masters.Credential) -> None:
    # The live rotation drill failed here, on a read: the legacy listing
    # answered 401 IdcsConversionError minutes after serving the identical
    # call. Both halves of the domains API can list keys, so the subject
    # being somebody else is no reason to be exposed to the shim.
    tenancy = Tenancy(identity=UnreadableIdentity())
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    iam = oci_iam.Iam.authorize(TENANCY, ROOT_USER, root['private-key'], connect=tenancy, domain_url=DOMAIN_URL)

    _, _, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)
    assert iam.api_keys(user_id) == [oci_iam.fingerprint(private_pem)]


def test_another_users_key_is_retired_through_the_domain_not_the_shim(kit: KdbxStore, root: masters.Credential) -> None:
    # The administrative delete names the key by the id the administrative
    # listing gave it, so the two are one operation; the legacy delete is
    # refused to everyone in a tenancy with domains.
    tenancy = Tenancy(identity=RefusingIdentity())
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    iam = oci_iam.Iam.authorize(TENANCY, ROOT_USER, root['private-key'], connect=tenancy, domain_url=DOMAIN_URL)
    _, _, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)

    iam.delete_api_key(user_id, oci_iam.fingerprint(private_pem))

    assert tenancy.identity.keys[user_id] == []
    assert tenancy.policy.served[-2:] == ['list_api_keys', 'delete_api_key']


def test_a_domain_resource_without_an_ocid_is_refused() -> None:
    # The OCID goes into the row and into every signing configuration that
    # follows; a SCIM id in its place would be stored, would look like an
    # answer, and would fail at the next authentication.
    with pytest.raises(oci_iam.CredentialRejected, match='without an OCID'):
        _ = oci_iam.Principal.domain(DomainResource(id='user-scim-id', ocid=''), kind='user')


def test_rotation_survives_a_legacy_layer_that_refuses_even_reads(
    kit: KdbxStore, root: masters.Credential, tmp_path: Path
) -> None:
    tenancy = Tenancy(identity=UnreadableIdentity())
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)

    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, into=successor, connect=tenancy)

    # Listing, verification and retirement all went through the domain: the
    # legacy layer answered 401 to every read and delete throughout.
    assert tenancy.identity.keys[user_id] == [current]
    _, _, private_pem = oci_iam.load_seed(successor, SEED_ENTRY)
    assert oci_iam.fingerprint(private_pem) == current


def test_a_healthy_domain_is_never_asked_through_the_shim(
    kit: KdbxStore, tenancy: Tenancy, root: masters.Credential, tmp_path: Path
) -> None:
    user_id = oci_iam.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)
    successor = KdbxStore.create(tmp_path / 'successor.kdbx', PASSWORD)
    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, into=successor, connect=tenancy)

    # A bring-up and two rotations' worth of reads and writes, and not one of
    # them touched a conversion-shim endpoint: users, groups, membership and
    # every API-key read, write and delete were served by the domain. This is
    # what "the legacy client is for IAM-native concepts only" means when a
    # domain answers -- the shim is a fallback, never a step on the happy path.
    assert tenancy.identity.shim_calls == []
    assert tenancy.identity.keys[user_id] == [current]
    # Policies are IAM's own concept and are not shim-converted, so the legacy
    # client is still the one that wrote them.
    assert [policy.statements for policy in tenancy.identity.policies.values()] == [list(oci_iam.STATEMENTS)]


def _survives_a_bring_up_and_a_rotation(tenancy: Tenancy) -> None:
    """Build the seed and rotate it, and assert exactly one key is left standing.

    The whole flow rather than a single call, because which endpoint answers
    decides what shape the principal downstream has: a user found the legacy
    way is known only by its OCID, which the domain then refuses as a group
    member and as a key's subject. A refusal is survivable only if everything
    after it survives too.
    """
    kit = MemoryKit()
    user_id = oci_iam.create_seed(root=_fresh_root(), seeds=kit, seed_entry=SEED_ENTRY, connect=tenancy)

    current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY, connect=tenancy)

    assert tenancy.identity.keys[user_id] == [current]
    assert oci_iam.fingerprint(oci_iam.load_seed(kit, SEED_ENTRY)[2]) == current


@pytest.mark.parametrize('endpoint', SHIMMED_ENDPOINTS)
@pytest.mark.parametrize('shape', ('always', 'once'))
def test_a_refused_shim_endpoint_does_not_stop_the_seed(endpoint: str, shape: str, pooled_keys: None) -> None:
    """No conversion-shim endpoint is load-bearing while the domain answers.

    The scan is over every shim-converted endpoint and both shapes the refusal
    takes live: refused for good, and refused once and served on the next
    identical call. A tenancy whose domain answers owes the shim nothing, so
    each of these is either never reached at all or falls back onto a call
    that is -- which is what the live drill's failed verification was not.
    """
    once = shape == 'once'
    refusals = ShimRefusals(
        refuses=frozenset() if once else frozenset({endpoint}),
        refuses_once={endpoint} if once else set[str](),
    )

    _survives_a_bring_up_and_a_rotation(Tenancy(identity=refusals))


@pytest.mark.parametrize('operation', DOMAIN_OPERATIONS)
def test_a_refused_domain_operation_falls_back_to_the_legacy_call(operation: str, pooled_keys: None) -> None:
    """The fallback runs the other way too, one domain operation at a time.

    Either side has been seen to refuse a call the other then accepted, so the
    direction only says which client is tried first. The scan is what keeps
    that true as endpoints are added: a domains call whose legacy counterpart
    is missing strands the flow here rather than in a bring-up.
    """
    tenancy = Tenancy(policy=DomainPolicy(always=frozenset({operation})))

    _survives_a_bring_up_and_a_rotation(tenancy)

    assert operation not in tenancy.policy.served
