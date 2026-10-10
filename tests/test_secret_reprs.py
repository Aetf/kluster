"""No record in the credential surface prints a secret it carries.

A dataclass gets a repr for free, and the free one prints every field. That is
one `%r` in a log line, or one `assert a == b` between two of them that pytest
then renders, away from putting a live credential into a transcript that
outlives the run — and a transcript is exactly where a credential cannot be
retired from, because nobody knows it is there.

The protection is two annotations on every secret field, and neither covers
for the other. `field(repr=False)` keeps the value out of the repr, which is
what a log line's `%r` prints and what a failed assertion's first line prints
for each side. That is all it does: it keeps the value out of a log line, not
out of an assertion diff. Pytest explains a failed `==` between two dataclasses
of one type by drilling into every field that takes part in the comparison —
`compare=True`, the default — whatever its repr, and prints each one that
differs as `field: <left> != <right>`. `field(compare=False)` is what keeps a
secret out of that half.

Both are annotations and therefore forgettable. What makes forgetting either
fail is the marker a secret field is declared with, `Annotated[<its type>,
Secret]` (`kluster.lib.secret`): every marked field of every record in the
package is held to both here, the fields read off the marker rather than off a
list, and the leak checks below put the secret sentinel in exactly those
fields before printing the record. What the marker cannot catch is a
credential in a field nobody marked, and that residual is stated where the
marker is declared.

**The modules `MODULES` names do not carry the marker yet, and a census holds
them instead**: it pins, for every record in them, which of its fields carry
secrets and which do not, and then holds both halves against the classes
themselves. A field added to any of them fails here until its author has said
which half it is in — the failure mode being guarded is a field added later to
a class that was safe when it was written. A module leaves the census on the
commit that marks its secret fields (the remaining modules are queued on
kluster-ops#587), and from then on it is the marker's residual that applies to
it: a new field there is held to nothing until it is marked.

**Out of comparison means that two records differing only in a secret are
equal and hash alike**: a set or a mapping keyed by them keeps one of two
credentials, and a record whose only unhashable field is a secret mapping
hashes instead of refusing. So none of them is compared by value, kept in a
set, used as a mapping key or cached over, and no record is named as an
exception: every secret field is held to both annotations. A test that
compares two of them compares their public fields, and a test that has to know
which secret a record carries reads the field itself, as the SSH provider's
tests read the key a session opened with. A record declared with `eq=False` —
`pki.Authority` — is held to the rule all the same: pytest drills into a
dataclass whose `__eq__` is `object`'s exactly as into a generated one.

**A record is a dataclass, a `NamedTuple` or a `TypedDict`**, which is the set
of constructs that print their contents: everything else inherits `object`'s
repr and discloses nothing but a type and an address. Only the first of the
three can hide a field, so the rule for the other two is not an annotation but
a prohibition — a `NamedTuple` or a `TypedDict` may carry no secret at all, and
both the marker's case and the census refuse one that says it does.

**The census's modules are the ones that hold credentials**, a component's
module as readily as a script's. Its boundary is deliberate rather than
exhaustive: it proves nothing about a class in a module it does not name, and
a module is covered whole or not at all, because a record censused by class
alone leaves the next record in the same module uncaught. The marker has no
such boundary: the package is walked whole.

**A field typed `pulumi.Input[str]` is classified by what it holds, not by the
`Output` it holds in production.** An `Output`'s repr discloses nothing, but
the type promises nothing about arriving as one — a test hands over the
literal — so a field of that type carrying a credential is hidden like any
other. `routing`'s session password is one, marked; `container` and `k8s` are
censused for that class of field: a mounted file's contents, an initial
state's contents, the ACME token, and a produced Secret's own data.

**A field that holds a library's object is classified by what the object
holds, not by what it prints**, as `pki.Authority`'s key is. `kdbx` and
`adopt` are here for that class of field: an unlocked `PyKeePass` holds the
master password and prints only its type and address, and an OCI client holds
the configuration it was built from, the API key inside it. Such a field is
filled with the kind of value it holds in production wherever that value
prints what it carries, and with the bare sentinel where it does not
(`SHAPED`).

**A field that holds a record is classified by what that record prints**, in
its repr and in pytest's explanation of a comparison, not by what it holds:
`pulumi_config.Stack` carries the stack passphrase through a
`BackendEnvironment` that hides it in both, and the last two tests here pin
that mechanism.
The one exception is a field already out of the repr for another reason —
`Context`'s three caches, the forge with its admin token among them — which
the census records as secret, since every hidden field has to be one it can
name.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import pkgutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, ForwardRef, NamedTuple, TypedDict, cast, final, get_args, get_origin, is_typeddict

import pulumi
import pytest

import kluster
from kluster.components.gateway import container
from kluster.lib import k8s
from kluster.lib.secret import Secret
from kluster.lib.state_backend import render
from kluster.providers.device_files import ssh
from kluster.scripts.credentials import (
    b2,
    escrow,
    github_secrets,
    kdbx,
    masters,
    oci_iam,
    payload,
    pulumi_config,
    slots,
)
from kluster.scripts.state_backend import adopt, config

#: The sentinel written into every secret field of every record built below, and looked for
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


#: The modules whose records this census covers: the ones whose secret fields
#: do not carry the marker yet.
MODULES: tuple[ModuleType, ...] = (
    escrow,
    kdbx,
    masters,
    oci_iam,
    payload,
    pulumi_config,
    slots,
    config,
    render,
    adopt,
    ssh,
    container,
    k8s,
)

CENSUS: dict[type, Census] = {
    escrow.Console: Census('steps kit'),
    escrow.Generated: Census('mint'),
    escrow.KitAttachment: Census('entry filename'),
    escrow.Label: Census('name what origin shape slot'),
    escrow.Registry: Census('root'),
    escrow.Shape: Census('looks_like matches'),
    escrow.Vault: Census('registry identity', secret='identity'),
    escrow.WorkstationSlot: Census('path read_by store'),
    # The unlocked database, which holds the master password as `.password`.
    # Hidden for what it holds rather than for how `PyKeePass` prints, as
    # `pki.Authority`'s key is; `SHAPED` says why it is filled with the bare sentinel.
    kdbx.KdbxStore: Census('path _db', secret='_db'),
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
    # The raw answer, which for a mint carries the credential the provider
    # discloses once.
    payload.Payload: Census('where fields', secret='fields'),
    # `operator` and `physical` are where the operator passphrase and
    # `physical`'s are found, functions, which hold no value to print.
    pulumi_config.BackendEnvironment: Census('passphrase url operator physical', secret='passphrase'),
    pulumi_config.Stack: Census('name directory environment run'),
    # The three caches hold the forge, an opened escrow and the backend
    # environment, each a record with a secret of its own, and are out of the
    # repr and out of comparison for that reason as well as for being caches.
    # `open_forge` is a function, which holds no value to print.
    slots.Context: Census(
        'open_forge open_vault open_environment project runner ask _forge _vault _environment',
        secret='_forge _vault _environment',
    ),
    slots.Decided: Census('where constant'),
    slots.Derived: Census('label'),
    slots.DeviceSecret: Census('what'),
    slots.EscrowCopy: Census('label'),
    slots.Issued: Census('role'),
    slots.Manual: Census('describes console command taken'),
    slots.Minted: Census('command unbuilt'),
    slots.OnBox: Census('what'),
    slots.PulumiConfig: Census('stack key'),
    slots.PulumiState: Census('stack what'),
    slots.Row: Census('register source targets pending'),
    slots.SealedSecret: Census('what'),
    slots.StateRead: Census('stack output'),
    slots.SecretStore: Census('key'),
    slots.WorkstationSlot: Census('name'),
    config.ClientBundle: Census('name address ca_cert cert key', secret='key'),
    render.Machine: Census(
        'operator_keys postgres_uid postgres_image database ci_role operator_role ca_cert server_cert '
        'server_key ssh_host_key age_recipients age_url age_sha256 b2_dump_key_id b2_dump_key b2_bucket_id '
        'b2_prefix dump_script dump_schedule reboot_day reboot_time reboot_window_minutes',
        secret='server_key ssh_host_key b2_dump_key',
    ),
    # The machine as the Butane template reads it: every field of the one
    # above, and the host key's public half.
    render._Parameters: Census(  # pyright: ignore[reportPrivateUsage]
        'operator_keys postgres_uid postgres_image database ci_role operator_role ca_cert server_cert '
        'server_key ssh_host_key age_recipients age_url age_sha256 b2_dump_key_id b2_dump_key b2_bucket_id '
        'b2_prefix dump_script dump_schedule reboot_day reboot_time reboot_window_minutes ssh_host_key_pub',
        secret='server_key ssh_host_key b2_dump_key',
    ),
    config.Roots: Census('ca age_recipients'),
    # The OCI clients `adopt` reads through, each built from a configuration
    # carrying the stack's OCI key.
    adopt.Clients: Census('network object_storage buckets', secret='network object_storage'),
    ssh.CommandResult: Census('exit_status stdout stderr'),
    ssh.Device: Census('host username private_key host_key port', secret='private_key'),
    ssh.FileStat: Census('owner group mode size kind'),
    container.BridgedDeclaration: Census('service pin'),
    # The zone-scoped token the proxy answers DNS-01 challenges with.
    container.CaddyService: Census('service pin acme_token vhosts legacy', secret='acme_token'),
    container.ContainerDeclaration: Census('service pin'),
    # An initial state is a service's own configuration.
    container.InitialState: Census('into content', secret='content'),
    # With `secret` set, a credential the image reads (the ACME token today);
    # hidden whatever the file holds, since the field is one.
    container.MountedFile: Census('name target content secret', secret='content'),
    container.OverlayDaemon: Census('service pin'),
    container.ResolverService: Census('service pin'),
    container.Rootfs: Census('repository tag digest'),
    container._AdguardInitialParams: Census(  # pyright: ignore[reportPrivateUsage]
        'cluster address api_port upstreams gateway_zones gateway_resolver'
    ),
    container._CaddyParams: Census(  # pyright: ignore[reportPrivateUsage]
        'acme_contact zone controller controller_upstream token_path api_port vhosts legacy_zone legacy'
    ),
    container._MachineParams: Census(  # pyright: ignore[reportPrivateUsage]
        'cluster name capability kill_signal kill_gracetime environment bridge host_network state_bind devices binds'
    ),
    container._ResolvParams: Census('cluster resolver'),  # pyright: ignore[reportPrivateUsage]
    # The produced Secret's own data, template holes and all.
    k8s.SecretTemplate: Census('data type immutable labels annotations', secret='data'),
}


@final
@dataclass(frozen=True)
class Record:
    """One record under test: every field it has, and the subset it may not print.

    Built from a census row for a module the census holds, and from the marker
    for every other record that carries one.
    """

    cls: type
    #: Every field of the record, in declaration order.
    names: tuple[str, ...]
    #: The subset that carries credential material.
    secrets: tuple[str, ...]

    @property
    def id(self) -> str:
        """The record's dotted path under `kluster`, which is what a failure names."""
        return f'{self.cls.__module__.removeprefix(f"{kluster.__name__}.")}.{self.cls.__qualname__}'


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
            return cast('tuple[str, ...]', cls._fields)
        case _:
            return tuple(spec.name for spec in dataclasses.fields(cls))  # pyright: ignore[reportArgumentType]


