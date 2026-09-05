"""The OCI credential family (docs/credentials.md §2–§3).

Three credentials, three lifetimes:

-   the **account root API key** — an account root (`masters.py`), belonging
    to a user who may manage users, groups and policies in the tenancy. The
    key that provisions instances is not this: it can launch a machine and
    cannot see an IAM user, so it can create neither the seed's user nor the
    seed's key;
-   the **seed API key** — offline, in the kit, belonging to a dedicated user
    in a dedicated group under a dedicated policy. It mints the per-stack
    users and their API keys, and — because that same permission covers its
    own user — its own successor;
-   a **per-consumer API key** (§3) — the same three IAM objects again, one
    name for all three, but scoped to a single compartment and never stored
    here: it goes straight from `mint_api_key` into its consumer's slot, and
    rotating it is running that command again. The compartment is part of that
    act rather than a prerequisite of it: `conventions` names one per consumer,
    and the mint creates the one that is not there yet — which is what the
    seed's `manage compartments` is for.

**An OCI API key is five things, and the row stores three** (§2): the user
OCID as `UserName`, the private key as an attachment because it is a file, and
the tenancy OCID as a protected custom attribute, since `UserName` is spoken
for. The other two are recovered rather than stored — the region is a
constant in `conventions`, and the fingerprint is a function of the public
key, so a stored copy could only ever disagree with the key it describes.

The row carries one more attribute, which is not part of the key: the
tenancy's **identity domain URL**. A tenancy that has identity domains
refuses the legacy `DELETE /users/{id}/apiKeys/{fingerprint}` outright
(`IdcsConversionError: Client is unauthorized`), for the account root as
much as for the key's own user, so retiring a key goes through the
identity-domains API instead — and that API is addressed by a per-tenancy
endpoint rather than by region. It is discovered once, with the account root,
and stored beside the tenancy OCID so that rotation never needs the root.

The password field stays empty here, which no other row does. The alternative
is a copy of the PEM in a field that KeePassXC will happily reveal in a
listing, next to the attachment that already holds it.

**Which identity API a call uses.** A tenancy with identity domains keeps
users, groups and user credentials in the domain; the legacy endpoints for
them are a conversion shim over it, and the shim refuses — sometimes always,
sometimes intermittently, and sometimes only for fields it cannot represent.
So anything the domain owns goes through `IdentityDomainsClient`: users,
groups, group membership and API keys. The legacy `IdentityClient` keeps two
jobs, and only those: the concepts that are IAM's own rather than the
domain's — policies, compartments, `list_domains` — and being the whole of the
identity API in a tenancy that has no domains, where every call below falls
back to it unchanged.

The fallback is bidirectional rather than a one-way migration. Both sides have
been seen to refuse a call the other then took, so a refusal from the domain
is a reason to try the legacy call, not a reason to stop; the direction only
says which one is tried first.

Within the domains API, a call acts on the caller's own user through the
self-service (`My*`) endpoints, which authorize on authentication alone, and
on anybody else's through the administrative ones, which need domain-admin
rights. The account root has those rights; the seed does not, which is why
everything the seed does to itself is self-service.
"""

# The OCI SDK is generated and ships no type information; this module is the
# whole of its untyped surface here, and everything it exports is annotated.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import oci
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ... import conventions
from ...conventions import CLUSTER_NAME, OCI_SEED_USER_EMAIL, Compartment
from ...lib import templates
from . import entries, masters
from .delivery import Delivery
from .kdbx import KdbxStore
from .masters import CredentialRejected

log = logging.getLogger(__name__)

#: The dedicated user, group and policy the seed key belongs to. One name for
#: all three: they exist only for each other, and a console reading them
#: sideways should not have to work out which is which.
SEED_NAME = 'kluster-seed'

#: A freshly uploaded key reaches the identity servers eventually, not
#: immediately: used right away it fails with 401 NotAuthenticated (the
#: Terraform provider has the same defect, oracle/terraform-provider-oci#2383,
#: papered over there with a sleep). The verification below is therefore also
#: the wait -- it polls the one condition that matters, "the key
#: authenticates", rather than guessing a delay -- and this is how long it is
#: allowed to keep not happening before the 401 is treated as real.
PROPAGATION_DEADLINE = 180.0
PROPAGATION_INTERVAL = 5.0

#: The identity service caps a user at three API keys, and minting comes
#: before revoking (a rotation must never stand without a working key) --
#: so room is made before minting, and a full user none of whose keys can
#: go is an error naming the console errand rather than a quota 400.
KEY_QUOTA = 3

#: The policies, in OCI's own policy language, one statement per line. They
#: live in files rather than in string literals for the reason every other
#: foreign configuration language in this repository does
#: (`docs/style/python.md`), and are rendered through the one mechanism
#: (`kluster.lib.templates`).
SEED_POLICY = 'templates/seed-policy.oci.j2'
CONSUMER_POLICY = 'templates/consumer-policy.oci.j2'

#: The package the two policy files sit beside, for `importlib.resources`.
_PACKAGE = 'kluster.scripts.credentials'


@dataclass(frozen=True)
class _SeedPolicyParams:
    group: str


@dataclass(frozen=True)
class _ConsumerPolicyParams:
    group: str
    compartment_id: str


def _statements(policy: str, params: object) -> tuple[str, ...]:
    """One rendered policy file as the statement tuple the API takes.

    A statement per non-blank line, so the file reads the way the console
    shows it and the tuple is a shape of this program rather than of the file.
    """
    rendered = templates.render(_PACKAGE, policy, params)
    return tuple(line.strip() for line in rendered.splitlines() if line.strip())


#: What the seed may do, and the whole of it. Managing users covers their API
#: keys, which is what makes the seed mint its own successor; managing groups
#: and policies is what lets it give a per-stack user the access its stack
#: needs; managing compartments is what lets it create the boundary that access
#: is confined to, so a consumer's compartment is an API call rather than a
#: console errand. Nothing here touches compute, storage or networking -- the
#: seed mints the credentials that do, and `manage policies in tenancy` is the
#: ceiling anyway: a principal that can write policy can grant itself the rest.
STATEMENTS: tuple[str, ...] = _statements(SEED_POLICY, _SeedPolicyParams(group=SEED_NAME))

#: OCI requires RSA for API keys, which is why these are generated rather than
#: derived: deterministic RSA generation was the footgun §2.2 excluded.
KEY_SIZE = 2048

#: The domains API is SCIM, and a SCIM payload names its own schema. The SDK
#: models cover the fields but not this, so the URNs are spelled out here.
USER_SCHEMA = 'urn:ietf:params:scim:schemas:core:2.0:User'
GROUP_SCHEMA = 'urn:ietf:params:scim:schemas:core:2.0:Group'
GROUP_EXTENSION_SCHEMA = 'urn:ietf:params:scim:schemas:oracle:idcs:extension:group:Group'
API_KEY_SCHEMA = 'urn:ietf:params:scim:schemas:oracle:idcs:apikey'
PATCH_SCHEMA = 'urn:ietf:params:scim:api:messages:2.0:PatchOp'


