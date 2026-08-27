"""The escrow: every locally-generated secret, random, with its ciphertext in git.

Some secrets are not minted by any provider — a state passphrase, a CA key, a
backup identity. The kit holds **one recovery keypair** for them
(credentials.md §2.2): the public recipient is committed in `escrow/RECIPIENTS`
and the private half exists only in the offline kit. Every such secret is
random at creation, goes to its consumer exactly as before, and leaves behind
an age ciphertext committed as `escrow/<label>/<generation>.age`.

**Generation and escrow are one act.** `generate` encrypts and writes before it
returns the plaintext, so there is no path on which a caller holds a fresh
secret whose ciphertext does not exist. That is the whole safety property: a
secret nobody can recover is indistinguishable from a lost one the moment its
consumer is rebuilt.

**Rotation splits in two, and that is the point.** Rotating one credential is a
new generation for its label, adopted by that one consumer. Rotating the kit is
a new recovery keypair plus `rewrap` — pure re-encryption of the same
plaintexts, with nothing in production touched. Under the derivation model the
two were the same event, so a custody-hygiene rotation of the kit re-encrypted
every stack and re-provisioned the state backend.

**Labels are API.** A label names a secret across generations; renaming one
orphans the ciphertexts filed under the old name. Add labels, never edit them.

The registry is a directory of files rather than one file, for three reasons:
git shows a new generation as an added file rather than as a diff nobody can
read, `check` walks it with no key at all, and a recovery needs nothing but
`age`, this directory and the kit.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import age, entries, pki, workstation
from .kdbx import KdbxStore

log = logging.getLogger(__name__)

#: The committed registry, beside `mise.toml`. Deliberately not under
#: `.credentials/`: that directory is git-ignored and local, and these
#: ciphertexts are meant to be in every clone.
DIRECTORY = 'escrow'

#: The recipients every ciphertext is written to, one per line. Comments and
#: blank lines are allowed so a second custodian's key can say whose it is.
RECIPIENTS_FILE = 'RECIPIENTS'
SUFFIX = '.age'

#: Files the registry root may hold that are not ciphertexts.
ROOT_FILES = frozenset({RECIPIENTS_FILE, 'README.md'})

#: Generations count from one and are dense: `check` refuses a gap, because a
#: missing generation is either a deleted ciphertext or a miscount, and both
#: are worth stopping for.
FIRST = 1

PASSPHRASE = 'pulumi/passphrase'
CA = 'state-backend/ca'
ALERTMANAGER = 'alertmanager/read'
BACKUP = 'backup/age'

#: Where the recovery key lives in the kit — the register's decision, not this
#: module's (`entries.py`).
RECOVERY_ENTRY = entries.SEEDS['recovery'].entry

#: Where `recover` puts a value when nobody asked for it on stdout, so the
#: ordinary path writes a `0600` file instead of printing a secret. Only the
#: passphrase has a slot: it is read on every `pulumi` run by a `mise.toml`
#: template that can neither prompt nor open a kit (credentials.md §4.4).
SLOTS: dict[str, Callable[[], Path]] = {PASSPHRASE: workstation.passphrase_path}

#: 32 bytes, in the form every consumer of a password accepts.
TOKEN_BYTES = 32

#: A label is a path, and a path built from a string is a way to write outside
#: the directory it is supposed to stay in.
_LABEL = re.compile(r'[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)*\Z')


class EscrowError(RuntimeError):
    pass


def _token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).decode().rstrip('=')


def _identity() -> str:
    return age.generate().secret


@dataclass(frozen=True)
class Shape:
    """What a label's plaintext looks like, and how to say so in an error.

    A generated secret has its shape by construction; an imported one is
    whatever the operator's pipe produced. A pipe that produced the wrong
    thing is found out either at the import or on the day something has to be
    rebuilt from the ciphertext, and only one of those two days is cheap.

    Deliberately shallow — a prefix, a PEM header — because a check that
    really parsed each secret would need every consumer's tooling in here to
    reject values no plausible producer emits.
    """

    #: The shape in the register's words, as the tail of "this is not ...".
    looks_like: str
    #: Whether a value has it. Every shape rejects a blank value; `validate`
    #: reports that one case in words of its own, because it has a cause the
    #: others do not.
    matches: Callable[[str], bool]


def _is_identity(value: str) -> bool:
    return value.strip().upper().startswith(age.SECRET_PREFIX)


#: Whichever of PKCS8, EC or RSA the operator happens to be holding: adoption
#: escrows a key exactly as it already exists.
_PEM_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----')


def _is_private_key(value: str) -> bool:
    stripped = value.strip()
    # Both ends, so a pipe that died mid-key is a refusal rather than a
    # ciphertext holding half a key.
    return _PEM_PRIVATE_KEY.match(stripped) is not None and stripped.endswith('-----')


#: A password, a token: nothing to recognise beyond there being something.
TEXT = Shape('a value', lambda value: bool(value.strip()))
IDENTITY = Shape(f'an age identity, which starts {age.SECRET_PREFIX}', _is_identity)
PRIVATE_KEY = Shape('a PEM private key, which starts -----BEGIN and ends -----END ... -----', _is_private_key)


@dataclass(frozen=True)
class Label:
    """One row of the escrow register."""

    #: The label, which is also the directory the generations sit in.
    name: str
    #: What the secret is, in the register's words.
    what: str
    #: How a fresh one is made. Every one of these is randomness plus a format.
    mint: Callable[[], str]
    #: What a value has to look like to be this label's secret.
    shape: Shape = TEXT

    def validate(self, value: str) -> None:
        """Raise unless `value` could be this label's secret.

        The empty case is called out on its own because it is the one a
        pipeline produces by accident: a producer that crashes before writing
        anything still leaves `escrow import` a value to escrow, and an
        escrowed empty string is indistinguishable from a lost secret at the
        moment its consumer is being rebuilt.
        """
        if self.shape.matches(value):
            return
        if not value.strip():
            raise EscrowError(
                f'nothing to escrow as {self.name}: the value is empty, which is what a producer that '
                'wrote no output leaves in the pipe'
            )
        raise EscrowError(f'this is not {self.shape.looks_like}, which is what {self.name} holds ({self.what})')


def backup_labels() -> tuple[str, ...]:
    """The backup identities the appliance encrypts dumps to, newest first.

    The current generation and the one before it, so every object still in
    retention opens with one of the two (physical/state-backend.md §5). The
    window is **clamped at the first generation**: until a rotation has
    happened there is no predecessor, and naming one would have the register
    demand a ciphertext for a generation that never existed — and a bring-up
    mint a key nothing needs and encrypt every dump to it.

    Read from the appliance's own pin rather than repeated here: the Butane
    file names exactly these recipients, so bumping the generation is one
    edit in one place. `state_backend.settings` is constants over
    `kluster.conventions` and imports nothing from this package, so naming it
    here is not the cycle the rest of that package would be — hence the local
    import.
    """
    from kluster.scripts.state_backend import settings

    current = settings.AGE_GENERATION
    window = (current, current - 1)
    return tuple(f'{BACKUP}/{number}' for number in window if number >= FIRST)


def register() -> dict[str, Label]:
    """Every label the escrow is expected to hold.

    This is the machine-readable half of credentials.md §2.2, and the reason
    `check` can be run by someone who has never seen the system: what should
    be there is written down rather than inferred from what is.

    Only durable roots are here. Leaf certificates under the state-backend CA
    are re-issued from it at will (`pki.py`), so escrowing them would store a
    secret whose loss costs nothing.
    """
    rows = [
        Label(PASSPHRASE, 'the Pulumi state passphrase, for every stack', _token),
        Label(CA, "the state-backend CA's private key", pki.generate_ca_key, shape=PRIVATE_KEY),
        Label(ALERTMANAGER, 'the bearer token the issue-sync poller presents', _token),
        *(
            Label(name, 'an age identity the state-backend encrypts its pg_dumps to', _identity, shape=IDENTITY)
            for name in backup_labels()
        ),
    ]
    return {row.name: row for row in rows}


def _row(label: str) -> Label:
    rows = register()
    if label not in rows:
        raise EscrowError(f'no label {label!r} in the register; it holds {", ".join(sorted(rows))}')
    return rows[label]


@dataclass(frozen=True)
class Registry:
    """The `escrow/` directory: paths, generations and recipients, no keys.

    Everything here is readable without the kit, which is what lets `check`
    be a check rather than a ceremony.
    """

    root: Path

    @classmethod
    def open(cls, root: Path | None = None) -> Registry:
        return cls(root=root if root is not None else workstation.repo_root() / DIRECTORY)

    @property
    def recipients_file(self) -> Path:
        return self.root / RECIPIENTS_FILE

    def directory(self, label: str) -> Path:
        if not _LABEL.match(label):
            raise EscrowError(f'{label!r} is not a label: lower-case words joined by / and -')
        return self.root.joinpath(*label.split('/'))

    def path(self, label: str, generation: int) -> Path:
        return self.directory(label) / f'{generation}{SUFFIX}'

    def recipients(self) -> list[str]:
        if not self.recipients_file.is_file():
            raise EscrowError(f'no {self.recipients_file}; `credentials escrow init` writes it')
        lines = (line.strip() for line in self.recipients_file.read_text().splitlines())
        values = [line for line in lines if line and not line.startswith('#')]
        if not values:
            raise EscrowError(f'{self.recipients_file} names no recipient')
        return values

    def set_recipients(self, values: Sequence[str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _ = self.recipients_file.write_text(''.join(f'{value}\n' for value in values))
        log.info('escrow: %s now names %d recipient(s)', self.recipients_file, len(values))

    def generations(self, label: str) -> list[int]:
        """Which generations of `label` are on disk, in order."""
        directory = self.directory(label)
        if not directory.is_dir():
            return []
        found = [path.stem for path in directory.glob(f'*{SUFFIX}')]
        return sorted(int(stem) for stem in found if stem.isdigit())

    def latest(self, label: str) -> int:
        found = self.generations(label)
        if not found:
            raise EscrowError(f'nothing escrowed for {label!r}; `credentials escrow generate {label}` mints one')
        return found[-1]

    def labels(self) -> list[str]:
        """Every label with at least one ciphertext, whether the register knows it or not."""
        if not self.root.is_dir():
            return []
        directories: set[Path] = {path.parent for path in self.root.rglob(f'*{SUFFIX}')}
        return sorted('/'.join(directory.relative_to(self.root).parts) for directory in directories)

    def strays(self) -> list[Path]:
        """Files that are neither a ciphertext nor one of the registry's own."""
        if not self.root.is_dir():
            return []
        return sorted(
            path
            for path in self.root.rglob('*')
            if path.is_file() and path.name not in ROOT_FILES and not (path.suffix == SUFFIX and path.stem.isdigit())
        )