def _declared_types(cls: type) -> dict[str, object]:
    """Each field's declared type, evaluated as far as its outermost layer.

    Evaluated in the namespace of the class that declares the field, which for
    an inherited field is a base's, and no further than the annotation's own
    text: the marker sits in that text, while resolving the whole type would
    also have to resolve `pulumi.Input`, whose definition names `Output` by a
    string only Pulumi's own module can evaluate. A `NamedTuple` or a
    `TypedDict` keeps each annotation as a forward reference instead of a
    string, so that is evaluated the same way.
    """
    declared: dict[str, object] = {}
    for base in reversed(cls.__mro__):
        for name, annotation in inspect.get_annotations(base, eval_str=True).items():
            if isinstance(annotation, ForwardRef):
                module = annotation.__forward_module__ or base.__module__
                annotation = eval(annotation.__forward_arg__, vars(sys.modules[module]))
            declared[name] = annotation
    return declared


def _carries_marker(annotation: object) -> bool:
    """Whether a declared type names `Secret` as `Annotated` metadata, at any depth.

    Any depth, because a marked type wrapped again is still marked:
    `Annotated[str, Secret] | None` is a secret field that may be absent, and a
    mapping of marked values is a field of secrets.
    """
    if get_origin(annotation) is Annotated:
        metadata = cast('tuple[object, ...]', getattr(annotation, '__metadata__', ()))
        if Secret in metadata:
            return True
    return any(_carries_marker(argument) for argument in get_args(annotation))


