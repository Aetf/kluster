"""No record in the credential surface prints a secret it carries.

A dataclass gets a repr for free, and the free one prints every field. That is
one `%r` in a log line, or one `assert a == b` between two of them that pytest
then renders, away from putting a live credential into a transcript that
outlives the run — and a transcript is exactly where a credential cannot be
retired from, because nobody knows it is there.

The protection is `field(repr=False)`, which is an annotation and therefore
forgettable. This is the census that makes forgetting it fail: it pins, for
every record in the modules below, which of its fields carry secrets and which
do not, and then holds both halves against the classes themselves. A field
added to any of them fails here until its author has said which half it is in —
which is the point, because the failure mode being guarded is a field added
later to a class that was safe when it was written.

**A record is a dataclass, a `NamedTuple` or a `TypedDict`**, which is the set
of constructs that print their contents: everything else inherits `object`'s
repr and discloses nothing but a type and an address. Only the first of the
three can hide a field, so the rule for the other two is not an annotation but
a prohibition — a `NamedTuple` or a `TypedDict` in these modules may carry no
secret at all, and the census refuses one that says it does.

**The modules are the ones that hold credentials**, and a module that grows a
record carrying one joins the list. The boundary is deliberate rather than
exhaustive: this proves nothing about a class it does not name.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from types import ModuleType
from typing import cast, final, is_typeddict

import pytest

from kluster.providers.device_files import ssh
from kluster.scripts.credentials import age, b2, cloudflare, escrow, masters, oci_iam
from kluster.scripts.state_backend import config

#: Written into every secret field of every record built below, and looked for
#: in the repr. Distinctive because the assertion that it is *absent* is only
#: worth anything if a value that shape could not have arrived by accident.
SECRET = 'SECRET-fbb1a7-MUST-NOT-BE-PRINTED'

#: Written into every field that is not a secret, so that a repr which prints
#: nothing at all cannot pass by saying nothing.
PUBLIC = 'public-fbb1a7'


@final
@dataclass(frozen=True)
class Census:
    """One record, split into what it may print and what it may not.

    Both halves are written out rather than one derived from the other: the
    whole point is that adding a field to a censused class is a failure until
    somebody classifies it, and a derived half would classify it silently.

    Space-separated because these are names in declaration order and the lists
    are read far more often than they are edited.
    """

    #: Every field of the class, in declaration order.
    all: str
    #: The subset that carries credential material.
    secret: str = ''

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self.all.split())

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(self.secret.split())


#: The modules whose records this census covers.
MODULES: tuple[ModuleType, ...] = (age, b2, cloudflare, escrow, masters, oci_iam, config, ssh)

CENSUS: dict[type, Census] = {
    age.Identity: Census('secret public', secret='secret'),
    b2.AppKey: Census('key_id key', secret='key'),
    b2.Bucket: Census('bucket_id lifecycle_rules'),
    b2.KeyPage: Census('keys next_key_id'),
    b2.LifecycleRule: Census('file_name_prefix days_from_uploading_to_hiding days_from_hiding_to_deleting'),
    b2.ListedKey: Census('key_id name capabilities bucket_id name_prefix'),
    b2.MintedKey: Census('session app_key'),
    b2.Role: Census('name capabilities bucket_id name_prefix'),
    b2.Session: Census('account_id api_url token', secret='token'),
    cloudflare.PermissionGroup: Census('group_id name scopes'),
    cloudflare.Role: Census('name permissions'),
    cloudflare.Session: Census('token token_id', secret='token'),
    cloudflare.Token: Census('token_id value', secret='value'),
    cloudflare.TokenSummary: Census('token_id name'),
    cloudflare.VerifiedToken: Census('token_id status'),
    cloudflare.Zone: Census('zone_id name account_id'),
    cloudflare.ZoneToken: Census('token_id value account_id zone_ids', secret='value'),
    escrow.Console: Census('steps kit'),
    escrow.Generated: Census('mint'),
    escrow.KitAttachment: Census('entry filename'),
    escrow.Label: Census('name what origin shape slot'),
    escrow.Registry: Census('root'),
    escrow.Shape: Census('looks_like matches'),
    escrow.Vault: Census('registry identity', secret='identity'),
    escrow.WorkstationSlot: Census('path read_by'),
    masters.Credential: Census('root values', secret='values'),
    masters.Field: Census('name describes file env kind'),
    masters.Root: Census('member title console fields'),
    oci_iam.ApiKey: Census('tenancy user private_key', secret='private_key'),
    oci_iam.Domain: Census('client url'),
    oci_iam.Iam: Census('tenancy identity caller connect domain'),
    oci_iam.Identity: Census('name email section statements'),
    oci_iam.KeyPair: Census('private_pem public_pem', secret='private_pem'),
    oci_iam.Policy: Census('ocid name statements'),
    oci_iam.Principal: Census('ocid handle'),
    oci_iam.SeedRow: Census('tenancy user private_key', secret='private_key'),
    oci_iam.TenancyDomain: Census('url display_name kind'),
    oci_iam._ConsumerPolicyParams: Census('group compartment_id'),  # pyright: ignore[reportPrivateUsage]
    oci_iam._SeedPolicyParams: Census('group'),  # pyright: ignore[reportPrivateUsage]
    oci_iam._SeedSession: Census('iam row'),  # pyright: ignore[reportPrivateUsage]
    config.ClientBundle: Census('name address ca_cert cert key', secret='key'),
    config.Machine: Census(
        'operator_keys postgres_uid postgres_image database ci_role operator_role ca_cert server_cert '
        'server_key age_recipients age_url age_sha256 b2_dump_key_id b2_dump_key b2_bucket_id b2_prefix '
        'dump_script dump_schedule reboot_day reboot_time reboot_window_minutes',
        secret='server_key b2_dump_key',
    ),
    config.Roots: Census('ca age_recipients'),
    ssh.CommandResult: Census('exit_status stdout stderr'),
    ssh.Device: Census('host username private_key host_key port', secret='private_key'),
    ssh.FileStat: Census('owner group mode size kind'),
}

CENSUSED: list[tuple[str, type, Census]] = sorted(
    ((f'{cls.__module__.rsplit(".", 1)[1]}.{cls.__name__}', cls, entry) for cls, entry in CENSUS.items()),
    key=lambda row: row[0],
)

#: The parameter list every per-record test below is driven by, so that a
#: failure names the record rather than a row number.
RECORDS = pytest.mark.parametrize(('name', 'cls', 'entry'), CENSUSED, ids=[row[0] for row in CENSUSED])


def _kind(cls: type) -> str:
    """Which of the three constructs `cls` is, or `''` if it is none of them.

    Recognized by what makes each one at runtime rather than by a base class: a
    `NamedTuple` is a tuple subclass carrying `_fields`, and a `TypedDict` has
    no base class left to test against by the time it is a class.
    """
    if is_typeddict(cls):
        return 'TypedDict'
    named_fields: object = getattr(cls, '_fields', None)
    if isinstance(named_fields, tuple) and issubclass(cls, tuple):
        return 'NamedTuple'
    # Last, because the type checker reads a negative `is_dataclass` as having
    # narrowed the class away and the checks above then take an unknown.
    return 'dataclass' if dataclasses.is_dataclass(cls) else ''


def _declared(module: ModuleType) -> set[type]:
    """The records this module defines, imports excluded."""
    return {
        value
        for value in vars(module).values()
        if isinstance(value, type) and _kind(value) and value.__module__ == module.__name__
    }


def _field_names(cls: type) -> tuple[str, ...]:
    """Every field of a record, in declaration order, whichever construct it is."""
    match _kind(cls):
        case 'TypedDict':
            annotations: dict[str, object] = dict(cls.__annotations__)
            return tuple(annotations)
        case 'NamedTuple':
            return cast('tuple[str, ...]', getattr(cls, '_fields'))
        case _:
            return tuple(spec.name for spec in dataclasses.fields(cls))  # pyright: ignore[reportArgumentType]


def _hidden(cls: type) -> set[str]:
    """The fields a record keeps out of its repr.

    Empty for everything but a dataclass, and not because those are safe: a
    `NamedTuple` and a `TypedDict` have no per-field repr control to set, which
    is why the census forbids a secret in one rather than asking for an
    annotation that does not exist.
    """
    if _kind(cls) != 'dataclass':
        return set()
    return {spec.name for spec in dataclasses.fields(cls) if not spec.repr}  # pyright: ignore[reportArgumentType]


def _filled(cls: type, entry: Census) -> object:
    """One instance of `cls` with a marker in every field, built past its constructor.

    Assigned rather than constructed because what is under test is the repr,
    which reads attributes and nothing else: a constructor would demand
    certificates, sessions and OCIDs of the right shape from every record in
    the census, and none of that would make the assertion stronger.
    """
    # `cast` because `object.__new__` is typed against `type[Self]`, and the
    # census holds plain `type`: what comes back is an instance either way.
    instance = cast('object', object.__new__(cls))
    for field_name in entry.names:
        marker = SECRET if field_name in entry.secrets else PUBLIC
        object.__setattr__(instance, field_name, marker)
    return instance


@pytest.mark.parametrize('module', MODULES, ids=[module.__name__.rsplit('.', 1)[1] for module in MODULES])
def test_every_record_in_these_modules_is_censused(module: ModuleType) -> None:
    """A new record joins the census, rather than arriving unclassified.

    The half that keeps this from being a list somebody forgets: a dataclass
    added to one of these modules fails here on the commit that adds it, which
    is where deciding whether it carries a credential costs nothing.
    """
    missing = sorted(cls.__name__ for cls in _declared(module) - set(CENSUS))

    assert not missing, f'{module.__name__} declares records this census does not classify: {", ".join(missing)}'


@RECORDS
def test_the_census_names_exactly_the_fields_the_record_has(name: str, cls: type, entry: Census) -> None:
    """And so a field added to a censused record fails until it is classified."""
    assert _field_names(cls) == entry.names, name
    assert set(entry.secrets) <= set(entry.names), f'{name} calls a field secret that it does not have'


@RECORDS
def test_every_secret_field_is_declared_out_of_the_repr(name: str, cls: type, entry: Census) -> None:
    """`field(repr=False)`, held against the census rather than against a reading of the file."""
    assert _hidden(cls) == set(entry.secrets), name


@RECORDS
def test_a_record_that_cannot_hide_a_field_carries_no_secret(name: str, cls: type, entry: Census) -> None:
    """A `NamedTuple` or a `TypedDict` is refused the secret rather than annotated.

    Neither construct has a per-field repr control, so there is nothing to set
    and the census would be recording a protection that does not exist. What is
    left is to keep credential material out of them, which is a design rule and
    is held here.
    """
    kind = _kind(cls)
    if kind == 'dataclass':
        return
    assert not entry.secrets, (
        f'{name} is a {kind} and cannot keep a field out of its repr: '
        f'carry {", ".join(entry.secrets)} in a dataclass instead'
    )


@RECORDS
def test_a_filled_record_prints_none_of_its_secrets(name: str, cls: type, entry: Census) -> None:
    """The property itself, measured on a repr rather than inferred from an annotation."""
    if _kind(cls) != 'dataclass':
        # Nothing to measure: the test above forbids the secret outright, and
        # neither of the other two constructs can be built past its constructor
        # the way `_filled` builds a dataclass.
        return
    instance = _filled(cls, entry)
    printed = repr(instance)

    # The record was actually filled: an assertion that a marker is absent from
    # a repr proves nothing if the marker never reached the object.
    for field_name in entry.secrets:
        assert getattr(instance, field_name) == SECRET, f'{name}.{field_name} was not filled'
    assert SECRET not in printed, f'{name} prints a secret: {printed}'
    if entry.names:
        assert PUBLIC in printed or not set(entry.names) - set(entry.secrets), (
            f'{name} prints nothing at all, so the assertion above is vacuous'
        )


def test_a_record_that_carries_another_prints_no_secret_of_the_inner_one() -> None:
    """Nesting is covered by the inner record's own repr, and that is the whole mechanism.

    `MintedKey` and `_SeedSession` are pairs whose halves are the credentials:
    neither hides a field of its own, and neither needs to, because the repr it
    builds is made of the reprs beneath it. Worth pinning because the
    alternative design — hiding the *containing* field — would look equally
    correct and would hide the identifiers a refusal is read by.
    """
    minted = b2.MintedKey(
        session=b2.Session(account_id='account', api_url='https://api.example', token=SECRET),
        app_key=b2.AppKey(key_id='key-id', key=SECRET),
    )
    seeded = oci_iam._SeedSession(  # pyright: ignore[reportPrivateUsage]
        iam=oci_iam.Iam(tenancy='ocid1.tenancy.oc1..account', identity=object(), caller='ocid1.user.oc1..seed'),
        row=oci_iam.SeedRow(tenancy='ocid1.tenancy.oc1..account', user='ocid1.user.oc1..seed', private_key=SECRET),
    )

    assert SECRET not in repr(minted)
    assert 'key-id' in repr(minted), 'the id still prints: it is what a console listing is matched against'
    assert SECRET not in repr(seeded)
    assert 'ocid1.user.oc1..seed' in repr(seeded), 'the OCIDs still print: they say which kit is open'