def _store(registry: Registry, label: str, secret: str, *, generation: int, recipients: Sequence[str]) -> Path:
    """Encrypt first, then place the file — the order the safety property needs.

    Written through a temporary sibling so an interrupted run leaves either the
    previous file or the whole new one, never a ciphertext that stops halfway.
    """
    armoured = age.encrypt(secret, recipients)
    path = registry.path(label, generation)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f'.{path.name}.new')
    _ = staged.write_text(armoured)
    os.replace(staged, path)
    return path


def init(kit: KdbxStore, registry: Registry, *, entry: str = RECOVERY_ENTRY) -> age.Identity:
    """Create the recovery keypair: private half into the kit, recipient into the repo.

    Refuses to replace either half. Every ciphertext under `escrow/` opens with
    this key and nothing else, so overwriting it is losing all of them at once;
    replacing a live recovery key is `credentials rotate`, which re-wraps the
    registry before the predecessor stops being needed.
    """
    if kit.has(entry):
        raise EscrowError(f'{entry!r} already holds a recovery key; replacing one is `credentials rotate`')
    if registry.recipients_file.exists():
        raise EscrowError(f'{registry.recipients_file} already exists; it belongs to a recovery key already in a kit')
    identity = age.generate()
    kit.put(entry, identity.public, identity.secret)
    registry.set_recipients([identity.public])
    return identity