def _marked(cls: type) -> tuple[str, ...]:
    """The fields of a record that carry the marker, in declaration order."""
    declared = _declared_types(cls)
    return tuple(name for name in _field_names(cls) if _carries_marker(declared[name]))


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


def _uncompared(cls: type) -> set[str]:
    """The fields a dataclass leaves out of its equality, and so out of pytest's diff of two of it."""
    return {spec.name for spec in dataclasses.fields(cls) if not spec.compare}  # pyright: ignore[reportArgumentType]


def _package() -> tuple[ModuleType, ...]:
    """Every module of `kluster`, imported.

    Imported by name rather than through the walk's own import, which drops a
    subpackage that fails to import without a word: a module that does not
    import fails here, by name, instead of leaving its records unchecked. A
    `__main__` would run its program on import, and is left out.
    """
    return tuple(
        importlib.import_module(info.name)
        for info in pkgutil.walk_packages(kluster.__path__, f'{kluster.__name__}.')
        if info.name.rsplit('.', 1)[1] != '__main__'
    )


#: Every record in the package that carries the marker on at least one field,
#: with the marked fields as its secrets.
MARKED: list[Record] = sorted(
    (
        Record(cls, _field_names(cls), secrets)
        for module in _package()
        for cls in _declared(module)
        if (secrets := _marked(cls))
    ),
    key=lambda record: record.id,
)

