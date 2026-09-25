"""Rendering the appliance's Ignition, and the client bundle that talks to it.

The Butane template is the box; this module supplies the values that only
exist at provision time — the reserved IP the server certificate is issued
for, the write-only B2 credential, the certificates the escrowed CA signs and
the age recipients whose identities the escrow holds — and hands the result to
`butane` for validation and conversion.
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
import json
import logging
import subprocess as sp
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from kluster.lib import config as lib_config
from kluster.scripts.credentials import age, escrow, pki, workstation

from . import settings

log = logging.getLogger(__name__)

#: `deploy/` sits outside the package: it is deployment material, not library
#: code, and the appliance's definition is meant to be readable on its own. So
#: it is found in the checkout this package runs from (`workstation.repo_root`),
#: and a copy of the package outside any checkout refuses by name on import
#: rather than reading whatever directory a fixed depth lands on.
DEPLOY_DIR = workstation.repo_root() / 'deploy' / 'state-backend'

TEMPLATE = 'butane.yaml.j2'
DUMP_SCRIPT = 'state-dump.sh'
OPERATOR_KEYS = 'operator-keys.txt'
#: The public half of the drill age identity (credentials.md §3), one line,
#: `#` comments allowed. Written by `credentials derived drill-age-identity
#: generate` and committed; the private half is in the ops repository's
#: `drill` Environment and nowhere on disk. Absent until that generator has
#: run: the appliance then encrypts to the escrowed generations alone.
DRILL_RECIPIENT = 'drill-recipient.txt'
DRILL_RECIPIENT_FILE = DEPLOY_DIR / DRILL_RECIPIENT

#: The bundle's file names, shared by the writer and the environment that
#: names them.
CA_FILE = 'ca.crt'
CERT_FILE = 'client.crt'
KEY_FILE = 'client.key'
URL_FILE = 'backend-url'
#: The tool's own host-key pin, beside the bundle for the same box. Its own
#: file rather than the operator's `~/.ssh/known_hosts`: nothing but
#: `state-backend ssh` reads it, and nothing else may write it.
KNOWN_HOSTS_FILE = 'known_hosts'

#: The libpq variables that carry a bundle's three files. Standard names, read
#: by libpq itself and by the driver Pulumi's Postgres backend uses, which is
#: what lets the connection string stay free of paths.
CA_ENV = 'PGSSLROOTCERT'
CERT_ENV = 'PGSSLCERT'
KEY_ENV = 'PGSSLKEY'

#: How much life the server certificate must have left for a converge to leave
#: a box alone, and the only home of that number: the documents name the
#: margin, never its value.
#:
#: It is small against `pki.LEAF_VALIDITY`, so a certificate spends a small
#: fraction of its life inside the margin and the box is replaced for expiry
#: at most once per certificate. And it is wider than `EXPIRY_ALERT_MARGIN`
#: below, so an installation being deployed at all has had the expiry
#: reported to it -- reporting is what a converge does unaided, and the
#: replacement itself waits for `--force` -- well before the probe's alert
#: can fire.
RENEWAL_MARGIN = dt.timedelta(days=90)

#: How much life the server certificate must have left for `state-backend
#: probe` to stay quiet (physical/state-backend.md §6), and the only home of
#: that number. Held below `RENEWAL_MARGIN` by a test: the probe is the
#: backstop for a box nobody has converged, or whose reports nobody acted on,
#: and a backstop that fires before the margin opens is the first reporter
#: rather than the last. The gap between the two is the time a reported
#: expiry has to be acted on before it is alerted as well.
EXPIRY_ALERT_MARGIN = dt.timedelta(days=30)


def drill_recipient(path: Path) -> str | None:
    """The drill key's public half as `path` holds it, or None while no such file exists.

    Absent is a state rather than a refusal: the file appears when the
    generator first runs, and every converge before that would otherwise
    refuse. A file that is there is held to what the generator writes -- one
    recipient, because the drill key has one slot and no generational pair
    (physical/state-backend.md §5) -- and an empty one is refused the way
    `operator-keys.txt` is, since a blank where a recipient should be is a
    dump the drill cannot open.
    """
    if not path.is_file():
        log.info('no drill recipient on file at %s; dumps encrypt to the escrowed generations alone', path)
        return None
    try:
        found = lib_config.lines(path, 'the drill age recipient')
    except ValueError as exc:
        raise age.AgeError(str(exc)) from exc
    if len(found) != 1:
        raise age.AgeError(
            f'{path} holds {len(found)} recipients, and the drill key has one slot: '
            '`credentials derived drill-age-identity generate --rotate` is what replaces it'
        )
    if not found[0].startswith(age.PUBLIC_PREFIX):
        raise age.AgeError(f'{path} holds {found[0]!r}, which is not an age recipient')
    return found[0]


def age_recipients(vault: escrow.Vault) -> tuple[str, ...]:
    """The public halves of every identity the appliance encrypts dumps to.

    One function behind two callers, which is the point: the box's recipient
    list is rendered from this, and so is the encryption of a dump an
    operator takes by hand (`state.py`). A dump written to a different set
    than the box's would be a file the drill and the escrow disagree about.

    The escrowed generations first, then the drill recipient where one is on
    file (`DRILL_RECIPIENT_FILE`). The drill key is not a root the escrow
    holds -- it opens nothing an escrowed generation does not also open -- so
    it is read from the committed file rather than recovered, and the field
    it lands in is digested (`Machine.age_recipients`): committing the file
    is drift the plain converge names.
    """
    generations = tuple(age.recipient(vault.recover(label)) for label in escrow.backup_labels())
    drill = drill_recipient(DRILL_RECIPIENT_FILE)
    return generations if drill is None else (*generations, drill)


def backup_identities(vault: escrow.Vault) -> list[str]:
    """The private halves, for opening a dump — the other direction of the same list.

    All of them at once, newest first, because which generation a given
    object was written under is not a fact the object carries (§5): any dump
    still in retention opens with one of these.
    """
    return [vault.recover(label) for label in escrow.backup_labels()]


@dataclass(frozen=True, eq=False)
class Roots:
    """What the appliance is built from that only the escrow can produce.

    Recovered once per run and passed down, so a converge that renders the
    machine twice opens the offline registry once. The age recipients are
    public halves: the identities themselves stay in the escrow -- or, for
    the drill key, in the ops repository's `drill` Environment -- and the box
    never holds one.
    """

    ca: pki.Authority
    age_recipients: tuple[str, ...]

    @staticmethod
    def labels() -> tuple[str, ...]:
        """The escrow labels this appliance is built out of.

        In the register's order: the authority, then the identities its dumps
        are encrypted to.
        """
        return (escrow.CA, *escrow.backup_labels())

    @classmethod
    def recover(cls, vault: escrow.Vault) -> Roots:
        return cls(ca=pki.Authority.from_pem(vault.recover(escrow.CA)), age_recipients=age_recipients(vault))

    @classmethod
    def ensure(cls, vault: escrow.Vault, *, appliance_exists: bool) -> Roots:
        """Recover the roots, minting first any the escrow does not hold yet.

        The appliance is the first thing to escrow (credentials.md §4.1): a
        bring-up has a kit and an empty registry, and the CA and the identity
        this run is about to encrypt dumps to are generated and committed by
        the same run that installs them. Idempotent by probing, like the rest
        of `provision` — a label already escrowed is left exactly as it is,
        because generating over it would orphan every dump under it.

        **Generation is a bring-up act, and `appliance_exists` is what says
        this run is not one.** It has no default: whether a box is running is
        a fact about the caller's situation that this function cannot see, and
        the direction a default would have to pick -- generate -- is the one
        that destroys a live appliance's recoverability. With a box already running, a label the
        registry cannot answer for means the registry is the wrong one —
        `--escrow` pointed at another directory, or a clone whose `escrow/`
        was never populated — rather than a label nobody has minted yet.
        Minting there would rebuild the box under a CA no client bundle
        chains to, and encrypt its dumps to a recipient no object still in
        retention was written to: both halves of the recovery story break at
        once, and neither failure shows until it is needed. So that case
        refuses, naming the label it could not recover.
        """
        for label in cls.labels():
            if vault.registry.generations(label):
                continue
            if appliance_exists:
                raise escrow.EscrowError(
                    f'nothing escrowed for {label}, and the appliance is already running: '
                    'generating one now would rebuild the box under roots nothing else holds. '
                    'Point --escrow at the registry this appliance was built from'
                )
            log.info('nothing escrowed for %s yet; generating it', label)
            _ = escrow.generate(vault.registry, label)
        return cls.recover(vault)


@dataclass(frozen=True)
class ClientBundle:
    """What an operator or CI needs to reach the backend.

    The certificates print and the private key does not. A certificate is
    public by construction — it is what the box is shown — while the key is
    what authenticates as this bundle's role for as long as the leaf is valid.
    """

    name: str
    address: str
    ca_cert: bytes
    cert: bytes
    key: bytes = field(repr=False)

    def url(self) -> str:
        """The connection string: everything about the backend, nothing about this machine.

        The three certificate files travel beside it as `PGSSLROOTCERT`,
        `PGSSLCERT` and `PGSSLKEY` (`ssl_env`) rather than inside it. libpq
        expands no variable inside a connection string, and neither does the
        driver Pulumi's Postgres backend uses — but both read those variables,
        so the channel that carries a path is the environment. A string with
        the paths in it would be a string that only one checkout on one
        machine can use, and moving that checkout would silently invalidate
        the copy recorded beside the bundle.

        `verify-full` against a literal IP: the state backend's hot path must
        not depend on DNS, which is itself something this backend deploys.
        """
        return f'postgres://{self.name}@{self.address}:{settings.PORT}/{settings.DATABASE}?sslmode=verify-full'


def ssl_env(directory: Path) -> dict[str, str]:
    """The libpq variables naming the bundle in `directory`.

    The other half of `ClientBundle.url`, and the half that varies by machine:
    a client bundle is reachable from anywhere its files are, so where they
    are is said once, in the environment, by whoever knows — `mise.toml` for a
    workstation slot, a workflow step for the `ci` bundle it materializes, and
    this function for the commands that drive `pg_dump`, `pg_restore` and
    `pulumi` themselves.

    Absolute, because the tools are run with a working directory of their own.
    """
    directory = directory.resolve()
    return {
        CA_ENV: str(directory / CA_FILE),
        CERT_ENV: str(directory / CERT_FILE),
        KEY_ENV: str(directory / KEY_FILE),
    }


def operator_keys() -> tuple[str, ...]:
    """The public keys that may log in to the appliance.

    Refused by name when the file is missing or holds nothing: an empty list
    renders a Butane document that `butane --strict` accepts and that boots an
    appliance nobody can reach, which is a failure discovered an hour later
    after an image import and a launch.
    """
    return lib_config.lines(DEPLOY_DIR / OPERATOR_KEYS, 'the appliance operator keys')


class Digested(enum.Enum):
    """How one field of the machine enters the digest map the box carries.

    The converge compares a running box to this commit component by component
    (`digests`), and some fields cannot be compared as their value: the
    certificates are re-issued on every render, and the secrets must not be
    digested at all. `NEVER` is therefore the enum's name for "this field is a
    secret", which is why it decides the repr as well as the digest.
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

    A `NEVER` field is kept out of the repr by the same declaration, so a
    secret added to this record later is covered by saying the one thing its
    author has to say anyway rather than by remembering a second annotation.
    """
    return field(metadata={'digest': how}, repr=how is not Digested.NEVER)


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
    #: private one in cloud metadata would buy nothing towards that.
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

    def parameters(self) -> dict[str, Any]:
        """The names the Butane template's expressions use.

        The fields, plus the one value the template needs that is *derived*
        from a field rather than stored beside it: the host key's public half,
        which the box carries as `ssh_host_key_pub` so that its fingerprint
        reaches the console banner. Derived rather than carried, because a
        second field could hold the public half of a different key than the
        private one beside it -- and because it is minted per render, so a
        field would have to be excluded from the digest map by hand
        (`digests`).
        """
        values = {spec.name: getattr(self, spec.name) for spec in fields(self)}
        values['ssh_host_key_pub'] = host_public_key(self)
        return values


def machine(
    roots: Roots,
    *,
    address: str,
    dump_key_id: str,
    dump_key: str,
    bucket_id: str,
    now: dt.datetime | None = None,
) -> Machine:
    """The machine this commit describes, at this address, with this dump key.

    `now` is the instant the server certificate is issued at, and it goes to
    `pki` untouched: the default is `pki`'s, in one place, so a test can pin
    the certificate's validity the way it pins `renewal_due`'s reading of it.
    """
    # One issuance, both halves. A leaf key is random at issuance (pki.py), so
    # asking the CA twice would hand the box a certificate its key does not
    # match -- and a box whose TLS key is wrong answers nothing.
    server = roots.ca.issue_server(address, now=now)
    # The box's SSH identity is minted here rather than generated on the box,
    # which is what lets the launch record a pin for it before it boots. It is
    # this render's alone: every caller that needs the public half derives it
    # from this value (`host_public_key`), so a pin can never describe a key
    # the box was not given.
    host_key = Ed25519PrivateKey.generate()
    return Machine(
        operator_keys=operator_keys(),
        postgres_uid=settings.POSTGRES_UID,
        postgres_image=settings.POSTGRES_IMAGE,
        database=settings.DATABASE,
        ci_role=settings.CI_ROLE,
        operator_role=settings.OPERATOR_ROLE,
        ca_cert=roots.ca.certificate().cert_pem.decode().strip(),
        server_cert=server.cert_pem.decode().strip(),
        server_key=server.key_pem.decode().strip(),
        ssh_host_key=host_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
        .strip(),
        age_recipients=roots.age_recipients,
        age_url=settings.AGE_URL,
        age_sha256=settings.AGE_SHA256,
        b2_dump_key_id=dump_key_id,
        b2_dump_key=dump_key,
        b2_bucket_id=bucket_id,
        b2_prefix=settings.B2_PREFIX,
        dump_script=(DEPLOY_DIR / DUMP_SCRIPT).read_text().strip(),
        dump_schedule=settings.DUMP_SCHEDULE,
        reboot_day=settings.REBOOT_DAY,
        reboot_time=settings.REBOOT_TIME,
        reboot_window_minutes=settings.REBOOT_WINDOW_MINUTES,
    )


def render_ignition(values: Machine) -> str:
    """Butane in, validated Ignition out.

    Takes the machine rather than building one, because every fact recorded
    beside the box has to come from the same render as the Ignition it boots
    with: when the server certificate inside that Ignition expires
    (`expires_at`), and the public half of the SSH host key it delivers
    (`host_public_key`). A second `machine` call would issue a second
    certificate and mint a second host key, so the box would record an expiry
    belonging to a certificate it never held and be pinned to a key it was
    never given.

    Rendered through an environment of its own rather than through
    `kluster.lib.templates`: that mechanism resolves a template relative to
    the package that owns it, and this one lives in `deploy/`, which is
    deployment material a reader is meant to be able to open on its own. The
    settings are the repository's (`StrictUndefined`, trailing newline kept),
    so a forgotten parameter is still an error at render time.
    """
    environment = Environment(
        loader=FileSystemLoader(DEPLOY_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    butane = environment.get_template(TEMPLATE).render(values.parameters())
    log.info('handing %s to butane for validation and conversion to Ignition', TEMPLATE)
    proc = sp.run(
        ['butane', '--strict', '--pretty'],
        input=butane,
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
    never given. A render is the only place both exist at once, which is why
    this takes the machine rather than building one -- a second `machine`
    call would mint a second key, and pin the box to the one it did not get.

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


def renewal_due(recorded: str, *, now: dt.datetime | None = None) -> str | None:
    """Why a recorded expiry makes the box stale, or None while it does not.

    The one time-dependent part of the comparison, and deliberately a
    threshold rather than a digest. A digest of "days remaining" would differ
    from the box's on every run after the day it launched, and a converge that
    always finds drift is a converge that always rebuilds. A threshold flaps in
    neither direction: outside `RENEWAL_MARGIN` this answers None and a second
    run is the same no-op as the first, and inside it the replacement carries a
    certificate with a full `pki.LEAF_VALIDITY` ahead of it, so the run after
    the rebuild is a no-op again.

    A box recording no expiry is stale for the reason a box with no digest map
    is: silence is not evidence that it matches, and an unreadable expiry is
    exactly the state this component exists to refuse. That costs one
    replacement, once, for a box built before this was recorded.
    """
    if not recorded:
        return 'the box does not record when its server certificate expires, so an expiry cannot be seen coming'
    try:
        expiry = dt.datetime.fromisoformat(recorded)
    except ValueError:
        return f'the box records {recorded!r} as its server certificate expiry, and that is not a date'
    # An expiry written by an older render could be naive; UTC is what every
    # writer of this field means, and a naive value compared against an aware
    # `now` raises rather than answering.
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=dt.timezone.utc)
    remaining = expiry - (now or dt.datetime.now(dt.timezone.utc))
    if remaining > RENEWAL_MARGIN:
        return None
    if remaining.days < 0:
        return f'the server certificate expired on {expiry.date().isoformat()}'
    return (
        f'the server certificate expires on {expiry.date().isoformat()}, '
        f'{remaining.days} day(s) from now and inside the {RENEWAL_MARGIN.days}-day renewal margin'
    )


def _identity(pem: str, *, with_key: bool) -> str:
    """A certificate reduced to what it *is*.

    Validity dates, serial numbers and signature bytes move on every issuance;
    the subject and the SANs do not. Digesting the latter is what lets a
    re-render be recognised as the same machine.
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


