"""The appliance's machine as values, rendered into Ignition, and digested.

The Butane template in `machine/` is the box; this module supplies the values
it is rendered with and hands the result to `butane` for validation and
conversion. The keys and recipients -- the certificates the escrowed CA
signs, the box's SSH identity, the age recipients, the write-only B2
credential -- are arguments: the `state-backend` stack reads them from its
configuration, its state and the files committed beside the template, and
`state-backend render` mints and recovers them for a scratch box
(`kluster.scripts.state_backend`), so nothing here opens the escrow.

The files in `machine/` are read through `kluster.lib.templates`, relative to
this package, so a render needs the package and nothing around it.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import subprocess as sp
from dataclasses import dataclass, field, fields
from importlib import resources
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kluster.lib import config as lib_config
from kluster.lib import templates

from . import settings

log = logging.getLogger(__name__)

#: The package the machine's files are found relative to, and the directory
#: inside it that holds them.
PACKAGE = 'kluster.lib.state_backend'
MACHINE = 'machine'

TEMPLATE = 'butane.yaml.j2'
DUMP_SCRIPT = 'state-dump.sh'
OPERATOR_KEYS = 'operator-keys.txt'


def machine_file(name: str) -> str:
    """One file of the machine, exactly as it is committed."""
    return templates.load(PACKAGE, f'{MACHINE}/{name}')


def operator_keys() -> tuple[str, ...]:
    """The public keys that may log in to the appliance.

    Refused by name when the file is missing or holds nothing: an empty list
    renders a Butane document that `butane --strict` accepts and that boots an
    appliance nobody can reach, which is a failure discovered an hour later
    after an image import and a launch.
    """
    with resources.as_file(resources.files(PACKAGE).joinpath(MACHINE, OPERATOR_KEYS)) as path:
        return lib_config.lines(path, 'the appliance operator keys')


class Digested(enum.Enum):
    """How one field of the machine enters the bill of materials the box carries.

    The stack's instance carries a digest per component in its
    `extendedMetadata`, in the clear (`bill_of_materials`), and a secret must
    not be digested as its value. `NEVER` is therefore the enum's name for
    "this field is a secret", which is why it decides the repr and the
    record's equality as well as the digest.
    """

    #: The value itself, JSON-encoded. The default, and the safe one.
    VALUE = 'value'
    #: Not digested as its value, because the value is a secret: its digest
    #: would travel in cloud metadata and its repr would travel in a
    #: transcript. A secret with a public half is read by that half instead.
    NEVER = 'never'


def _digested(how: Digested = Digested.VALUE) -> Any:
    """Declare a `Machine` field's digest treatment beside the field itself.

    The rule travels with the name it applies to, so renaming a field cannot
    leave a rule pointing at nothing — which for a `NEVER` field would mean
    putting a secret's digest into cloud metadata.

    A `NEVER` field is kept out of the repr and out of comparison by the same
    declaration, so a secret added to this record later is covered by saying
    the one thing its author has to say anyway rather than by remembering two
    more annotations. Comparison is the half a failed assertion reads: pytest
    prints every compared field that differs, repr or no repr.
    """
    secret = how is Digested.NEVER
    return field(metadata={'digest': how}, repr=not secret, compare=not secret)


@dataclass(frozen=True)
class Machine:
    """Everything the Butane template needs: the machine, as values.

    A record rather than a mapping because two things read it — the renderer,
    and the bill of materials a planned replacement names what moved by
    (`bill_of_materials`). A field the template uses but this does not carry
    would be a change no plan names, and the type checker is what holds the
    two lists together.
    """

    operator_keys: tuple[str, ...] = _digested()
    postgres_uid: int = _digested()
    postgres_image: str = _digested()
    database: str = _digested()
    ci_role: str = _digested()
    operator_role: str = _digested()
    #: The CA's certificate; its private half stays in the escrow.
    ca_cert: str = _digested()
    #: The server's certificate, for the reserved address. Rotating it, and the
    #: key below with it, is a reissue into the stack's configuration and then
    #: the replacement `--force` asks for.
    server_cert: str = _digested()
    server_key: str = _digested(Digested.NEVER)
    #: The box's SSH identity, in OpenSSH's own private-key format. Its public
    #: half is what `state-backend ssh` pins the connection against, committed
    #: beside the template.
    ssh_host_key: str = _digested(Digested.NEVER)
    age_recipients: tuple[str, ...] = _digested()
    age_url: str = _digested()
    age_sha256: str = _digested()
    b2_dump_key_id: str = _digested()
    #: A credential. Its identity is `b2_dump_key_id`, which moves with it;
    #: hashing the secret into cloud metadata buys nothing.
    b2_dump_key: str = _digested(Digested.NEVER)
    b2_bucket_id: str = _digested()
    b2_prefix: str = _digested()
    dump_script: str = _digested()
    dump_schedule: str = _digested()
    reboot_day: str = _digested()
    reboot_time: str = _digested()
    reboot_window_minutes: int = _digested()


@dataclass(frozen=True)
class _Parameters(Machine):
    """The names the Butane template's expressions use.

    The machine's fields, plus the one value the template needs that is
    *derived* from a field rather than stored beside it: the host key's public
    half, which the box carries as `ssh_host_key_pub` so that its fingerprint
    reaches the console banner. Derived rather than carried by `Machine`,
    because a second field could hold the public half of a different key than
    the private one beside it.
    """

    ssh_host_key_pub: str = field(kw_only=True)

    @classmethod
    def of(cls, values: Machine) -> _Parameters:
        carried = {spec.name: getattr(values, spec.name) for spec in fields(values)}
        return cls(**carried, ssh_host_key_pub=host_public_key(values))


def machine(
    *,
    ca_cert: str,
    server_cert: str,
    server_key: str,
    ssh_host_key: str,
    age_recipients: tuple[str, ...],
    dump_key_id: str,
    dump_key: str,
    bucket_id: str,
) -> Machine:
    """The machine this commit describes, around the keys and recipients given.

    The certificates are PEM, the host key is OpenSSH's own private-key
    format, and the recipients are age's public halves. Every other value is
    this commit's: the pins in `settings`, the operator keys and the dump
    script in `machine/`.
    """
    return Machine(
        operator_keys=operator_keys(),
        postgres_uid=settings.POSTGRES_UID,
        postgres_image=settings.POSTGRES_IMAGE,
        database=settings.DATABASE,
        ci_role=settings.CI_ROLE,
        operator_role=settings.OPERATOR_ROLE,
        ca_cert=ca_cert,
        server_cert=server_cert,
        server_key=server_key,
        ssh_host_key=ssh_host_key,
        age_recipients=age_recipients,
        age_url=settings.AGE_URL,
        age_sha256=settings.AGE_SHA256,
        b2_dump_key_id=dump_key_id,
        b2_dump_key=dump_key,
        b2_bucket_id=bucket_id,
        b2_prefix=settings.B2_PREFIX,
        dump_script=machine_file(DUMP_SCRIPT).strip(),
        dump_schedule=settings.DUMP_SCHEDULE,
        reboot_day=settings.REBOOT_DAY,
        reboot_time=settings.REBOOT_TIME,
        reboot_window_minutes=settings.REBOOT_WINDOW_MINUTES,
    )


def butane(values: Machine) -> str:
    """The Butane document for this machine: the template in `machine/`, rendered.

    Through `kluster.lib.templates`, whose settings make a forgotten parameter
    an error at render time rather than an empty line in the machine.
    """
    return templates.render(PACKAGE, f'{MACHINE}/{TEMPLATE}', _Parameters.of(values))


def render_ignition(values: Machine) -> str:
    """Butane in, validated Ignition out.

    Takes the machine rather than building one, so the Ignition and the bill
    of materials beside it (`bill_of_materials`) describe the one machine.
    """
    document = butane(values)
    log.info('handing %s to butane for validation and conversion to Ignition', TEMPLATE)
    proc = sp.run(
        ['butane', '--strict', '--pretty'],
        input=document,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'butane rejected the config:\n{proc.stderr}')
    return proc.stdout


def host_public_key(values: Machine) -> str:
    """The `ssh-ed25519 AAAA...` line for the host key this machine carries.

    What the box's console banner shows and what the bill of materials reads
    the host key by, derived from the private half rather than stored beside
    it: there is one value, so neither can name a key the box was never given.

    The type is checked rather than assumed: `load_ssh_private_key` answers
    for every algorithm OpenSSH has, and only this one is what `state-backend
    ssh` tells the client to accept.
    """
    key = serialization.load_ssh_private_key(values.ssh_host_key.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f'the machine carries a {type(key).__name__} host key, and ed25519 is what is pinned')
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )


def _server_public_key(values: Machine) -> str:
    """The server key's public half, as DER SubjectPublicKeyInfo in hex: what the certificate beside it must carry."""
    key = serialization.load_pem_private_key(values.server_key.encode(), password=None)
    return (
        key.public_key()
        .public_bytes(encoding=serialization.Encoding.DER, format=serialization.PublicFormat.SubjectPublicKeyInfo)
        .hex()
    )