#: Every record the census holds, with its secrets as the census lists them.
CENSUSED: list[Record] = sorted(
    (Record(cls, entry.names, entry.secrets) for cls, entry in CENSUS.items()), key=lambda record: record.id
)


def _records(records: list[Record]) -> pytest.MarkDecorator:
    """Drive a per-record test over `records`, so that a failure names the record rather than a row number."""
    return pytest.mark.parametrize('record', records, ids=[record.id for record in records])


def _signing_configuration(key: str) -> dict[str, object]:
    """What an OCI client holds as `adopt` builds one: a configuration, its key's content inside it."""
    return {
        'user': 'ocid1.user.oc1..census',
        'fingerprint': '00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff',
        'key_content': key,
        'tenancy': 'ocid1.tenancy.oc1..census',
        'region': 'us-ashburn-1',
    }


#: The secret fields filled with the kind of value they hold in production, the
#: sentinel inside it, rather than with the bare sentinel. What the tests that
#: fill a record measure is whether the annotations keep a field's printed form
#: out, so a field is filled this way when its production value prints what it
#: carries: a dict prints every entry, and an SDK model prints every attribute.
#:
#: `KdbxStore._db` is the field that keeps the bare sentinel, and the reason is
#: the same measurement. Its production value is a `PyKeePass`, which prints a
#: type and an address, and pytest explains a differing field holding one by
#: that repr alone: it reads the attributes of dataclasses, attrs classes and
#: named tuples, and of nothing else. Filled with a database, or with a
#: stand-in carrying a `password` attribute, the row would pass with both
#: annotations removed.
#: `test_an_unlocked_kit_prints_no_password` builds
#: a real unlocked store and holds what this row rests on, that the store keeps
#: the password as `.password`; it passes with both of `_db`'s annotations
#: removed, because the real object prints no password either way, so the row
#: filled with the bare sentinel is what fails when either annotation is dropped.
SHAPED: dict[tuple[type, str], Callable[[str], object]] = {
    (adopt.Clients, 'network'): _signing_configuration,
    (adopt.Clients, 'object_storage'): _signing_configuration,
}