def digests(roots: Roots, *, address: str, dump_key_id: str, bucket_id: str) -> dict[str, str]:
    """A digest per component of the machine, for comparing a box to the repo.

    Per component rather than one number so that a drifted box can say *what*
    drifted -- `butane`, `operator_keys`, `postgres_image` -- which is the
    difference between "re-provision, trust me" and a converge whose reason is
    readable. The map is small enough to travel in the instance's metadata,
    which is where the answer for a running box comes from.

    The template itself is a component: it is the machine's definition, and a
    change to it must be visible without every value it interpolates changing.
    """
    values = machine(roots, address=address, dump_key_id=dump_key_id, dump_key='', bucket_id=bucket_id)
    parts = {'butane': (DEPLOY_DIR / TEMPLATE).read_text()}
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


def drift(intended: Mapping[str, str], actual: Mapping[str, str]) -> list[str]:
    """The component names that differ. An empty list means the box matches.

    A component the box does not carry counts as drift, so a box provisioned
    before a component existed -- or before this bookkeeping did -- converges
    rather than passing on a silence.
    """
    return sorted(key for key in set(intended) | set(actual) if intended.get(key) != actual.get(key))


def client_bundle(authority: pki.Authority, *, name: str, address: str) -> ClientBundle:
    """The `ci` or `operator` credential, and the string for reaching the box with it.

    Every call issues a fresh key: a client certificate is re-issuable from
    the CA and escrowed nowhere, so writing a bundle is minting one rather
    than reproducing one. The box needs no notice — it authenticates the CA,
    not a particular leaf.
    """
    credential = authority.issue_client(name)
    return ClientBundle(
        name=name,
        address=address,
        ca_cert=authority.certificate().cert_pem,
        cert=credential.cert_pem,
        key=credential.key_pem,
    )


