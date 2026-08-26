"""The OCI credential family (docs/credentials.md §2–§3).

Two credentials, two lifetimes:

-   the **account root API key** — an account root (`masters.py`), belonging
    to a user who may manage users, groups and policies in the tenancy. The
    key that provisions instances is not this: it can launch a machine and
    cannot see an IAM user, so it can create neither the seed's user nor the
    seed's key;
-   the **seed API key** — offline, in the kit, belonging to a dedicated user
    in a dedicated group under a dedicated policy. It mints the per-stack
    users and their API keys, and — because that same permission covers its
    own user — its own successor.

**An OCI API key is five things, and the row stores three** (§2): the user
OCID as `UserName`, the private key as an attachment because it is a file, and
the tenancy OCID as a protected custom attribute, since `UserName` is spoken
for. The other two are recovered rather than stored — the region is a
constant in `conventions`, and the fingerprint is a function of the public
key, so a stored copy could only ever disagree with the key it describes.

The password field stays empty here, which no other row does. The alternative
is a copy of the PEM in a field that KeePassXC will happily reveal in a
listing, next to the attachment that already holds it.
"""

# The OCI SDK is generated and ships no type information; this module is the
# whole of its untyped surface here, and everything it exports is annotated.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import time

import oci
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from . import entries, masters
from ...conventions import OCI_REGION, OCI_SEED_USER_EMAIL
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

#: What the seed may do, and the whole of it. Managing users covers their API
#: keys, which is what makes the seed self-reproducing; managing groups and
#: policies is what lets it give a per-stack user the access its stack needs.
#: Nothing here touches compute, storage or networking -- the seed mints the
#: credentials that do.
STATEMENTS: tuple[str, ...] = (
    f'Allow group {SEED_NAME} to manage users in tenancy',
    f'Allow group {SEED_NAME} to manage groups in tenancy',
    f'Allow group {SEED_NAME} to manage policies in tenancy',
)

#: OCI requires RSA for API keys, which is why these are generated rather than
#: derived: deterministic RSA generation is the footgun §2.2 excludes.
KEY_SIZE = 2048

#: Builds an identity client for a (tenancy, user, private key PEM). Injected
#: so a test can drive the whole flow without a tenancy, and so verification
#: can connect *as the key it just minted*.
Connect = Callable[[str, str, str], Any]


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


def generate_key() -> tuple[str, str]:
    """A fresh RSA key pair, as (private PEM, public PEM)."""
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
    return private_pem, public_pem


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


def identity_client(tenancy: str, user: str, private_key_pem: str) -> Any:
    """An identity client for one API key, with no configuration file involved.

    The key never reaches `~/.oci/config` or any other path: it is read out of
    the kit (or the secret store) and handed to the SDK in memory.
    """
    config: dict[str, Any] = {
        'tenancy': tenancy,
        'user': user,
        'key_content': private_key_pem,
        'fingerprint': fingerprint(private_key_pem),
        'region': OCI_REGION,
    }
    try:
        oci.config.validate_config(config)
    except oci.exceptions.InvalidConfig as exc:
        raise CredentialRejected(f'the OCI credential is not usable: {exc}') from exc
    return oci.identity.IdentityClient(config, retry_strategy=_retry())


def _data(response: Any) -> Any:
    """Unwrap an SDK response at the one boundary that is untyped by nature."""
    return response.data


@dataclass
class Iam:
    """The tenancy's IAM, as seen through one API key."""

    tenancy: str
    identity: Any
    #: How to build a client for a *different* key -- verification connects as
    #: the key it just minted, which is the only proof that it works.
    connect: Connect = identity_client

    @classmethod
    def authorize(cls, tenancy: str, user: str, private_key_pem: str, *, connect: Connect = identity_client) -> Iam:
        return cls(tenancy=tenancy, identity=connect(tenancy, user, private_key_pem), connect=connect)

    def group(self) -> Any:
        """The seed's group, created if absent."""
        for existing in _data(self.identity.list_groups(compartment_id=self.tenancy, name=SEED_NAME)):
            return existing
        created = _data(
            self.identity.create_group(
                oci.identity.models.CreateGroupDetails(
                    compartment_id=self.tenancy,
                    name=SEED_NAME,
                    description='Holds the kluster seed user (credentials.md §2)',
                )
            )
        )
        log.info('created group %s', SEED_NAME)
        return created

    def user(self) -> Any:
        """The seed's user, created if absent."""
        for existing in _data(self.identity.list_users(compartment_id=self.tenancy, name=SEED_NAME)):
            return existing
        created = _data(
            self.identity.create_user(
                oci.identity.models.CreateUserDetails(
                    compartment_id=self.tenancy,
                    name=SEED_NAME,
                    description='The kluster seed API key (credentials.md §2)',
                    email=OCI_SEED_USER_EMAIL,
                )
            )
        )
        log.info('created user %s', SEED_NAME)
        return created

    def membership(self, user_id: str, group_id: str) -> None:
        memberships = _data(
            self.identity.list_user_group_memberships(compartment_id=self.tenancy, user_id=user_id, group_id=group_id)
        )
        if memberships:
            return
        _ = self.identity.add_user_to_group(
            oci.identity.models.AddUserToGroupDetails(user_id=user_id, group_id=group_id)
        )
        log.info('added %s to %s', SEED_NAME, SEED_NAME)

    def policy(self) -> Any:
        """The seed's policy, created if absent and corrected if it has drifted.

        Corrected rather than left alone: the statements are what the seed may
        do, and a policy that no longer matches them is either a half-finished
        change or someone's edit, both of which the register answers the same
        way.
        """
        for existing in _data(self.identity.list_policies(compartment_id=self.tenancy)):
            if existing.name != SEED_NAME:
                continue
            if list(existing.statements) != list(STATEMENTS):
                updated = _data(
                    self.identity.update_policy(
                        existing.id,
                        oci.identity.models.UpdatePolicyDetails(statements=list(STATEMENTS)),
                    )
                )
                log.info("policy %s: statements set to the register's", SEED_NAME)
                return updated
            return existing
        created = _data(
            self.identity.create_policy(
                oci.identity.models.CreatePolicyDetails(
                    compartment_id=self.tenancy,
                    name=SEED_NAME,
                    description='What the kluster seed key may do (credentials.md §2)',
                    statements=list(STATEMENTS),
                )
            )
        )
        log.info('created policy %s', SEED_NAME)
        return created

    def upload_key(self, user_id: str, public_pem: str) -> str:
        """Register a public key on the user. Returns the fingerprint OCI assigned."""
        key = _data(self.identity.upload_api_key(user_id, oci.identity.models.CreateApiKeyDetails(key=public_pem)))
        return str(key.fingerprint)

    def api_keys(self, user_id: str) -> list[str]:
        return [str(key.fingerprint) for key in _data(self.identity.list_api_keys(user_id))]

    def delete_api_key(self, user_id: str, key_fingerprint: str) -> None:
        _ = self.identity.delete_api_key(user_id, key_fingerprint)
        log.info('deleted superseded API key %s', key_fingerprint)


