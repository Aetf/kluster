"""A fake OCI tenancy, shared by the suites that mint against its IAM.

Its own named module rather than a `conftest`, for the reason `memory_kit` is
one: test modules import it, and every directory with tests may have a
`conftest` of its own on `sys.path`.

What the fake encodes is authorization, because that is what the modules
under test are about:

-   the tenancy remembers the principal behind every call — the user and the
    key that signed it — so a test asserts *who* deleted a key, and not merely
    that it went. Which session a sweep runs as is the whole of more than one
    defect, and the tenancy's state afterwards cannot show it: every principal
    that may delete a key leaves the same state behind;
-   the identity domain serves two halves: the self-service endpoints act on
    the caller's own user and nothing else, and the administrative ones are
    refused to everyone but the account root, the domain administrator;
-   a key signs only while it is on its user, so a key never uploaded or
    already deleted is refused; and a tenancy may accept signatures from
    only some keys, which is how a key the control plane registered but the
    signing path refuses looks.

What every session carries lives here: what the identity domain refuses
(`DomainPolicy`) and whose signatures the tenancy accepts (`Tenancy.signers`).
Every other refusal the live service has been seen to make is a `FakeIdentity`
subclass in `test_oci_iam`, the one suite that exercises them — today a key
that lags, a legacy shim that refuses, domains hidden from the caller, and a
delete refused once and taken the next time.
"""

# The SDK ships no stubs; the same waiver `oci_iam.py` itself carries.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import oci

from kluster import conventions
from kluster.scripts.credentials import oci_iam

TENANCY = 'ocid1.tenancy.oc1..tenancy'
ROOT_USER = 'ocid1.user.oc1..root'
DOMAIN_URL = 'https://idcs-000.identity.oraclecloud.com:443'

#: How many policies one unfiltered `list_policies` answers with. The real
#: service pages; this is the smallest page that makes a walk and a filtered
#: lookup behave differently.
POLICY_PAGE = 1

#: Every way a key comes off a user, as the fake's endpoints name them: the
#: domain's self-service and administrative deletes, and the legacy one, which
#: shares the administrative delete's name.
DELETIONS = frozenset({'delete_my_api_key', 'delete_api_key'})

#: Every way a user's keys are read, named the same way.
KEY_LISTINGS = frozenset({'list_my_api_keys', 'list_api_keys'})


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


def named(kind: str, name: str) -> Named:
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


@dataclass
class DomainPolicy:
    """What the identity domain refuses, and for how long.

    Refusals are per operation because that is how they were met live: one
    endpoint answering while its neighbor on the same host and the same
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
        shim-converted endpoints is one fact about the service: a subclass
        that refuses some of them refuses here, and a test that asserts a call
        never reached the shim reads `shim_calls`.
        """
        self.shim_calls.append(endpoint)

    def hold(self, compartment: conventions.Compartment) -> None:
        """Give the tenancy the compartment `conventions` records, under its OCID."""
        assert compartment.ocid is not None
        self.compartments[compartment.ocid] = Named(id=compartment.ocid, name=compartment.name)

    def list_groups(self, compartment_id: str, name: str | None = None) -> Response:
        self.check_shim('ListGroups')
        return Response([group for group in self.groups.values() if name in (None, group.name)])

    def create_group(self, details: Any) -> Response:
        self.check_shim('CreateGroup')
        group = named('group', details.name)
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
        user = named('user', details.name)
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
        made = named('compartment', details.name)
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
        made = named('group', group.display_name)
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
        made = named('user', user.user_name)
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


class Call(NamedTuple):
    """One remote call, and the principal that signed it.

    `args` is every argument the call was made with, by the name the fake
    endpoint gives it, whether the caller passed it by position or by keyword.
    """

    user: str
    fingerprint: str
    operation: str
    args: Mapping[str, Any]