def _filled(record: Record, side: str = '') -> object:
    """One instance of the record with a sentinel in every field, built past its constructor.

    Assigned rather than constructed because what is under test is the repr
    and pytest's explanation of a comparison, which read attributes and
    nothing else: a constructor would demand certificates, sessions and OCIDs
    of the right shape from every record here, and none of that would make the
    assertion stronger.

    `side` is appended to every sentinel, so that two instances built with two
    sides differ in every field and a comparison of them has something to
    report in each. A field `SHAPED` names gets its sentinel inside the value
    that table builds.
    """
    # `cast` because `object.__new__` is typed against `type[Self]`, and the
    # record holds plain `type`: what comes back is an instance either way.
    instance = cast('object', object.__new__(record.cls))
    for field_name in record.names:
        sentinel = (SECRET if field_name in record.secrets else PUBLIC) + side
        shape = SHAPED.get((record.cls, field_name))
        object.__setattr__(instance, field_name, sentinel if shape is None else shape(sentinel))
    return instance


def _explained(config: pytest.Config, left: object, right: object) -> str:
    """What pytest reports for a failed `left == right`, whole.

    The hook a rewritten `assert` calls to explain a failed comparison, called
    directly. Directly rather than through a failing `assert`, because the
    rewriting truncates the explanation at default verbosity outside CI: a
    secret printed below the cut would then pass here and print on CI.
    """
    answers = cast(
        'list[list[str]]', config.hook.pytest_assertrepr_compare(config=config, op='==', left=left, right=right)
    )
    return '\n'.join(line for answer in answers for line in answer)


@final
@dataclass(frozen=True)
class _MarkedRecord:
    """A dataclass declaring the marker in each form the reader has to find it in, beside a field without it."""

    plain: str
    outermost: Annotated[str, Secret]
    pulumi_facing: Annotated[pulumi.Input[str], Secret]
    optional: Annotated[str, Secret] | None
    nested: Mapping[str, Annotated[str, Secret]]


class _MarkedTuple(NamedTuple):
    """A `NamedTuple` declaring the marker, which keeps its annotations as forward references."""

    plain: str
    outermost: Annotated[str, Secret]


class _MarkedDict(TypedDict):
    """A `TypedDict` declaring the marker, which keeps its annotations as forward references."""

    plain: str
    outermost: Annotated[str, Secret]


@pytest.mark.parametrize(
    ('cls', 'marked'),
    [
        (_MarkedRecord, ('outermost', 'pulumi_facing', 'optional', 'nested')),
        (_MarkedTuple, ('outermost',)),
        (_MarkedDict, ('outermost',)),
    ],
    ids=['dataclass', 'NamedTuple', 'TypedDict'],
)
def test_the_marker_is_read_wherever_a_field_declares_it(cls: type, marked: tuple[str, ...]) -> None:
    """The reader every case below is driven by, on records declaring the marker in every form it takes.

    A form the reader missed would drop its fields out of `MARKED` without a
    word, and every case over `MARKED` would then pass by never seeing them.
    """
    assert _marked(cls) == marked


def test_the_package_has_marked_records() -> None:
    """The walk reached the package's modules: an empty `MARKED` would pass every case over it."""
    assert MARKED, 'no record in the package carries the marker'


@_records(MARKED)
def test_every_marked_field_is_out_of_the_repr_and_out_of_comparison(record: Record) -> None:
    """The relation the marker exists for, held against every marked field in the package.

    The fields come off the marker, so there is no list to keep: a field is
    held here from the commit that marks it. A `NamedTuple` or a `TypedDict`
    has no per-field repr control, so one that marks a field is refused the
    field rather than held to annotations it cannot carry.
    """
    kind = _kind(record.cls)
    assert kind == 'dataclass', (
        f'{record.id} is a {kind} and cannot keep a field out of its repr: '
        f'carry {", ".join(record.secrets)} in a dataclass instead'
    )
    printed = sorted(set(record.secrets) - _hidden(record.cls))
    compared = sorted(set(record.secrets) - _uncompared(record.cls))

    assert not printed, f'{record.id} prints {", ".join(printed)}: declare it field(repr=False)'
    assert not compared, f'{record.id} compares {", ".join(compared)}: declare it field(compare=False)'