def _mint_verified(iam: Iam, user_id: str) -> str:
    """Put a new key on the user and prove it signs before anything depends on it.

    Returns the private key PEM; the fingerprint is a function of it.
    """
    private_pem, public_pem = generate_key()
    log.info('uploading a new API key for %s', SEED_NAME)
    assigned = iam.upload_key(user_id, public_pem)
    if assigned != fingerprint(private_pem):
        raise CredentialRejected(f'OCI registered fingerprint {assigned}, which is not the one this key computes to')
    minted = Iam.authorize(iam.tenancy, user_id, private_pem, connect=iam.connect)
    log.info(
        'waiting for %s to authenticate (propagation is eventually consistent, usually well under a minute)', assigned
    )
    deadline = time.monotonic() + PROPAGATION_DEADLINE
    while True:
        try:
            _ = minted.api_keys(user_id)
            break
        except oci.exceptions.ServiceError as exc:
            if exc.status != 401 or time.monotonic() >= deadline:
                raise
            time.sleep(PROPAGATION_INTERVAL)
    log.info('minted an API key for %s (%s), verified against the API', SEED_NAME, assigned)
    return private_pem


def _store(kit: KdbxStore, entry: str, *, tenancy: str, user_id: str, private_pem: str) -> None:
    """Write the row: user OCID, PEM attachment, tenancy attribute (§2)."""
    kit.put(entry, user_id, '')
    kit.attach(entry, entries.OCI_KEY_ATTACHMENT, private_pem.encode())
    kit.set_attribute(entry, entries.OCI_TENANCY_ATTRIBUTE, tenancy)


def load_seed(store: KdbxStore, entry: str) -> tuple[str, str, str]:
    """The row's three stored parts, as (tenancy OCID, user OCID, private PEM)."""
    return (
        store.attribute(entry, entries.OCI_TENANCY_ATTRIBUTE),
        store.get(entry, attribute='UserName'),
        store.attachment(entry, entries.OCI_KEY_ATTACHMENT).decode(),
    )


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
    iam = Iam.authorize(root['tenancy'], root['user'], root['private-key'], connect=connect)
    group = iam.group()
    user = iam.user()
    iam.membership(str(user.id), str(group.id))
    _ = iam.policy()

    private_pem = _mint_verified(iam, str(user.id))
    _store(seeds, seed_entry, tenancy=iam.tenancy, user_id=str(user.id), private_pem=private_pem)

    # A run that died between upload and store left a key whose private half
    # no longer exists anywhere; a user holds at most three keys, so orphans
    # eventually block minting. Only the stored key survives -- the same sweep
    # `rotate_seed` runs, and as the same identity: the seed deletes its own
    # keys, because an identity-domains tenancy lets a user manage its own
    # credentials while refusing the account root the equivalent legacy call
    # (IdcsConversionError). The seed being stored already, a failed sweep is
    # a warning and a console errand, not a failed bring-up.
    seed_iam = Iam.authorize(iam.tenancy, str(user.id), private_pem, connect=iam.connect)
    current = fingerprint(private_pem)
    for existing in seed_iam.api_keys(str(user.id)):
        if existing == current:
            continue
        try:
            seed_iam.delete_api_key(str(user.id), existing)
        except oci.exceptions.ServiceError as exc:
            log.warning('could not delete superseded API key %s (%s); delete it in the console', existing, exc.code)
    return str(user.id)


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
    tenancy, user_id, previous_pem = load_seed(store, seed_entry)
    iam = Iam.authorize(tenancy, user_id, previous_pem, connect=connect)

    private_pem = _mint_verified(iam, user_id)
    _store(into or store, seed_entry, tenancy=tenancy, user_id=user_id, private_pem=private_pem)

    previous = fingerprint(previous_pem)
    current = fingerprint(private_pem)
    for existing in iam.api_keys(user_id):
        if existing != current:
            iam.delete_api_key(user_id, existing)
    log.info('seed rotated: %s -> %s', previous, current)
    return current