#: How the stack's bill of materials reads each secret field: by its public
#: half, which names the key without being it. The dump key's secret has none,
#: and is read through its id (`b2_dump_key_id`), which moves with it.
_PUBLIC_HALVES = {
    'server_key': _server_public_key,
    'ssh_host_key': host_public_key,
}


def bill_of_materials(values: Machine) -> dict[str, str]:
    """A digest per component of the machine, for a box whose keys are stable (rfc-006 §4.4).

    The map the `state-backend` stack carries in the instance's
    `extendedMetadata`, in the clear: a planned replacement names there what
    moved, beside a `metadata` the diff can show only as secret. Every
    component is read as it is, the certificates whole, because the stack
    renders from keys that outlive every render; and each secret by its public
    half, so that rotating a key moves its own entry. The template itself is a
    component, comments included: it is the machine's definition, and a change
    to it is named without every value it interpolates changing. Nothing
    compares the map but the engine.
    """
    parts = {'butane': machine_file(TEMPLATE)}
    for spec in fields(values):
        value = getattr(values, spec.name)
        if spec.metadata.get('digest', Digested.VALUE) is not Digested.NEVER:
            parts[spec.name] = json.dumps(value, sort_keys=True, default=str)
        elif (public := _PUBLIC_HALVES.get(spec.name)) is not None:
            parts[spec.name] = public(values)
    return {key: hashlib.sha256(value.encode()).hexdigest()[:16] for key, value in sorted(parts.items())}