def write_client_bundle(bundle: ClientBundle, directory: Path) -> None:
    """Place a bundle on disk with the permissions libpq insists on.

    libpq refuses a client key that anyone but its owner can read, so that one
    is `0600`; the directory is `0700` for the same reason one level up, which
    is what `workstation.secret_dir` gives every slot.

    The URL lands here too, because a bundle is only usable with the string
    that names the backend it authenticates against — and because
    `mise.toml` reads this file to put `PULUMI_BACKEND_URL` in the
    environment. It says nothing about this directory: what does is the three
    `PGSSL*` variables the same file derives from where the bundle was found.
    """
    _ = workstation.secret_dir(directory)
    _ = (directory / CA_FILE).write_bytes(bundle.ca_cert)
    _ = (directory / CERT_FILE).write_bytes(bundle.cert)
    _ = workstation.write(directory / KEY_FILE, bundle.key.decode())
    _ = (directory / URL_FILE).write_text(bundle.url() + '\n')
    log.info('wrote client bundle to %s', directory)


def write_known_hosts(directory: Path, *, address: str, public_key: str) -> Path:
    """Place the box's host-key pin where `state-backend ssh` reads it.

    One line -- the address the client dials, then the key it must answer
    with -- in the slot that already holds the client bundle for this box.
    The file is the tool's own: `ssh` is pointed at it and at nothing else,
    so the operator's `~/.ssh/known_hosts` is neither read nor written, and
    an entry some earlier bare login left there decides nothing.

    Rewritten whole rather than appended to. The address is reserved and the
    box is cattle, so exactly one identity is ever correct for it, and a
    second line would be a second answer this file accepts.
    """
    _ = workstation.secret_dir(directory)
    path = directory / KNOWN_HOSTS_FILE
    _ = path.write_text(f'{address} {public_key}\n')
    return path