def generate(registry: Registry, label: str) -> str:
    """Mint a fresh random secret as the label's next generation, and return it.

    The ciphertext is on disk before this returns, which is the safety
    property in one sentence: no caller ever holds a generated secret that
    the escrow does not carry.

    Nothing adopts the new generation on its own. The consumer named in the
    register has to be re-run against it, and until then the previous
    generation is what production holds — which is exactly what makes a
    per-credential rotation a decision rather than a side effect.
    """
    row = _row(label)
    existing = registry.generations(label)
    generation = existing[-1] + 1 if existing else FIRST
    secret = row.mint()
    # The row's own mint against the row's own shape: the register says what a
    # label holds in one place, and a mint that stopped agreeing with it fails
    # here rather than at the recovery.
    row.validate(secret)
    path = _store(registry, label, secret, generation=generation, recipients=registry.recipients())
    log.info('escrow: %s generation %d written to %s (%s)', label, generation, path, row.what)
    log.warning('nothing has adopted it yet: re-run what consumes %s, and commit %s', label, path)
    return secret


def adopt(registry: Registry, label: str, secret: str) -> Path:
    """Escrow a value that already exists as the label's next generation.

    The one way a secret enters the registry without being minted here, and it
    exists for adoption (credentials.md §4.2): a credential already in
    production — the state passphrase, the CA key, an age identity — is
    escrowed exactly as it stands, so taking on this model rotates nothing.

    The *next* generation rather than a fixed first one, so importing can
    never overwrite what is already filed under this label. On a registry
    that holds nothing for it, next is first, which is the migration case.

    The value is checked against the register's shape for the label before
    anything is written. An import is the only way a value the escrow did not
    mint gets in, so it is also the only place where "this is not the secret
    you think it is" can be caught before the ciphertext is committed and
    trusted.
    """
    row = _row(label)
    row.validate(secret)
    existing = registry.generations(label)
    generation = existing[-1] + 1 if existing else FIRST
    path = _store(registry, label, secret, generation=generation, recipients=registry.recipients())
    log.info('escrow: adopted the existing %s as generation %d in %s', label, generation, path)
    return path


