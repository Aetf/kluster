"""The appliance's machine as values, rendered into Ignition, and digested.

The Butane template in `machine/` is the box; this module supplies the values
it is rendered with and hands the result to `butane` for validation and
conversion. The values that exist only at provision time -- the certificates
the escrowed CA signs, the box's SSH identity, the age recipients whose
identities the escrow holds, the write-only B2 credential -- are arguments:
minting or recovering one is the caller's (`kluster.scripts.state_backend`),
so nothing here opens the escrow.

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

from cryptography import x509
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
    """How one field of the machine enters the digest map the box carries.

    The converge compares a running box to this commit component by component
    (`digests`), and some fields cannot be compared as their value: the
    certificates are re-issued on every render, and the secrets must not be
    digested at all. `NEVER` is therefore the enum's name for "this field is a
    secret", which is why it decides the repr and the record's equality as
    well as the digest.
    """

    #: The value itself, JSON-encoded. The default, and the safe one.
    VALUE = 'value'
    #: A certificate, by subject, SANs and public key — "which CA is this".
    AUTHORITY = 'authority'
    #: A certificate, by subject and SANs only — "what does this box answer as".
    LEAF = 'leaf'
    #: Not compared at all, because the value is a secret: its digest would
    #: travel in cloud metadata and its repr would travel in a transcript.
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
    and the digest that decides whether a running box still matches the
    repository (`digests`). A field the template uses but this does not carry
    would be a change the converge cannot see, and the type checker is what
    holds the two lists together.
    """

    operator_keys: tuple[str, ...] = _digested()
    postgres_uid: int = _digested()
    postgres_image: str = _digested()
    database: str = _digested()
    ci_role: str = _digested()
    operator_role: str = _digested()
    #: The CA's private half comes from the escrow and outlives every render,
    #: so "which CA does this box chain to" is a fact about the box and a
    #: change to it is a rebuild.
    ca_cert: str = _digested(Digested.AUTHORITY)
    #: The leaf, compared by what it asserts and not by whose key it carries:
    #: a re-render legitimately issues a new key for the same machine.
    #: Rotating the server key therefore takes `provision --replace`.
    server_cert: str = _digested(Digested.LEAF)
    #: Random at every issuance (pki.py), so it describes this render rather
    #: than this machine.
    server_key: str = _digested(Digested.NEVER)
    #: The box's SSH identity, in OpenSSH's own private-key format, minted
    #: fresh by every render like the server key above and delivered the same
    #: way. `NEVER` for the same two reasons, and for one more: its public
    #: half is what `ssh` pins the connection against, and a digest of the
    #: private one in cloud metadata would buy nothing toward that.
    #: Rotating it is `provision --replace`.
    ssh_host_key: str = _digested(Digested.NEVER)
    age_recipients: tuple[str, ...] = _digested()
    age_url: str = _digested()
    age_sha256: str = _digested()
    b2_dump_key_id: str = _digested()
    #: A credential. Its *identity* is what the converge compares, and that is
    #: `b2_dump_key_id`; hashing the secret into cloud metadata buys nothing.
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
    the private one beside it -- and because it is minted per render, so a
    field would have to be excluded from the digest map by hand (`digests`).
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

    Takes the machine rather than building one, because every fact recorded
    beside the box has to come from the same render as the Ignition it boots
    with: when the server certificate inside that Ignition expires
    (`expires_at`), and the public half of the SSH host key it delivers
    (`host_public_key`). A second machine would carry a second certificate
    and a second host key, so the box would record an expiry belonging to a
    certificate it never held and be pinned to a key it was never given.
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

    What the launch records as the box's pin and what a `known_hosts` entry
    holds, derived from the private half rather than stored beside it: there
    is one value, so the pin cannot be the public half of a key the box was
    never given. A machine is the only place both exist at once, which is why
    this takes the machine rather than a key minted beside it.

    The type is checked rather than assumed: `load_ssh_private_key` answers
    for every algorithm OpenSSH has, and only this one is what the client is
    told to accept (`provision.ssh`).
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


def expires_at(values: Machine) -> str:
    """When the server certificate this machine carries stops being valid.

    Recorded on the box beside its digest map, because the bill of materials
    cannot see an expiry coming on its own: every component of it is
    re-derived from the repository, and the repository issues a fresh
    certificate on every render, so the intended side is always young. Only
    the box knows how old its own certificate is.
    """
    return x509.load_pem_x509_certificate(values.server_cert.encode()).not_valid_after_utc.isoformat()


def _identity(pem: str, *, with_key: bool) -> str:
    """A certificate reduced to what it *is*.

    Validity dates, serial numbers and signature bytes move on every issuance;
    the subject and the SANs do not. Digesting the latter is what lets a
    re-render be recognized as the same machine.
    """
    cert = x509.load_pem_x509_certificate(pem.encode())
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        san = sorted(str(name.value) for name in names)
    except x509.ExtensionNotFound:
        san = []
    spki = ''
    if with_key:
        spki = hashlib.sha256(
            cert.public_key().public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).hexdigest()
    return json.dumps([cert.subject.rfc4514_string(), san, spki])


def digests(values: Machine) -> dict[str, str]:
    """A digest per component of the machine, for comparing a box to the repo.

    Per component rather than one number so that a drifted box can say *what*
    drifted -- `butane`, `operator_keys`, `postgres_image` -- which is the
    difference between "re-provision, trust me" and a converge whose reason is
    readable. The map is small enough to travel in the instance's metadata,
    which is where the answer for a running box comes from.

    The template itself is a component: it is the machine's definition, and a
    change to it must be visible without every value it interpolates changing.
    That makes the template's text, comments included, part of what a running
    box is compared on.
    """
    parts = {'butane': machine_file(TEMPLATE)}
    for spec in fields(values):
        value = getattr(values, spec.name)
        match spec.metadata.get('digest', Digested.VALUE):
            case Digested.NEVER:
                continue
            case Digested.AUTHORITY:
                parts[spec.name] = _identity(value, with_key=True)
            case Digested.LEAF:
                parts[spec.name] = _identity(value, with_key=False)
            case _:
                parts[spec.name] = json.dumps(value, sort_keys=True, default=str)
    return {key: hashlib.sha256(value.encode()).hexdigest()[:16] for key, value in sorted(parts.items())}


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

    The `state-backend` stack's counterpart of `digests`, and the map it
    carries in the instance's `extendedMetadata`, in the clear: a planned
    replacement names there what moved, beside a `metadata` the diff can show
    only as secret. Every component is read as it is, the certificates whole,
    because the stack renders from keys that outlive every render rather than
    from ones issued per render; and each secret by its public half, so that
    rotating a key moves its own entry. The template's text is a component, as
    in `digests`. Nothing compares the map but the engine.
    """
    parts = {'butane': machine_file(TEMPLATE)}
    for spec in fields(values):
        value = getattr(values, spec.name)
        if spec.metadata.get('digest', Digested.VALUE) is not Digested.NEVER:
            parts[spec.name] = json.dumps(value, sort_keys=True, default=str)
        elif (public := _PUBLIC_HALVES.get(spec.name)) is not None:
            parts[spec.name] = public(values)
    return {key: hashlib.sha256(value.encode()).hexdigest()[:16] for key, value in sorted(parts.items())}