@pytest.mark.parametrize('module', MODULES, ids=[module.__name__.rsplit('.', 1)[1] for module in MODULES])
def test_a_censused_module_carries_no_marker(module: ModuleType) -> None:
    """A module is held by the census or by the marker, so that the two never disagree about one record.

    The commit that marks a module's secret fields takes the module out of
    `MODULES` and its rows out of `CENSUS` in the same change.
    """
    marked = sorted(record.id for record in MARKED if record.cls.__module__ == module.__name__)

    assert not marked, f'{module.__name__} carries the marker, so it leaves the census: {", ".join(marked)}'


@pytest.mark.parametrize('module', MODULES, ids=[module.__name__.rsplit('.', 1)[1] for module in MODULES])
def test_every_record_in_these_modules_is_censused(module: ModuleType) -> None:
    """A new record joins the census, rather than arriving unclassified.

    The half that keeps this from being a list somebody forgets: a dataclass
    added to one of these modules fails here on the commit that adds it, which
    is where deciding whether it carries a credential costs nothing.
    """
    missing = sorted(cls.__name__ for cls in _declared(module) - set(CENSUS))

    assert not missing, f'{module.__name__} declares records this census does not classify: {", ".join(missing)}'


@_records(CENSUSED)
def test_the_census_names_exactly_the_fields_the_record_has(record: Record) -> None:
    """And so a field added to a censused record fails until it is classified."""
    assert _field_names(record.cls) == record.names, record.id
    assert set(record.secrets) <= set(record.names), f'{record.id} calls a field secret that it does not have'


@_records(CENSUSED)
def test_every_secret_field_is_declared_out_of_the_repr(record: Record) -> None:
    """`field(repr=False)`, held against the census rather than against a reading of the file."""
    assert _hidden(record.cls) == set(record.secrets), record.id


@_records(CENSUSED)
def test_a_record_that_cannot_hide_a_field_carries_no_secret(record: Record) -> None:
    """A `NamedTuple` or a `TypedDict` is refused the secret rather than annotated.

    Neither construct has a per-field repr control, so there is nothing to set
    and the census would be recording a protection that does not exist. What is
    left is to keep credential material out of them, which is a design rule and
    is held here.
    """
    kind = _kind(record.cls)
    if kind == 'dataclass':
        return
    assert not record.secrets, (
        f'{record.id} is a {kind} and cannot keep a field out of its repr: '
        f'carry {", ".join(record.secrets)} in a dataclass instead'
    )


@_records(CENSUSED + MARKED)
def test_a_filled_record_prints_none_of_its_secrets(record: Record) -> None:
    """The property itself, measured on a repr rather than inferred from an annotation."""
    if _kind(record.cls) != 'dataclass':
        # Nothing to measure: the cases above forbid the secret outright, and
        # neither of the other two constructs can be built past its constructor
        # the way `_filled` builds a dataclass.
        return
    instance = _filled(record)
    printed = repr(instance)

    # The record was actually filled, with a value that prints its secret: an
    # assertion that a sentinel is absent from a repr proves nothing if the
    # sentinel never reached the object, or reached it in a form that prints
    # nothing.
    for field_name in record.secrets:
        assert SECRET in repr(getattr(instance, field_name)), f'{record.id}.{field_name} was not filled'
    assert SECRET not in printed, f'{record.id} prints a secret: {printed}'
    if record.names:
        assert PUBLIC in printed or not set(record.names) - set(record.secrets), (
            f'{record.id} prints nothing at all, so the assertion above is vacuous'
        )