def rewrap(registry: Registry, *, identities: Sequence[str], recipients: Sequence[str] | None = None) -> list[Path]:
    """Re-encrypt every ciphertext to `recipients`, changing no plaintext.

    This is what a kit rotation costs: nothing in production is touched, no
    consumer is re-run, and the retired recovery key opens nothing afterwards.

    Several identities are passed rather than one so the run is resumable —
    a re-run finds some files already under the successor and some still
    under the predecessor, and opens both. The recipients file is written
    last, so it names what the directory actually holds.
    """
    targets = list(recipients if recipients is not None else registry.recipients())
    if not targets:
        raise EscrowError('no recipient to re-wrap to')
    held = {age.recipient(secret) for secret in identities}
    if not held & set(targets):
        raise EscrowError('none of the identities in hand is among the recipients; this would lock the escrow')

    # Everything is opened before anything is written: a run that dies part
    # way through then leaves a registry the same identities can still open.
    plaintexts = {
        (label, generation): age.decrypt(registry.path(label, generation), identities)
        for label in registry.labels()
        for generation in registry.generations(label)
    }
    written = [
        _store(registry, label, secret, generation=generation, recipients=targets)
        for (label, generation), secret in sorted(plaintexts.items())
    ]
    registry.set_recipients(targets)
    log.info('escrow: re-wrapped %d ciphertext(s)', len(written))
    return written


def rotate_recovery(kit: KdbxStore, successor: KdbxStore, registry: Registry, *, entry: str = RECOVERY_ENTRY) -> None:
    """Put a successor recovery key in the new kit and re-wrap the escrow to it.

    The successor is written before the re-wrap, so an interrupted rotation
    leaves the key that the half-re-wrapped registry needs sitting in a kit
    rather than nowhere.
    """
    retired = kit.get(entry)
    identity = age.generate()
    successor.put(entry, identity.public, identity.secret)
    _ = rewrap(registry, identities=[identity.secret, retired], recipients=[identity.public])


def missing(registry: Registry) -> list[str]:
    """Register labels with nothing escrowed — what a bring-up still has to mint."""
    return [label for label in register() if not registry.generations(label)]


def check(registry: Registry) -> list[str]:
    """Everything wrong with the registry, said in full. No kit, no key.

    Deliberately answerable by someone holding only a clone: presence of every
    expected label, ciphertexts that are age files, generations that count
    from one without a gap. What it cannot check is whether they decrypt —
    that needs the kit, and the run that needs the kit is a recovery.
    """
    problems: list[str] = []
    try:
        for value in registry.recipients():
            if not value.startswith(age.PUBLIC_PREFIX):
                problems.append(f'{RECIPIENTS_FILE}: {value!r} is not an age recipient')
    except EscrowError as exc:
        problems.append(str(exc))

    expected = register()
    for label, row in expected.items():
        generations = registry.generations(label)
        if not generations:
            problems.append(f'{label}: nothing escrowed, and the register expects {row.what}')
            continue
        if generations != list(range(FIRST, FIRST + len(generations))):
            listed = ', '.join(str(number) for number in generations)
            problems.append(f'{label}: generations are {listed}, which is not {FIRST} upwards without a gap')
        for generation in generations:
            path = registry.path(label, generation)
            if not age.is_armoured(path.read_text()):
                problems.append(f'{label}: generation {generation} is not an armoured age file')

    for label in registry.labels():
        if label not in expected and not label.startswith(f'{BACKUP}/'):
            problems.append(f'{label}: escrowed, but the register does not name it')
    problems.extend(f'{path}: neither a ciphertext nor part of the registry' for path in registry.strays())
    return problems


@dataclass(frozen=True)
class Vault:
    """The registry plus the one key that opens it.

    The pairing is a type rather than two arguments because every read needs
    both, and a reader that has the directory but not the key has nothing.
    """

    registry: Registry
    identity: str

    @classmethod
    def open(cls, kit: KdbxStore, registry: Registry | None = None, *, entry: str = RECOVERY_ENTRY) -> Vault:
        return cls(registry=registry if registry is not None else Registry.open(), identity=kit.get(entry))

    def recover(self, label: str, generation: int | None = None) -> str:
        """One escrowed secret, latest generation unless another is named."""
        number = generation if generation is not None else self.registry.latest(label)
        return age.decrypt(self.registry.path(label, number), [self.identity])