@dataclass(frozen=True)
class Identity:
    """A user, the group that holds it and the policy that empowers it.

    One name for all three: they exist only for each other, and a console
    reading them sideways should not have to work out which is which. The
    descriptions say in the console what the register says here, because a
    principal nobody can explain is a principal nobody dares delete.

    Two kinds exist. The seed's (§2) may manage users, groups and policies
    across the tenancy, which is what makes it able to mint the other kind;
    a consumer's (§3) may do anything it likes inside one compartment and
    nothing at all outside it.

    One of the provider roles credentials.md §3 describes, in this platform's
    own vocabulary.

    Here the grant is OCI's own vocabulary, policy statements, and the name is
    IAM's rather than a label: it is what all three objects are called.
    """

    #: The name all three objects carry.
    name: str
    #: The user's primary address. An identity domain refuses a user without
    #: one and requires it to be unique within the domain (`conventions`).
    email: str
    #: The register section describing this credential, quoted into every
    #: description a console shows.
    section: str
    #: What the group may do, and the whole of it.
    statements: tuple[str, ...]

    @staticmethod
    def name_for(consumer: str) -> str:
        """The one name a consumer's user, group and policy all carry.

        Derived rather than stored, so a caller that only needs to say which
        principal it is talking about -- a log line, a delivery -- does not
        have to know a compartment first.
        """
        return f'{CLUSTER_NAME}-{consumer}'

    @classmethod
    def for_consumer(cls, consumer: str, *, compartment_id: str) -> Identity:
        """The identity one consumer's own key belongs to (§3).

        The compartment is the scope, rather than a list of verbs: the user is
        an administrator of one compartment and a stranger everywhere else, so
        widening what a consumer may do is a resource it declares in its own
        compartment rather than an edit here, and the blast radius of the key
        is a boundary the console shows rather than a list to audit.
        """
        name = cls.name_for(consumer)
        return cls(
            name=name,
            email=f'{name}@{conventions.OCI_TENANCY.user_email_domain}',
            section='§3',
            statements=_statements(CONSUMER_POLICY, _ConsumerPolicyParams(group=name, compartment_id=compartment_id)),
        )

    @property
    def user_description(self) -> str:
        return f'The {self.name} API key (credentials.md {self.section})'

    @property
    def group_description(self) -> str:
        return f'Holds the {self.name} user (credentials.md {self.section})'

    @property
    def policy_description(self) -> str:
        return f'What the {self.name} key may do (credentials.md {self.section})'


#: The seed's own identity (§2). Its statements are what make it
#: self-reproducing: managing users covers their API keys, its own included.
SEED = Identity(name=SEED_NAME, email=OCI_SEED_USER_EMAIL, section='§2', statements=STATEMENTS)


class Connect(Protocol):
    """Builds a client for one API key.

    Injected so a test can drive the whole flow without a tenancy, and so
    verification can connect *as the key it just minted*. `domain_url`
    selects the API rather than the credential: given one, the client speaks
    the tenancy's identity-domains endpoints; without one, the legacy
    identity service.
    """

    def __call__(self, tenancy: str, user: str, private_key_pem: str, *, domain_url: str | None = None) -> Any: ...


def _fingerprint(public_der: bytes) -> str:
    digest = hashlib.md5(public_der, usedforsecurity=False).hexdigest()
    return ':'.join(digest[index : index + 2] for index in range(0, len(digest), 2))