@_records(CENSUSED + MARKED)
def test_a_failed_comparison_of_two_filled_records_prints_none_of_their_secrets(
    pytestconfig: pytest.Config, record: Record
) -> None:
    """The property itself, measured on pytest's explanation rather than inferred from an annotation."""
    if _kind(record.cls) != 'dataclass':
        return
    explained = _explained(pytestconfig, _filled(record, '-left'), _filled(record, '-right'))

    assert SECRET not in explained, f'{record.id} prints a secret in a failed comparison:\n{explained}'
    if set(record.names) - set(record.secrets) - _uncompared(record.cls):
        assert f'{PUBLIC}-left' in explained, (
            f'{record.id} explains no differing field at all, so the assertion above is vacuous:\n{explained}'
        )


def test_an_unlocked_kit_prints_no_password(pytestconfig: pytest.Config, tmp_path: Path) -> None:
    """The library object behind `_db`, built by its library rather than filled.

    An unlocked `PyKeePass` holds the master password as `.password`. It is
    checked to hold it before its record is checked not to print it, so the
    case does not pass by the secret never having arrived.
    """
    kit = kdbx.KdbxStore.create(tmp_path / 'kit.kdbx', SECRET)
    other = kdbx.KdbxStore.create(tmp_path / 'other.kdbx', f'{SECRET}-other')
    database: Any = kit._db  # pyright: ignore[reportPrivateUsage]

    assert database.password == SECRET, 'the store was not unlocked with the password'
    assert SECRET not in repr(kit)
    explained = _explained(pytestconfig, kit, other)
    assert SECRET not in explained, explained
    assert 'other.kdbx' in explained, 'the explanation reached the path, which is what tells two kits apart'


def test_a_record_that_carries_another_prints_no_secret_of_the_inner_one(pytestconfig: pytest.Config) -> None:
    """Nesting is covered by the inner record's own repr and comparison, and that is the whole mechanism.

    `MintedKey` and `_SeedSession` are pairs whose halves are the credentials:
    neither hides a field of its own, and neither needs to, because the repr it
    builds is made of the reprs beneath it, and pytest explains a differing
    inner record by drilling into that record's own compared fields. Worth
    pinning because the alternative design — hiding the *containing* field —
    would look equally correct and would hide the identifiers a refusal is read
    by.
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

    rotated = b2.MintedKey(
        session=b2.Session(account_id='account', api_url='https://api.example', token=f'{SECRET}-rotated'),
        app_key=b2.AppKey(key_id='rotated-key-id', key=f'{SECRET}-rotated'),
    )
    explained = _explained(pytestconfig, minted, rotated)

    assert SECRET not in explained, explained
    assert 'rotated-key-id' in explained, 'the explanation reached the inner record, where the key is'


def test_a_context_prints_neither_the_token_nor_the_passphrase_it_reaches() -> None:
    """The two records a push reaches everything through, by the same mechanism.

    `slots.Context` is what every row's push is handed, and `pulumi_config.Stack`
    is what a config push runs as: one carries the forge's admin token, the
    `github` stack's config secret (framework/github.md §1), the other the
    passphrase that opens every stack's committed configuration.
    The context opens the forge only when a row needs it and keeps what it
    opened, so it is read once here before the context is printed, and the
    forge is printed on its own as well: the inner record's own repr is the
    mechanism for it as for the stack's environment, so this is where a
    regression in either inner record would show first.
    """
    environment = pulumi_config.BackendEnvironment(
        passphrase=SECRET, url='https://backend.example', operator=lambda: SECRET
    )
    context = slots.Context(
        open_forge=lambda: github_secrets.Forge(token=SECRET),
        open_vault=lambda: cast('escrow.Vault', object()),
        open_environment=lambda: environment,
        project=Path('.'),
    )
    stack = pulumi_config.Stack(name='dns', directory=Path('.'), environment=environment)

    assert context.forge.token == SECRET and stack.environment.passphrase == SECRET, 'the records were not filled'
    assert SECRET not in repr(context)
    assert SECRET not in repr(context.forge)
    assert SECRET not in repr(stack)
    assert 'https://backend.example' in repr(stack), 'the URL still prints: it says which backend a run opened'