def _not_authenticated(user: str) -> oci.exceptions.ServiceError:
    """What the service answers a signature it does not accept."""
    return oci.exceptions.ServiceError(
        status=401,
        code='NotAuthenticated',
        headers=dict[str, str](),
        message=f'the signature of {user} was not accepted',
    )


class Signed:
    """One session's client: a fake endpoint, signing as one key.

    Every call is written into the tenancy's `calls` under the user and the
    key the session was opened with, before the endpoint acts, so a call the
    endpoint then refuses is on the record too.
    """

    def __init__(self, endpoint: FakeIdentity | FakeDomain, *, tenancy: Tenancy, user: str, fingerprint: str) -> None:
        self.endpoint: FakeIdentity | FakeDomain = endpoint
        self.tenancy: Tenancy = tenancy
        self.user: str = user
        self.fingerprint: str = fingerprint

    def __getattr__(self, name: str) -> Any:
        operation = getattr(self.endpoint, name)

        def signed(*args: Any, **kwargs: Any) -> Any:
            bound = inspect.signature(operation).bind(*args, **kwargs)
            self.tenancy.calls.append(Call(self.user, self.fingerprint, name, dict(bound.arguments)))
            if not self.tenancy.accepts(self.user, self.fingerprint):
                raise _not_authenticated(self.user)
            return operation(*args, **kwargs)

        return signed


@dataclass
class Tenancy:
    """A connect function whose sessions all reach one fake tenancy.

    Every session is a view of the same account, and every call made through
    one is recorded with the principal that signed it (`calls`), which is how
    a test asserts who retired a key and that a minted key was used before
    anything depended on it. Opening a session is recorded apart
    (`connections`, and `domain_connections` for the domains endpoint),
    because carrying a credential into a tenancy is a fact of its own even
    where no call follows.
    """

    identity: FakeIdentity = field(default_factory=FakeIdentity)
    connections: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    domain_connections: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    #: Every call any session made, legacy and domain alike, in order.
    calls: list[Call] = field(default_factory=list[Call])
    #: What this tenancy's domain refuses, and to whom. Domain-admin rights
    #: belong to the account root; every other caller gets the self-service
    #: half and nothing more.
    policy: DomainPolicy = field(default_factory=DomainPolicy)
    #: The key fingerprints whose signatures the tenancy accepts, or None for
    #: every key. Any other key is answered 401 on every call, on both
    #: endpoints: a key registered on the control plane that the signing path
    #: refuses. Keyed by key rather than by user because that is what the
    #: service authenticates, and a user's old and new keys differ here.
    signers: frozenset[str] | None = None

    def accepts(self, user: str, fingerprint: str) -> bool:
        """Whether a call signed as `user` with the key `fingerprint` authenticates.

        A key authenticates while it is on its user: one never uploaded, or
        already deleted, is refused, which is what makes a session that
        deleted the key it signs with unable to go on. The account root is the
        exception, because the root is not a user this fake mints for: its key
        comes from the operator, and the tenancy holds none of the root's keys
        to check it against.
        """
        if self.signers is not None and fingerprint not in self.signers:
            return False
        return user == ROOT_USER or fingerprint in self.identity.keys.get(user, [])

    def __call__(self, tenancy: str, user: str, private_key_pem: str, *, domain_url: str | None = None) -> Signed:
        signed_as = oci_iam.fingerprint(private_key_pem)
        if domain_url is not None:
            self.domain_connections.append((domain_url, user, signed_as))
            domain = FakeDomain(identity=self.identity, user_id=user, admin=user == ROOT_USER, policy=self.policy)
            return Signed(domain, tenancy=self, user=user, fingerprint=signed_as)
        self.connections.append((user, signed_as))
        return Signed(self.identity, tenancy=self, user=user, fingerprint=signed_as)

    def made(self, operations: frozenset[str]) -> list[Call]:
        """The calls to any of `operations`, in the order they were made."""
        return [call for call in self.calls if call.operation in operations]