def fingerprint(private_key_pem: str) -> str:
    """The fingerprint OCI computes for a key: MD5 of the public DER, colon-grouped.

    Derived on every use rather than stored (§2): it is a function of the key,
    and the only thing a stored copy can add is the chance of disagreeing.
    """
    private = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    return _fingerprint(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def fingerprint_of_public(public_key_pem: str) -> str:
    """The same fingerprint, computed from the public half alone.

    What OCI itself does with an uploaded key, which is how the value it
    returns can be checked against the key that was sent.
    """
    public = serialization.load_pem_public_key(public_key_pem.encode())
    return _fingerprint(
        public.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


@dataclass(frozen=True)
class KeyPair:
    """A fresh RSA key pair, both halves in PEM.

    Two PEM strings that a signature would not distinguish: carried together
    so that no caller can pass the private half where the public one belongs.
    """

    private_pem: str
    public_pem: str


def generate_key() -> KeyPair:
    """A fresh RSA key pair."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return KeyPair(private_pem=private_pem, public_pem=public_pem)


def _retry() -> Any:
    """Bounded retries for the propagation window after an IAM write.

    A freshly uploaded API key is accepted by the control plane before the
    signing path honours it, and a fresh user is `NotAuthorizedOrNotFound` for
    a moment after it exists. OCI exposes no readiness signal for either, so
    the SDK's own backoff is what turns the window into latency; a real
    permission problem still surfaces, just later.
    """
    return (
        oci.retry.RetryStrategyBuilder(
            max_attempts_check=True,
            max_attempts=8,
            total_elapsed_time_check=True,
            total_elapsed_time_seconds=300,
            retry_max_wait_between_calls_seconds=15,
            service_error_check=True,
            service_error_retry_config={401: ['NotAuthenticated'], 404: ['NotAuthorizedOrNotFound']},
            backoff_type=oci.retry.BACKOFF_FULL_JITTER_EQUAL_ON_THROTTLE_VALUE,
        )
        .add_service_error_check()
        .get_retry_strategy()
    )


def identity_client(tenancy: str, user: str, private_key_pem: str, *, domain_url: str | None = None) -> Any:
    """A client for one API key, with no configuration file involved.

    The key never reaches `~/.oci/config` or any other path: it is read out of
    the kit (or the secret store) and handed to the SDK in memory.

    With a `domain_url` this is the identity-domains client for that domain,
    which is a different service on a per-tenancy endpoint rather than a
    different credential; the signing key is the same either way.
    """
    config: dict[str, Any] = {
        'tenancy': tenancy,
        'user': user,
        'key_content': private_key_pem,
        'fingerprint': fingerprint(private_key_pem),
        'region': conventions.OCI_TENANCY.region,
    }
    try:
        oci.config.validate_config(config)
    except oci.exceptions.InvalidConfig as exc:
        raise CredentialRejected(f'the OCI credential is not usable: {exc}') from exc
    if domain_url is not None:
        return oci.identity_domains.IdentityDomainsClient(config, service_endpoint=domain_url, retry_strategy=_retry())
    return oci.identity.IdentityClient(config, retry_strategy=_retry())


def _why(exc: oci.exceptions.ServiceError) -> str:
    """What the service actually answered, in one phrase.

    The code alone is not enough to act on: an identity-domains refusal
    carries none (`code` is `None`) and says everything it has to say in the
    status and the message.
    """
    return f'HTTP {exc.status} {exc.code or "-"}: {exc.message}'


def _data(response: Any) -> Any:
    """Unwrap an SDK response at the one boundary that is untyped by nature."""
    return response.data


@dataclass(frozen=True)
class Principal:
    """A user or a group, under both of the names identity gives it.

    An OCID is what a signing configuration, a policy and the kit's row all
    speak, and it is the only name the legacy API has. The domains API assigns
    a second one — a SCIM id — and addresses its own resources by that, so a
    session that found a principal through the domain has to remember which
    name it may hand back to which call.

    A principal read through the legacy API has one name in both fields: there
    the OCID *is* the handle.
    """

    #: The tenancy-wide identifier: `ocid1.user.oc1..`, `ocid1.group.oc1..`.
    ocid: str
    #: What the domains API calls it, where that is known.
    handle: str

    @classmethod
    def legacy(cls, resource: Any) -> Principal:
        return cls(ocid=str(resource.id), handle=str(resource.id))

    @classmethod
    def domain(cls, resource: Any, *, kind: str) -> Principal:
        """A domains resource, refused if it will not say its OCID.

        The OCID is not a nicety here: it is what goes into the row and into
        the signing configuration of every session that follows. A SCIM id put
        in its place would be stored, would look like an answer, and would
        fail at the next authentication.
        """
        ocid = getattr(resource, 'ocid', None)
        if not ocid:
            raise CredentialRejected(
                f'the identity domain returned a {kind} without an OCID, which is the name the kit and '
                'the signing configuration need'
            )
        return cls(ocid=str(ocid), handle=str(resource.id))


@dataclass(frozen=True)
class Policy:
    """One IAM policy, under the three things converging it takes.

    Read into a record rather than handed on as the SDK answered, so that what
    the tenancy holds and what the register describes are the same shape and
    the drift between them is a comparison of two tuples.
    """

    #: The tenancy-wide identifier: `ocid1.policy.oc1..`.
    ocid: str
    #: The name the identity's user, group and policy all carry.
    name: str
    #: What the group this policy empowers may do.
    statements: tuple[str, ...]

    @classmethod
    def of(cls, resource: Any) -> Policy:
        """One policy as the legacy client answered with it."""
        return cls(
            ocid=str(resource.id),
            name=str(resource.name),
            statements=tuple(str(statement) for statement in resource.statements),
        )


@dataclass(frozen=True)
class TenancyDomain:
    """One row of the tenancy's identity-domain listing.

    Not `Domain`, which is a domain already opened with a key: this is what
    the listing says about one, carrying the three things picking one takes --
    the endpoint to address it at, whether it is the default, and a name to
    put in the error that names the ones there are.
    """

    #: The per-tenancy endpoint the domains API is addressed at.
    url: str
    #: What a console shows the domain as.
    display_name: str
    #: `DEFAULT` for the domain a user the legacy API created lands in.
    kind: str

    @classmethod
    def of(cls, resource: Any) -> TenancyDomain:
        """One domain as `list_domains` answered with it.

        The type is upper-cased here because it is compared against the SDK's
        own constant, which is upper case, and the comparison belongs with the
        value rather than at every use of it.
        """
        return cls(url=str(resource.url), display_name=str(resource.display_name), kind=str(resource.type).upper())


@dataclass(frozen=True)
class Domain:
    """One identity domain, opened with one API key.

    Two halves, told apart by whose resources they touch. The self-service
    (`Me`) endpoints act on the caller's own user and authorize on
    authentication alone, so a caller needs no policy beyond existing — that
    is what lets the sweep run as the seed in a tenancy where the legacy
    delete is refused to everyone, the account root included. The
    administrative endpoints take an explicit resource and need domain-admin
    rights, which the account root holds and the seed does not.
    """

    client: Any
    #: The endpoint `client` is addressed at, carried with it rather than
    #: beside it: a session hands its domain on to the session of a key it has
    #: just minted, and a client whose endpoint is unknown -- or an endpoint
    #: with no client opened at it -- is half of a domain.
    url: str

    # -- the self-service half: the caller's own user, no rights required ---

    def my_api_keys_by_fingerprint(self) -> dict[str, str]:
        """This user's API keys, as fingerprint -> the id a delete addresses.

        The domains API names a key by its own id and the legacy one by
        fingerprint, so this map is what makes a fingerprint deletable here.
        """
        listed = _data(self.client.list_my_api_keys())
        return {str(key.fingerprint): str(key.id) for key in (listed.resources or [])}

    def delete_my_api_key(self, key_id: str) -> None:
        _ = self.client.delete_my_api_key(key_id)

    def create_my_api_key(self, public_pem: str) -> str:
        """Register a key on the caller's own user. Returns the fingerprint assigned."""
        created = _data(
            self.client.create_my_api_key(
                my_api_key=oci.identity_domains.models.MyApiKey(schemas=[API_KEY_SCHEMA], key=public_pem)
            )
        )
        return str(created.fingerprint)

    # -- the administrative half: anybody's resources, domain-admin rights ---

    def group(self, name: str) -> Principal | None:
        listed = _data(self.client.list_groups(filter=f'displayName eq "{name}"'))
        for resource in listed.resources or []:
            return Principal.domain(resource, kind='group')
        return None

    def create_group(self, name: str, description: str) -> Principal:
        created = _data(
            self.client.create_group(
                group=oci.identity_domains.models.Group(
                    schemas=[GROUP_SCHEMA, GROUP_EXTENSION_SCHEMA],
                    display_name=name,
                    urn_ietf_params_scim_schemas_oracle_idcs_extension_group_group=(
                        oci.identity_domains.models.ExtensionGroupGroup(description=description)
                    ),
                )
            )
        )
        return Principal.domain(created, kind='group')

    def user(self, name: str) -> Principal | None:
        listed = _data(self.client.list_users(filter=f'userName eq "{name}"'))
        for resource in listed.resources or []:
            return Principal.domain(resource, kind='user')
        return None

    def create_user(self, name: str, description: str, email: str) -> Principal:
        """Create a user in the domain.

        The domain wants more of a user than IAM does — a family name and a
        primary email address are both mandatory, and there is nobody to ask
        for them, so the seed's own name and the register's address stand in.
        This is the shape the legacy `CreateUser` could not express, which is
        why it refused in a tenancy with domains.
        """
        created = _data(
            self.client.create_user(
                user=oci.identity_domains.models.User(
                    schemas=[USER_SCHEMA],
                    user_name=name,
                    display_name=name,
                    description=description,
                    name=oci.identity_domains.models.UserName(family_name=name, given_name=name),
                    emails=[
                        oci.identity_domains.models.UserEmails(value=email, type='work', primary=True),
                        oci.identity_domains.models.UserEmails(value=email, type='recovery'),
                    ],
                )
            )
        )
        return Principal.domain(created, kind='user')

    def members(self, group: Principal) -> list[str]:
        """The handles of the group's members.

        Membership is an attribute of the group in SCIM rather than a resource
        of its own, so reading it is reading the group.
        """
        read = _data(self.client.get_group(group.handle, attributes='members'))
        return [str(member.value) for member in (read.members or [])]

    def add_member(self, group: Principal, user: Principal) -> None:
        _ = self.client.patch_group(
            group.handle,
            patch_op=oci.identity_domains.models.PatchOp(
                schemas=[PATCH_SCHEMA],
                operations=[
                    oci.identity_domains.models.Operations(
                        op=oci.identity_domains.models.Operations.OP_ADD,
                        path='members',
                        value=[{'value': user.handle, 'type': 'User'}],
                    )
                ],
            ),
        )

    def create_api_key(self, user: Principal, public_pem: str) -> str:
        """Register a key on somebody else's user. Returns the fingerprint assigned."""
        created = _data(
            self.client.create_api_key(
                api_key=oci.identity_domains.models.ApiKey(
                    schemas=[API_KEY_SCHEMA],
                    key=public_pem,
                    user=oci.identity_domains.models.ApiKeyUser(value=user.handle, ocid=user.ocid),
                )
            )
        )
        return str(created.fingerprint)

    def api_keys_by_fingerprint(self, user_ocid: str) -> dict[str, str]:
        """Somebody else's API keys, as fingerprint -> the id a delete addresses.

        The administrative counterpart of `my_api_keys_by_fingerprint`, and the reason a
        session that is not the key's own user need not fall back to the
        legacy listing. The subject is named by OCID because that is the name
        the row, the policy and every signing configuration carry; naming it
        by SCIM id would need a user lookup first, which is another call
        through the same API this one is already making.
        """
        listed = _data(self.client.list_api_keys(filter=f'user.ocid eq "{user_ocid}"'))
        return {str(key.fingerprint): str(key.id) for key in (listed.resources or [])}

    def delete_api_key_by_id(self, key_id: str) -> None:
        """Retire somebody else's key, named by the id the listing gave it."""
        _ = self.client.delete_api_key(key_id)


@dataclass(frozen=True, kw_only=True)
class Iam:
    """The tenancy's IAM, as seen through one API key."""

    tenancy: str
    identity: Any
    #: The OCID this session signs as. It decides which half of the domains
    #: API a call belongs to: its own user is self-service, anyone else's is
    #: administrative -- so it has no default. A session that did not know
    #: whose it was would match no subject at all, and would send the calls
    #: that authorize on authentication alone to the endpoints that need
    #: domain-admin rights instead.
    caller: str
    #: How to build a client for a *different* key -- verification connects as
    #: the key it just minted, which is the only proof that it works.
    connect: Connect = identity_client
    #: The identity-domains view of the same key, endpoint and all, where the
    #: row knows the tenancy's domain. Absent in a tenancy without domains,
    #: and for a row written before the attribute existed.
    domain: Domain | None = None

    @property
    def domain_url(self) -> str | None:
        """Where this session's domain is, or None if it has none."""
        return self.domain.url if self.domain is not None else None

    @classmethod
    def authorize(
        cls,
        tenancy: str,
        user: str,
        private_key_pem: str,
        *,
        connect: Connect = identity_client,
        domain_url: str | None = None,
    ) -> Iam:
        """A session for one key, speaking the tenancy's domain where one is known.

        `domain_url` is what the row records, which is nothing at all in a
        tenancy without domains and in a row written before the attribute
        existed; the session either has the domain and its endpoint, or has
        neither.
        """
        return cls(
            tenancy=tenancy,
            identity=connect(tenancy, user, private_key_pem),
            caller=user,
            connect=connect,
            domain=(
                Domain(client=connect(tenancy, user, private_key_pem, domain_url=domain_url), url=domain_url)
                if domain_url
                else None
            ),
        )

    def group(self, identity: Identity) -> Principal:
        """The identity's group, created if absent.

        A group is a domain resource where there is a domain, so that is where
        it is looked for and made; the legacy call is what a tenancy without
        domains has, and what answers when the domain will not.
        """
        if self.domain is not None:
            try:
                found = self.domain.group(identity.name)
                if found is not None:
                    return found
                created = self.domain.create_group(identity.name, identity.group_description)
            except oci.exceptions.ServiceError as exc:
                log.debug('the identity domain refused to provide the group (%s); trying the legacy call', _why(exc))
            else:
                log.info('created group %s in the identity domain', identity.name)
                return created
        for existing in _data(self.identity.list_groups(compartment_id=self.tenancy, name=identity.name)):
            return Principal.legacy(existing)
        legacy = _data(
            self.identity.create_group(
                oci.identity.models.CreateGroupDetails(
                    compartment_id=self.tenancy,
                    name=identity.name,
                    description=identity.group_description,
                )
            )
        )
        log.info('created group %s', identity.name)
        return Principal.legacy(legacy)

    def user(self, identity: Identity) -> Principal:
        """The identity's user, created if absent.

        Through the domain first for the reason the group is, and for one
        more: the legacy `CreateUser` in a tenancy with domains refuses
        outright, because the domain demands fields the legacy request has
        nowhere to put.
        """
        if self.domain is not None:
            try:
                found = self.domain.user(identity.name)
                if found is not None:
                    return found
                created = self.domain.create_user(identity.name, identity.user_description, identity.email)
            except oci.exceptions.ServiceError as exc:
                log.debug('the identity domain refused to provide the user (%s); trying the legacy call', _why(exc))
            else:
                log.info('created user %s in the identity domain', identity.name)
                return created
        for existing in _data(self.identity.list_users(compartment_id=self.tenancy, name=identity.name)):
            return Principal.legacy(existing)
        legacy = _data(
            self.identity.create_user(
                oci.identity.models.CreateUserDetails(
                    compartment_id=self.tenancy,
                    name=identity.name,
                    description=identity.user_description,
                    email=identity.email,
                )
            )
        )
        log.info('created user %s', identity.name)
        return Principal.legacy(legacy)

    def membership(self, identity: Identity, user: Principal, group: Principal) -> None:
        """Put the identity's user in its group, if it is not there already.

        In the domain the membership is an attribute of the group rather than
        a resource of its own, so the read is a read of the group and the
        write is a patch of it. The legacy pair (`list_user_group_memberships`
        and `add_user_to_group`) says the same thing about the same fact.
        """
        if self.domain is not None:
            try:
                if user.handle in self.domain.members(group):
                    return
                self.domain.add_member(group, user)
            except oci.exceptions.ServiceError as exc:
                log.debug(
                    'the identity domain refused to record the membership (%s); trying the legacy call', _why(exc)
                )
            else:
                log.info('added %s to the %s group in the identity domain', identity.name, identity.name)
                return
        memberships = _data(
            self.identity.list_user_group_memberships(
                compartment_id=self.tenancy, user_id=user.ocid, group_id=group.ocid
            )
        )
        if memberships:
            return
        _ = self.identity.add_user_to_group(
            oci.identity.models.AddUserToGroupDetails(user_id=user.ocid, group_id=group.ocid)
        )
        log.info('added %s to the %s group', identity.name, identity.name)

    def policy(self, identity: Identity) -> Policy:
        """The identity's policy, created if absent and corrected if it has drifted.

        Corrected rather than left alone: the statements are what the group
        may do, and a policy that no longer matches them is either a
        half-finished change or someone's edit, both of which the register
        answers the same way.

        A policy is IAM's own concept rather than the domain's, so this is one
        of the two things the legacy client keeps (§2) — there is no domains
        endpoint to try first and none to fall back to.
        """
        # Filtered by name, as the user and group lookups are: an unfiltered
        # listing pages, and one page is all a single call hands back, so a
        # policy that had drifted past the first page would look absent and be
        # created a second time -- which the service answers with a 409 rather
        # than with a second policy. The name is compared again below because
        # the filter is the service's promise rather than this code's.
        for listed in _data(self.identity.list_policies(compartment_id=self.tenancy, name=identity.name)):
            existing = Policy.of(listed)
            if existing.name != identity.name:
                continue
            if existing.statements != identity.statements:
                updated = _data(
                    self.identity.update_policy(
                        existing.ocid,
                        oci.identity.models.UpdatePolicyDetails(statements=list(identity.statements)),
                    )
                )
                log.info("policy %s: statements set to the register's", identity.name)
                return Policy.of(updated)
            return existing
        created = _data(
            self.identity.create_policy(
                oci.identity.models.CreatePolicyDetails(
                    compartment_id=self.tenancy,
                    name=identity.name,
                    description=identity.policy_description,
                    statements=list(identity.statements),
                )
            )
        )
        log.info('created policy %s', identity.name)
        return Policy.of(created)

    def find_compartment(self, name: str) -> str | None:
        """The OCID of the tenancy's compartment of that name, or None.

        A compartment is IAM's own concept rather than the identity domain's,
        so this and the create below are the calls the legacy client keeps
        beside policy (§2): there is no domains endpoint to try first and none
        to fall back to.

        Only the tenancy's own children are considered. A compartment is a
        tree, and `compartment_id_in_subtree` would let a same-named
        compartment nested under somebody else's answer for this one.
        """
        for existing in _data(self.identity.list_compartments(compartment_id=self.tenancy, name=name)):
            # The name filter is the service's promise rather than this code's,
            # and a compartment keeps its name while it is being deleted:
            # adopting one would hand a consumer a boundary that is on its way
            # out, and it is the state rather than the name that says so.
            if existing.name != name or str(existing.lifecycle_state) in ('DELETING', 'DELETED'):
                continue
            return str(existing.id)
        return None

    def create_compartment(self, intended: Compartment) -> str:
        """Make the consumer's compartment as a child of the tenancy.

        Called only where `find_compartment` found nothing: a compartment name
        is unique among the children of one compartment, so a create whose name
        is taken is answered with a 409 rather than with a second compartment.
        """
        created = _data(
            self.identity.create_compartment(
                oci.identity.models.CreateCompartmentDetails(
                    compartment_id=self.tenancy,
                    name=intended.name,
                    description=f'What the {CLUSTER_NAME}-{intended.consumer} key may act on (credentials.md §3)',
                )
            )
        )
        log.info('created compartment %s', intended.name)
        return str(created.id)

    def upload_key(self, user: Principal, public_pem: str) -> str:
        """Register a public key on the user. Returns the fingerprint OCI assigned.

        An API key is a user credential, so the domain owns it: a rotation
        registers its own successor through the self-service endpoint, and a
        bring-up registers the seed's first key through the administrative one
        because the account root is signing and the seed is the subject.
        """
        if self.domain is not None:
            try:
                if user.ocid == self.caller:
                    assigned = self.domain.create_my_api_key(public_pem)
                else:
                    assigned = self.domain.create_api_key(user, public_pem)
            except oci.exceptions.ServiceError as exc:
                log.debug('the identity domain refused to register the key (%s); trying the legacy call', _why(exc))
            else:
                return assigned
        key = _data(self.identity.upload_api_key(user.ocid, oci.identity.models.CreateApiKeyDetails(key=public_pem)))
        return str(key.fingerprint)

    def _domain_keys(self, domain: Domain, user_id: str) -> dict[str, str]:
        """A user's keys through the domain, as fingerprint -> deletable id.

        The subject decides which half of the domains API answers: the
        caller's own user is self-service, which authorizes on authentication
        alone, and anybody else's is administrative, which needs domain-admin
        rights. Both return the same map, so everything above this asks one
        question rather than two.

        The self-service endpoint takes no subject and therefore cannot be
        used for somebody else: it would answer about the caller, and an
        answer about the wrong user is worse than a refusal — the quota check
        would pass on the wrong count and the sweep would retire the wrong
        keys.
        """
        if user_id == self.caller:
            return domain.my_api_keys_by_fingerprint()
        return domain.api_keys_by_fingerprint(user_id)

    def key_fingerprints(self, user_id: str) -> list[str]:
        """The user's key fingerprints, through the domain where there is one.

        Same reasoning as `retire_key`, and one degree stronger: the legacy
        read is refused *intermittently* even to the key's own user, so the
        subject is no reason to take it. It stays as the fallback and as the
        whole of the call in a session without a domain -- a row written
        before the attribute existed, or a tenancy that has no domains.
        """
        if self.domain is not None:
            try:
                return list(self._domain_keys(self.domain, user_id))
            except oci.exceptions.ServiceError as exc:
                # debug, not warning: the verification poll retries through
                # here every few seconds while a fresh key propagates.
                log.debug('the identity domain refused to list keys (%s); trying the legacy call', _why(exc))
        return [str(key.fingerprint) for key in _data(self.identity.list_api_keys(user_id))]

    def domains(self) -> list[TenancyDomain]:
        """The identity domains of the tenancy compartment."""
        return [TenancyDomain.of(listed) for listed in _data(self.identity.list_domains(compartment_id=self.tenancy))]

    def retire_key(self, user_id: str, key_fingerprint: str) -> None:
        """Retire one key, through the identity domain where there is one.

        Domains first, legacy second, rather than the other way round: in a
        tenancy that has identity domains the legacy call is refused every
        time and for everyone, so trying it first would spend a guaranteed
        round trip and log a failure on the path that works. The legacy call
        stays as the fallback because it is the only one a tenancy without
        domains has, and because a domain that refuses is not a reason to
        stop trying to delete.

        Two calls, because the two APIs name a key differently: the domain
        addresses it by its own id and the legacy service by fingerprint, so
        the listing that translates one into the other is part of the
        deletion. A listing that refuses falls through to the legacy delete
        rather than failing, on the same reasoning.
        """
        if self.domain is not None:
            try:
                key_id = self._domain_keys(self.domain, user_id).get(key_fingerprint)
            except oci.exceptions.ServiceError as exc:
                log.debug('the identity domain refused to list keys (%s); trying the legacy delete', _why(exc))
            else:
                if key_id is not None:
                    try:
                        if user_id == self.caller:
                            self.domain.delete_my_api_key(key_id)
                        else:
                            self.domain.delete_api_key_by_id(key_id)
                    except oci.exceptions.ServiceError as exc:
                        # debug, not warning: the sweep retries through here
                        # while a refusal may still be transient, and the
                        # outcome of the whole attempt is logged by the caller
                        # either way.
                        log.debug(
                            'the identity domain refused to delete %s (%s); trying the legacy call', key_id, _why(exc)
                        )
                    else:
                        log.info('deleted superseded API key %s', key_fingerprint)
                        return
        _ = self.identity.delete_api_key(user_id, key_fingerprint)
        log.info('deleted superseded API key %s', key_fingerprint)


def _mint_verified(iam: Iam, user: Principal, *, name: str) -> str:
    """Put a new key on the user and prove it signs before anything depends on it.

    Returns the private key PEM; the fingerprint is a function of it. The
    verifying session is opened at the same domain as `iam`, because a key
    must be verified the way it will be used. `name` is the identity being
    minted for, which appears in the log because a bring-up mints for more
    than one.
    """
    pair = generate_key()
    private_pem = pair.private_pem
    log.info('uploading a new API key for %s', name)
    assigned = iam.upload_key(user, pair.public_pem)
    if assigned != fingerprint(private_pem):
        raise CredentialRejected(f'OCI registered fingerprint {assigned}, which is not the one this key computes to')
    minted = Iam.authorize(iam.tenancy, user.ocid, private_pem, connect=iam.connect, domain_url=iam.domain_url)
    log.info(
        'waiting for %s to authenticate (propagation is eventually consistent, usually well under a minute)', assigned
    )
    deadline = time.monotonic() + PROPAGATION_DEADLINE
    while True:
        try:
            _ = minted.key_fingerprints(user.ocid)
            break
        except oci.exceptions.ServiceError as exc:
            if exc.status != 401 or time.monotonic() >= deadline:
                raise
            time.sleep(PROPAGATION_INTERVAL)
    log.info('minted an API key for %s (%s), verified against the API', name, assigned)
    return private_pem


def _retire(iam: Iam, user_id: str, key_fingerprint: str) -> None:
    """Delete one superseded key, outwaiting a refusal that is only transient.

    A sweep usually runs as a key minted seconds earlier, and minting proves
    only that the key *lists*: authorization for the other endpoints
    propagates on its own schedule, and the legacy shim refuses
    intermittently even once it has. Both look identical from here -- a
    refusal with no way to tell "never" from "not yet" apart -- so the whole
    attempt is retried, domain lookup and legacy fallback together, on the
    same bounded-deadline shape verification uses. Retrying one leg would be
    a guess at which one was lagging.

    Only a refusal that outlives the deadline becomes a console errand: the
    kept key is stored and verified already, so one key that will not go must
    not fail the run or block revoking the rest.
    """
    deadline = time.monotonic() + PROPAGATION_DEADLINE
    waiting = False
    while True:
        try:
            iam.retire_key(user_id, key_fingerprint)
            return
        except oci.exceptions.ServiceError as exc:
            if time.monotonic() >= deadline:
                log.warning(
                    'could not delete superseded API key %s (%s); delete it in the console',
                    key_fingerprint,
                    _why(exc),
                )
                return
            if not waiting:
                waiting = True
                log.info(
                    'the service refused to delete %s (%s); retrying until it goes or %.0f seconds pass '
                    '(a refusal this soon after a mint is usually authorization still propagating, '
                    'which clears well under a minute)',
                    key_fingerprint,
                    _why(exc),
                    PROPAGATION_DEADLINE,
                )
            else:
                log.debug('the service still refuses to delete %s (%s)', key_fingerprint, _why(exc))
            time.sleep(PROPAGATION_INTERVAL)


def _sweep(iam: Iam, user_id: str, keep: str) -> None:
    """Delete every key on the user except `keep`, as the user itself.

    The caller is authorized as `keep` rather than as an administrator: the
    self-service endpoints need no rights beyond authentication, so the sweep
    asks nothing of the seed's policy, and a session must not saw off the key
    it signs with mid-sweep. Each key gets its own deadline (`_retire`),
    because each is its own operation and one that cannot go says nothing
    about the next.
    """
    for existing in iam.key_fingerprints(user_id):
        if existing == keep:
            continue
        _retire(iam, user_id, existing)


def _room_for_one_more(iam: Iam, user_id: str, *, name: str, live: str | None) -> None:
    """Refuse to mint into a full user, naming the keys in the way.

    Reached when the sweep could not make room: a create-after-loss has no key
    to sweep with, a sweep's deletions can be refused, and a run whose push
    failed leaves the sweep undone by design (`delivery.py`). The quota 400 the
    mint would hit names neither the keys nor the fix, so this does.

    **Which key to keep has two answers, and they are different refusals.**
    `live` is the fingerprint the caller knows is in use. A seed rotation knows
    it: the credential at stake is the one the kit holds the private half of
    and the one this program is signing with, computed a statement earlier. So
    that refusal names it as the key to keep and lists the rest as the ones to
    delete. Telling a rotation that deleting all of them is safe would be the
    destructive advice on this whole path -- the key it would spend is the
    seed's own, after which the row is recoverable only from the account root,
    through `credentials seed oci create`. Not `kit bootstrap --only oci`: the
    row is still in the kit, only its key at OCI is gone, and the walk skips
    what the kit already has (`lifecycle.bootstrap`). The single-row create
    probes nothing and overwrites both halves, which is what a present-but-dead
    row needs.

    Everything else is the other answer, and `live` is `None`. A derived row's
    credential lives in a slot this program never reads, delivered there as a
    secret; a seed create has nothing in hand to compare against, because the
    row it is about to write is the first thing that would hold a private half.
    Either way nothing here can pick the live key out of the strays, and the
    refusal says so, offering the two moves that are safe without knowing --
    delete all but one and re-run, or delete all of them and re-run, because
    the re-run mints a fresh key and writes it down either way.
    """
    try:
        held = iam.key_fingerprints(user_id)
    except oci.exceptions.ServiceError as exc:
        # The check exists to turn a quota 400 into an error naming the keys;
        # a caller both of whose listings are refused just loses the nicety
        # and lets the mint speak for itself.
        log.debug('could not read the key count (%s); minting without the pre-check', exc.code)
        return
    if len(held) < KEY_QUOTA:
        return
    spare = [fingerprint for fingerprint in held if fingerprint != live]
    if live is not None and len(spare) < len(held):
        raise CredentialRejected(
            f'{name} already holds {len(held)} API keys (the quota) and the sweep could not make room. '
            f'Keep {live}: it is the key this kit holds the private half of and the one this command signs '
            f'as, and deleting it would leave the row recoverable only from the account root. Delete the '
            f'rest in the console and run this again: {", ".join(spare)}. A sweep that could not make room '
            'usually means this row predates the identity-domain attribute retirement goes through: '
            '`credentials seed oci domain` records it once, and until it does every rotation strands '
            'another key here (docs/credentials.md §4.3).'
        )
    raise CredentialRejected(
        f'{name} already holds {len(held)} API keys (the quota), and this run cannot tell which of them is '
        'in use: nothing it reads holds the private half of any of them. Delete all but one in the console '
        'and run this again, or delete all of them and run this again, which is equally safe because the '
        f're-run mints a fresh key and writes it down: {", ".join(held)}'
    )


def domain_url(iam: Iam) -> str:
    """The URL of the identity domain the seed user lives in.

    Discovered rather than configured: it is a property of the tenancy, not
    of this repository, and a tenancy with one domain has no ambiguity to
    resolve. A user the legacy `IdentityClient` created lands in the default
    domain, which is why that is the one picked where there are several.
    """
    found = iam.domains()
    if len(found) == 1:
        return found[0].url
    for candidate in found:
        if candidate.kind == oci.identity.models.Domain.TYPE_DEFAULT:
            return candidate.url
    raise CredentialRejected(
        'the tenancy has no default identity domain to retire API keys through '
        f'(it has: {", ".join(candidate.display_name for candidate in found) or "none"})'
    )


def _discovered_domain(iam: Iam, *, whose: str) -> str | None:
    """`domain_url`, downgraded to a warning that names the repair.

    Reading the tenancy's domains is an administrator's call: the account
    root has it, the seed's own policy (users, groups, policies) does not
    necessarily. Neither a bring-up nor a rotation is worth failing over it,
    because the only thing lost is the retirement of superseded keys -- which
    is already a warning of its own.
    """
    try:
        return domain_url(iam)
    except (oci.exceptions.ServiceError, CredentialRejected) as exc:
        log.warning(
            'could not read the tenancy identity domain as the %s (%s); superseded keys may survive this run. '
            'Run `credentials seed oci domain` once, which reads it with the account root and records it on the row.',
            whose,
            exc,
        )
        return None


def _store(kit: KdbxStore, entry: str, *, tenancy: str, user_id: str, private_pem: str, domain: str | None) -> None:
    """Write the row: user OCID, PEM attachment, tenancy and domain attributes (§2)."""
    kit.put(entry, user_id, '')
    kit.attach(entry, entries.OCI_KEY_ATTACHMENT, private_pem.encode())
    kit.set_attribute(entry, entries.OCI_TENANCY_ATTRIBUTE, tenancy)
    if domain:
        kit.set_attribute(entry, entries.OCI_DOMAIN_ATTRIBUTE, domain)


@dataclass(frozen=True)
class SeedRow:
    """The three parts of the seed row that are stored (§2).

    A record rather than three strings in an order: two of them are OCIDs of
    different kinds, and nothing downstream would notice them swapped.
    """

    tenancy: str
    user: str
    private_key: str


def load_seed(store: KdbxStore, entry: str) -> SeedRow:
    """The row's three stored parts."""
    return SeedRow(
        tenancy=store.attribute(entry, entries.OCI_TENANCY_ATTRIBUTE),
        user=store.get(entry, attribute='UserName'),
        private_key=store.attachment(entry, entries.OCI_KEY_ATTACHMENT).decode(),
    )


def load_domain(store: KdbxStore, entry: str) -> str | None:
    """The row's identity domain URL, or None for a row written before it.

    Absent is a state rather than an error: kits exist that predate the
    attribute, and rotation must not require the account root to repair one.
    """
    if entries.OCI_DOMAIN_ATTRIBUTE not in store.attributes(entry):
        return None
    return store.attribute(entry, entries.OCI_DOMAIN_ATTRIBUTE)


@dataclass(frozen=True)
class _SeedSession:
    """The seed, authorized, together with the row it was opened from.

    Named apart from `b2.Session` and `cloudflare.Session`, which are the
    authorized client itself: here that is `iam`, and this is the pair.
    """

    iam: Iam
    #: The stored row, whose tenancy is the one everything minted from this
    #: seed belongs to.
    row: SeedRow

    @property
    def domain(self) -> str | None:
        """The identity domain the session is addressed at, or None.

        The session's own, rather than a second copy of it: None here is a
        tenancy that has no domain and one whose domains this key may not
        read, both of which authorize the same way.
        """
        return self.iam.domain_url


def _seed_session(store: KdbxStore, seed_entry: str, *, connect: Connect) -> _SeedSession:
    """Read the seed row and authorize as it, repairing a pre-domain row on the way.

    The one preamble of every command the seed performs, its own rotation and
    a consumer's mint alike, because the repair below must not be maintained
    twice: it runs only for a kit written before the row carried an identity
    domain, which is the least exercised and worst place for two copies to
    drift apart.

    A row from before the attribute existed gets one chance at the domain from
    the seed itself. Reading the tenancy's domains is an administrator's call
    and the seed's policy does not include it, so where the tenancy refuses,
    the warning names the repair (`credentials seed oci domain`) and the run
    goes on through the legacy shim.
    """
    row = load_seed(store, seed_entry)
    domain = load_domain(store, seed_entry)
    iam = Iam.authorize(row.tenancy, row.user, row.private_key, connect=connect, domain_url=domain)
    if domain is None:
        domain = _discovered_domain(iam, whose='seed')
        if domain is not None:
            iam = Iam.authorize(row.tenancy, row.user, row.private_key, connect=connect, domain_url=domain)
    return _SeedSession(iam=iam, row=row)


def adopt_domain(
    store: KdbxStore, *, seed_entry: str, root: masters.Credential, connect: Connect = identity_client
) -> str:
    """Record the tenancy's identity domain on a row that predates it.

    The one-time repair for a kit written before API-key retirement moved to
    the identity-domains API. It borrows the account root because reading the
    tenancy's domains is an administrator's call and the seed's policy does
    not include it -- which is exactly why rotation warns rather than doing
    this by itself.
    """
    iam = Iam.authorize(
        root[masters.OCI_TENANCY], root[masters.OCI_USER], root[masters.OCI_PRIVATE_KEY], connect=connect
    )
    url = domain_url(iam)
    store.set_attribute(seed_entry, entries.OCI_DOMAIN_ATTRIBUTE, url)
    log.info('recorded the identity domain on %s', seed_entry)
    return url


def ensure(iam: Iam, identity: Identity) -> Principal:
    """Converge the user, its group, the membership between them and the policy.

    Idempotent by probing, like every other stage in this family: each of the
    four asks whether it exists and creates it only if it does not, so a run
    interrupted between the group and the key is resumed by running it again.
    Returns the user, which is what a key is then minted on.
    """
    group = iam.group(identity)
    user = iam.user(identity)
    iam.membership(identity, user, group)
    _ = iam.policy(identity)
    return user


def create_seed(
    *, root: masters.Credential, seeds: KdbxStore, seed_entry: str, connect: Connect = identity_client
) -> str:
    """Create the seed user, its group, its policy and its key. Returns the user OCID.

    Idempotent in the same way `bootstrap` is: each of the four asks whether
    it exists and creates it only if it does not, so a run interrupted between
    the group and the key is resumed by running it again.

    Needed once at bring-up, and again only if the seed is lost — routine
    rotation is `rotate_seed`, which never touches the account root.
    """
    # Above everything, because everything below it writes: the group, the
    # user, the membership, the policy and a live signing key are all made in
    # whatever tenancy the operator's root names, and a root naming another
    # account is exactly what a hand-assembled kit gets wrong. Held afterwards
    # it would be a refusal that leaves four IAM principals and a usable key in
    # an account nothing here records -- and the kit, having stored that
    # tenancy, would then refuse every later mint correctly, so the orphan
    # would never be mentioned again.
    verify_tenancy(root[masters.OCI_TENANCY])

    # The account root is the credential that can read the tenancy's identity
    # domains, and this is the one moment it is in hand: stored on the row,
    # rotation retires keys without ever borrowing it again. It is also read
    # first, because the domain is where the user, the group and the key are
    # made -- the root is the domain administrator, and a session that does
    # not know its domain can only reach them through the legacy shim.
    iam = Iam.authorize(
        root[masters.OCI_TENANCY], root[masters.OCI_USER], root[masters.OCI_PRIVATE_KEY], connect=connect
    )
    domain = _discovered_domain(iam, whose='account root')
    if domain is not None:
        iam = Iam.authorize(
            root[masters.OCI_TENANCY],
            root[masters.OCI_USER],
            root[masters.OCI_PRIVATE_KEY],
            connect=connect,
            domain_url=domain,
        )

    user = ensure(iam, SEED)

    # No `live` key: this creates the seed, so there is nothing in the kit
    # yet whose private half would tell one of the strays from the others.
    _room_for_one_more(iam, user.ocid, name=SEED.name, live=None)
    private_pem = _mint_verified(iam, user, name=SEED.name)
    _store(seeds, seed_entry, tenancy=iam.tenancy, user_id=user.ocid, private_pem=private_pem, domain=domain)

    # A run that died between upload and store left a key whose private half
    # no longer exists anywhere; a user holds at most three keys, so orphans
    # eventually block minting. Only the stored key survives (_sweep).
    seed_iam = Iam.authorize(iam.tenancy, user.ocid, private_pem, connect=iam.connect, domain_url=domain)
    _sweep(seed_iam, user.ocid, fingerprint(private_pem))
    return user.ocid


def rotate_seed(
    store: KdbxStore, *, seed_entry: str, into: KdbxStore | None = None, connect: Connect = identity_client
) -> str:
    """Have the seed mint its own successor key and retire the predecessor.

    The user, group and policy are unchanged: what rotates is the key material
    under them. The predecessor is deleted only after the successor is stored
    and verified, so an interrupted rotation leaves a working seed either way.

    `into` is where the successor is written, defaulting to the database the
    predecessor came from — a whole-kit rotation writes a *new* file (§4.2)
    and the retired one must stay exactly as it was.
    """
    seed = _seed_session(store, seed_entry, connect=connect)

    # Before the sweep, which is this path's first write and a destructive one:
    # a row naming an account `conventions` does not record is a row whose keys
    # this program has no business deleting or adding to. Lower stakes than
    # `create_seed`, which makes principals, and the same shape.
    verify_tenancy(seed.row.tenancy)

    # Everything but the signing key is dead weight, and the quota (three)
    # must have room for the successor before it can be minted.
    live = fingerprint(seed.row.private_key)
    _sweep(seed.iam, seed.row.user, live)
    # The one path where the live key is knowable: it is the key the kit holds
    # and the one this session signs with, so a refusal here names it as the
    # key to keep rather than telling the operator to spend it.
    _room_for_one_more(seed.iam, seed.row.user, name=SEED.name, live=live)
    # The row stores the OCID and nothing else, which is all a rotation needs:
    # the key it registers goes on its own user, and the self-service endpoint
    # takes no subject at all.
    private_pem = _mint_verified(seed.iam, Principal(ocid=seed.row.user, handle=seed.row.user), name=SEED.name)
    _store(
        into or store,
        seed_entry,
        tenancy=seed.row.tenancy,
        user_id=seed.row.user,
        private_pem=private_pem,
        domain=seed.domain,
    )

    previous = live
    current = fingerprint(private_pem)
    successor = Iam.authorize(seed.row.tenancy, seed.row.user, private_pem, connect=connect, domain_url=seed.domain)
    _sweep(successor, seed.row.user, current)
    log.info('seed rotated: %s -> %s', previous, current)
    return current


@dataclass(frozen=True)
class ApiKey:
    """A minted API key, as the five things a signing configuration needs.

    Three are carried and two are derived, for the reason the kit's row stores
    three (§2): the region is a constant of this installation, and the
    fingerprint is a function of the key, so a carried copy of either could
    only ever disagree with what it describes.
    """

    #: The tenancy the user belongs to.
    tenancy: str
    #: The user OCID the key signs as.
    user: str
    #: The PEM, which exists here and in the slot this is delivered to, and
    #: nowhere else -- never in the kit (§1 rule 2).
    private_key: str

    @property
    def region(self) -> str:
        return conventions.OCI_TENANCY.region

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.private_key)


def verify_tenancy(tenancy: str) -> None:
    """Hold the seed's account against the one `conventions` records.

    The tenancy OCID is a fact rather than a credential, so it has one home
    (`conventions.OCI_TENANCY`) and a mint copies nothing: the seed belongs to
    whichever account it was created in, and all that is left is to prove that
    account is this installation's. The two ways it can fail to be are the ways
    the fact goes stale — a kit re-seeded from a different tenancy, and an OCID
    recorded wrong — and both would build IAM objects and a signing key in an
    account nothing here manages, so both are worth stopping over.

    Both accounts are named, because which of the two is stale is the
    operator's question and neither one alone answers it.
    """
    intended = conventions.OCI_TENANCY.tenancy_ocid
    if tenancy != intended:
        raise CredentialRejected(
            f'this seed signs for {tenancy}, but `conventions.OCI_TENANCY` records {intended} as the account '
            'this program declares into: one of the two is stale, and minting here would leave a live key in '
            'an account the stack does not act in'
        )
    log.info('the seed signs for %s, which is the tenancy `conventions` records', tenancy)


def ensure_compartment(iam: Iam, consumer: str, *, override: str | None = None) -> str:
    """The compartment one consumer's key is confined to, created if it is not there.

    `override` is the drill-tenancy escape and is taken as given: a tenancy
    that is not this installation's has neither the names nor the OCIDs
    `conventions` records, so a run pointed at one names its compartment on the
    command line and nothing here second-guesses it.

    Otherwise the compartment is `conventions`': looked up by name, which is
    the half of the mapping that is a decision rather than a discovery.

    What happens next depends on whether the mapping already carries the OCID.
    Where it does not, the compartment is created if the tenancy has none of
    that name and adopted if it has — the way a user or a group is — and the
    OCID that comes back is announced as the one-line edit to commit, because
    the stacks read it from that file and this command has no business editing
    it. Where the mapping does carry one, nothing is created: a recorded OCID
    that no longer answers is a fact gone stale, and the two ways it can go
    stale — the compartment is not there, or the name now belongs to a
    different one — are both worth stopping over rather than papering over
    with a second compartment.
    """
    if override is not None:
        log.info('confining %s to %s, which was named on the command line', consumer, override)
        return override
    intended = conventions.OCI_TENANCY.compartments[consumer]
    log.info('looking up the %s compartment', intended.name)
    found = iam.find_compartment(intended.name)
    if intended.ocid is None:
        ocid = found if found is not None else iam.create_compartment(intended)
        log.warning(
            'the %s compartment is %s: record it as the `%s` entry of `conventions.OCI_TENANCY.compartments` and commit '
            'that line, because the stack reads the OCID from there and refuses until it is written',
            intended.name,
            ocid,
            consumer,
        )
        return ocid
    if found is None:
        raise CredentialRejected(
            f'`conventions` records {intended.ocid} as the {intended.name} compartment, but this tenancy has '
            'none of that name: either the mapping is stale, or this is not the tenancy it describes — a drill '
            'names its own compartment with --compartment'
        )
    if found != intended.ocid:
        raise CredentialRejected(
            f'the compartment named {intended.name} in this tenancy is {found}, but `conventions` records '
            f'{intended.ocid} for {consumer}: one of the two is stale, and minting against the wrong one would '
            'confine the key to a compartment the stack does not act in'
        )
    return found


def mint_api_key(
    kit: KdbxStore,
    *,
    consumer: str,
    compartment_id: str | None = None,
    seed_entry: str,
    connect: Connect = identity_client,
) -> Delivery[ApiKey]:
    """Mint one consumer's API key from the seed, with the IAM objects under it.

    The whole of a §3 OCI row's *birth*, compartment included: the boundary the
    key is confined to is `conventions`' to name and this command's to create,
    so a consumer whose compartment does not exist yet is one command away
    rather than a console errand. `compartment_id` overrides that mapping for a
    drill tenancy. Where the result is delivered is the caller's business,
    because that is the half that differs between a stack — a Pulumi config
    secret — and the state-backend provisioner, which is not a stack and reads
    a workstation slot instead.

    Idempotent, so rotating the row is re-running its command: the compartment,
    the user, the group and the policy are converged rather than created, the
    successor is verified by being used before anything supersedes it, and the
    sweep leaves exactly the key this run minted.

    **The sweep is handed back rather than run**, so it happens after the
    caller's push and not before it (`delivery.py`, and the register's §4 for
    why). A push that fails therefore leaves the predecessor live and this
    run's key stranded beside it, which the next run's sweep clears.

    **The account is proven before anything is created, not after.** The seed's
    row names the tenancy it belongs to, so opening the kit is enough to know
    it, and everything below writes to that tenancy — the seed's own policy
    first, which is a create in a tenancy that has none, then the compartment.
    Checked afterward it would be a refusal that leaves an IAM user, a group,
    a policy and a live signing key behind in an account this installation does
    not own, recorded nowhere and known to nobody who could revoke them. A run
    given `compartment_id` is pointed at a drill tenancy and is not held to the
    recorded account, for the reason `ensure_compartment` is not: none of the
    names `conventions` records describe that tenancy.
    """
    log.info('opening the OCI seed from the kit')
    seed = _seed_session(kit, seed_entry, connect=connect)
    if compartment_id is None:
        verify_tenancy(seed.row.tenancy)

    # The seed's own policy first, and here rather than in a verb of its own:
    # this is the one command that needs the compartment statement, the seed
    # may write policy in the tenancy, and a converge an operator has to
    # remember to run is a converge that is forgotten -- with the failure
    # landing mid-mint, as a refusal to create the compartment. `policy`
    # corrects drift, so a seed whose policy predates a statement adopts it
    # here; OCI takes a few seconds to honour the widened policy, which the
    # client's retry strategy spends waiting rather than failing on.
    log.info("converging the seed's own policy, which is what may create the compartment below")
    _ = seed.iam.policy(SEED)

    compartment = ensure_compartment(seed.iam, consumer, override=compartment_id)
    identity = Identity.for_consumer(consumer, compartment_id=compartment)

    log.info('converging the user, group and policy for %s', identity.name)
    user = ensure(seed.iam, identity)
    # No `live` key: this consumer's credential is in a slot this program
    # never reads, delivered there as a secret.
    _room_for_one_more(seed.iam, user.ocid, name=identity.name, live=None)
    private_pem = _mint_verified(seed.iam, user, name=identity.name)

    # Swept as the key just minted rather than as the seed, the way a bring-up
    # sweeps as the seed it just created: the self-service endpoints authorize
    # on authentication alone (§4.3), so retiring what this run supersedes --
    # the predecessor on a re-run, an orphan from a run that died between
    # upload and push -- asks nothing of the seed's policy. Which session may
    # sweep is this module's knowledge, which is why the caller is handed a
    # closure rather than a rule it would have to follow.
    minted_session = Iam.authorize(seed.row.tenancy, user.ocid, private_pem, connect=connect, domain_url=seed.domain)
    return Delivery.of(
        ApiKey(tenancy=seed.row.tenancy, user=user.ocid, private_key=private_pem),
        lambda: _sweep(minted_session, user.ocid, fingerprint(private_pem)),
    )
