"""The escrow: every secret no provider mints, with its ciphertext in git.

Some secrets are not minted by any provider — a state passphrase, a CA key, a
backup identity, a GitHub App's private key. The kit holds **one recovery
keypair** for them (credentials.md §2.2): the public recipient is committed in
`escrow/RECIPIENTS` and the private half exists only in the offline kit. Every
such secret goes to its consumer exactly as before, and leaves behind an age
ciphertext committed as `escrow/<label>/<generation>.age`.

**A row's origin says where its plaintext comes from**, and there are two.
Most are `Generated`: random at creation, drawn here. The rest are `Console`:
made in a provider console that publishes no API for making one, so the row
carries the steps that create it and `record` takes what they produce. The
difference is one field because everything after it is the same — the same
ciphertext, the same generations, the same recovery.

**Generation and escrow are one act.** `generate` encrypts and writes before it
returns the plaintext, so there is no path on which a caller holds a fresh
secret whose ciphertext does not exist. That is the whole safety property: a
secret nobody can recover is indistinguishable from a lost one the moment its
consumer is rebuilt. `record` keeps the same property from the other side: the
value is escrowed before anything is told it landed.

**Rotation splits in two, and that is the point.** Rotating one credential is a
new generation for its label, adopted by that one consumer. Rotating the kit is
a new recovery keypair plus `rewrap` — pure re-encryption of the same
plaintexts, with nothing in production touched. The second costs no downtime
precisely because it changes no plaintext.

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
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kluster import conventions
from kluster.lib import config

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

#: The `github` stack's own passphrase, which decrypts that stack's committed
#: configuration and nothing else. Filed under the stack it belongs to rather
#: than beside the estate's passphrase, because what makes it a separate row is
#: exactly that it is not the estate's: it reaches no CI Environment, which is
#: what confines the admin token in that stack's config to the workstation
#: (framework/github.md §1).
GITHUB_PASSPHRASE = 'github/passphrase'

CA = 'state-backend/ca'
ALERTMANAGER = 'alertmanager/read'
BACKUP = 'backup/age'
#: The private key each single-purpose GitHub App signs its own JWT with, from
#: which a workflow mints the short-lived installation token it works with
#: (credentials.md §3). One label per App, because they rotate apart.
DISPATCH_KEY = 'github/dispatch-key'
TRIGGER_KEY = 'github/trigger-key'

#: Where the recovery key lives in the kit — the register's decision, not this
#: module's (`entries.py`).
RECOVERY_ENTRY = entries.SEEDS['recovery'].entry

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
class Generated:
    """A secret this side draws at random, which is most of the registry.

    Its whole definition is a mint, because a value nobody has to be asked for
    needs nothing else: `generate` calls it, checks the result against the
    row's shape and escrows it in the same act.
    """

    #: How a fresh one is made. Every one of these is randomness plus a format.
    mint: Callable[[], str]


@dataclass(frozen=True)
class KitAttachment:
    """Where a kit that carries this row as a seed keeps its value.

    A row of §2's table is written into the kit as an entry with the key
    material attached (`entries.py`), and a kit holding one is the only copy
    of that value there is. `record --from-kit` reads it from there, so a
    credential that belongs in the registry gets there without a console visit
    that would rotate it for no reason.

    Transitional by construction: a kit is filled from §2's table and a
    rotation writes a new database from the same table, so once both envelopes
    hold a kit written without these rows, nothing answers here and this
    reader can go.
    """

    #: The entry path the retired seed row was written to.
    entry: str
    #: The attachment on it that holds the key material.
    filename: str


@dataclass(frozen=True)
class Console:
    """A secret made in a provider console, because no API of that platform makes one.

    Not a seed: it mints nothing, so the kit would gain a row it cannot rotate
    (`entries.py`). Not minted here either — what this side does is take the
    value the console produced and escrow it, which is also the whole of a
    rotation: create another one there, `record` it here, delete the
    superseded one on the same page.

    The steps live on the row for the reason §2's do: a runbook is a second
    place for them to be wrong.
    """

    #: What a human does in that console, printed at the moment it is asked for.
    steps: str
    #: Where a kit that predates the row's classification still holds it.
    kit: KitAttachment | None = None


#: Where a row's plaintext comes from. Two cases and no third: a value is drawn
#: here or it is made somewhere no API reaches, and everything downstream --
#: the ciphertext, the generations, the recovery -- is the same either way.
Origin = Generated | Console


@dataclass(frozen=True)
class WorkstationSlot:
    """Where a recovered secret is written on this machine, and who reads it there.

    Distinct from `slots.Slot`, which addresses a delivery channel: this one
    is a local file under `.credentials/` (`workstation.py`). Both halves are
    the row's own facts — the second is what `recover` says once the value has
    landed, and it is true of the credential rather than of the command.
    """

    path: Callable[[], Path]
    read_by: str


@dataclass(frozen=True)
class Label:
    """One row of the escrow register."""

    #: The label, which is also the directory the generations sit in.
    name: str
    #: What the secret is, in the register's words.
    what: str
    #: Where its plaintext comes from, which decides the verb below.
    origin: Origin
    #: What a value has to look like to be this label's secret.
    shape: Shape = TEXT
    #: Where `recover` puts the value when nobody asked for it on stdout, so
    #: the ordinary path writes a `0600` file instead of printing a secret.
    #: The config passphrases have one, being read by `mise.toml` templates
    #: that cannot open a kit; the rest reach their consumers through a
    #: provisioning run, a seal or `credentials derived sync`. A property of
    #: the row rather than a second table keyed by label, which could name a
    #: label the register does not.
    slot: WorkstationSlot | None = None

    @property
    def single(self) -> bool:
        """Whether the label holds one value for its lifetime.

        Most labels rotate by growing a generation, which every reader can
        name (`Vault.recover`). A label whose readers take the latest alone
        and whose consumers were built against the first cannot: a second
        generation there is a value the next build adopts while everything
        already produced under the first stays answerable only to the first.
        Such a label rotates as a *new label* instead, and both writers refuse
        a second generation under it (`_next_generation`).

        A fact of the label's namespace rather than a field beside it: the
        backup identities carry their generation in the name, so every label
        under `BACKUP` is one of these -- a retired generation included, which
        is the same reading `check` gives a retired one.
        """
        return self.name.startswith(f'{BACKUP}/')

    @property
    def verb(self) -> str:
        """The `credentials derived <row> <verb>` that puts a value in this row.

        Derived from the origin rather than declared beside it, so a row whose
        value stopped being drawn here cannot go on advertising a command that
        would draw one.
        """
        return 'generate' if isinstance(self.origin, Generated) else 'record'

    def validate(self, value: str) -> None:
        """Raise unless `value` could be this label's secret.

        The empty case is called out on its own because it is the one a
        pipeline produces by accident: a producer that crashes before writing
        anything still leaves `credentials derived <row> import` a value to
        escrow, and an escrowed empty string is indistinguishable from a lost
        secret at the moment its consumer is being rebuilt.
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

    **The backup generation is the label, so each label holds one identity
    for its lifetime** (credentials.md §2.2; `Label.single`). The
    appliance's recipient list and the identities a restore opens a dump with
    are both the latest escrow generation of each window label
    (`state_backend.config`), so a second generation under one label would
    have the next provision run encrypt every dump to a key that no dump
    already in retention was written to, while `derived check` -- which reads
    density, not count -- stays green. Rotating the backup key is bumping the
    pin below and generating the label that names the new generation.

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


#: How each App's key is created, and where the identifier that goes with it
#: comes from. The client id is never a row here: it is a public identifier
#: the App's own page shows for as long as the App exists, unlike the key,
#: which is disclosed once. Where it is recorded differs between the Apps, so
#: that sentence is each App's own (`_DISPATCH_CLIENT_ID`, `_TRIGGER_CLIENT_ID`).
_APP_KEY_CONSOLE = """github.com/settings/apps → the "kluster {name}" App → Private keys →
  Generate a private key. The PEM downloads once and is shown never again;
  GitHub publishes no API that creates one, so nothing here can mint it and
  nothing here can mint its successor. Rotation is this same visit: generate
  another, record it, and delete the superseded key on the same page.
{app}"""


def _named(names: Sequence[str]) -> str:
    """Names as a sentence lists them: `a`, `a and b`, `a, b and c`."""
    return ' and '.join(names) if len(names) < 3 else f'{", ".join(names[:-1])} and {names[-1]}'


def _installed_on(app: conventions.forge.App) -> str:
    """Every repository the census installs `app` on, as a sentence lists them.

    Read off the repositories' own rows (`Repository.apps`) rather than
    written here, so the console step that tells the operator where to
    install the App names what the workflows minting from it need.
    """
    return _named([repository.name for repository in conventions.forge.REPOSITORIES if app in repository.apps])


def _declared_on(variable: conventions.forge.Variable) -> str:
    """Every repository the forge declares `variable` on, as a sentence lists them."""
    return _named(
        [repository.name for repository in conventions.forge.REPOSITORIES if variable in repository.variables]
    )


#: The dispatch App's client id is a census fact: recorded in the clear and
#: declared by the forge as the repository variable the workflows minting
#: from it read (credentials.md §3), so no step here reads it off the page by hand.
_DISPATCH_CLIENT_ID = (
    'It is recorded in the clear on `conventions.forge.DISPATCH_APP` (its `client_id`), and the `github` stack declares it as '
    f'the {_declared_on(conventions.forge.DISPATCH_APP_CLIENT_ID)} repository variable '
    f'`{conventions.forge.DISPATCH_APP_CLIENT_ID.name}` the workflows minting from it read, so nothing about it is typed into '
    'a console. A newly created App has a new one: record it there and land it with `mise run github up`.'
)

#: The trigger App has no row in the census (`conventions.forge`) and no
#: variable declared for it, so its client id is read off the App's page.
_TRIGGER_CLIENT_ID = (
    'It is public and readable there for as long as the App exists, so it is read off the page when the '
    'delivery slot is filled rather than stored here.'
)


def _app_key_console(*, name: str, permission: str, installed: str, client_id: str) -> str:
    """One App's steps, from the shape both of them share.

    Two Apps differ in a name, a permission, their installations and where
    their client id is recorded, and in nothing else; writing the text twice
    would be two places for the part that is the same to drift.
    """
    app = textwrap.fill(
        f'Creating the App, where there is none: New GitHub App named "kluster {name}", Repository → '
        f'{permission}, no webhook, installed on {installed} and on nothing else. The JWT issuer that goes '
        f'with the key is the *client id* on that page, not the numeric app id. {client_id}',
        width=78,
        initial_indent='  ',
        subsequent_indent='  ',
        break_on_hyphens=False,
    )
    return _APP_KEY_CONSOLE.format(name=name, app=app)


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
        Label(
            PASSPHRASE,
            'the Pulumi state passphrase, for every stack',
            Generated(_token),
            # Read on every `pulumi` run by a `mise.toml` template that can
            # neither prompt nor open a kit (credentials.md §4.4).
            slot=WorkstationSlot(
                path=workstation.passphrase_path,
                read_by='mise.toml reads it from there on every pulumi run',
            ),
        ),
        Label(
            GITHUB_PASSPHRASE,
            "the `github` stack's own config passphrase, held by no CI job",
            Generated(_token),
            # Read by the `github` task in `mise.toml` (`mise run github
            # <pulumi args>`), and by the `credentials` commands that reach
            # that stack's config (credentials.md §4.4).
            slot=WorkstationSlot(
                path=workstation.github_passphrase_path,
                read_by='mise.toml exports it as KLUSTER_GITHUB_PASSPHRASE, for `mise run github <pulumi args>`',
            ),
        ),
        Label(CA, "the state-backend CA's private key", Generated(pki.generate_ca_key), shape=PRIVATE_KEY),
        Label(ALERTMANAGER, 'the bearer token the issue-sync poller presents', Generated(_token)),
        *(
            Label(
                name,
                'an age identity the state-backend encrypts its pg_dumps to',
                Generated(_identity),
                shape=IDENTITY,
            )
            for name in backup_labels()
        ),
        Label(
            DISPATCH_KEY,
            f"the dispatch App's private key, which signs for contents:write on "
            f'{_installed_on(conventions.forge.DISPATCH_APP)}',
            Console(
                _app_key_console(
                    name='dispatch',
                    permission='Contents: Read and write',
                    installed=_installed_on(conventions.forge.DISPATCH_APP),
                    client_id=_DISPATCH_CLIENT_ID,
                ),
                kit=KitAttachment('seeds/GitHub App (dispatch)', 'private-key.pem'),
            ),
            shape=PRIVATE_KEY,
        ),
        Label(
            TRIGGER_KEY,
            "the trigger App's private key, which signs for actions:write on the deployment repository",
            Console(
                _app_key_console(
                    name='trigger',
                    permission='Actions: Read and write',
                    installed='kluster',
                    client_id=_TRIGGER_CLIENT_ID,
                ),
                kit=KitAttachment('seeds/GitHub App (trigger)', 'private-key.pem'),
            ),
            shape=PRIVATE_KEY,
        ),
    ]
    return {row.name: row for row in rows}


def row_name(label: str) -> str:
    """The name a label's row carries on the command line.

    The registry files a label under a path — `escrow/pulumi/passphrase/1.age`
    — while every §3 row is addressed as one word, joined by `-`, in the
    command tree and in the slot map alike. This is the whole of the
    translation between the two, and it is a function of the label rather than
    a second table, so a row name cannot come to mean a label that is not the
    one it was derived from.
    """
    return label.replace('/', '-')


def rows() -> dict[str, Label]:
    """Every escrowed row of the register, keyed by the name the CLI gives it."""
    return {row_name(label): row for label, row in register().items()}


def fill_command(label: str) -> str:
    """The command that files this row's first generation, for a message that names it.

    A label the register does not name has no verb of its own; a retired backup
    generation is the one such label that legitimately exists, and `generate`
    is what filed it while it was current.
    """
    row = register().get(label)
    return f'credentials derived {row_name(label)} {row.verb if row is not None else "generate"}'


def _row(label: str) -> Label:
    rows = register()
    if label not in rows:
        raise EscrowError(f'no label {label!r} in the register; it holds {", ".join(sorted(rows))}')
    return rows[label]


def slot(label: str) -> WorkstationSlot | None:
    """The workstation slot the register gives this label, if it gives it one."""
    return _row(label).slot


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
        try:
            return list(config.lines(self.recipients_file, 'the escrow recipients'))
        except FileNotFoundError as exc:
            raise EscrowError(
                f'no {self.recipients_file}; `credentials kit bootstrap` writes it while creating the recovery key'
            ) from exc
        except ValueError as exc:
            raise EscrowError(f'{self.recipients_file} names no recipient') from exc

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

    def next_generation(self, label: str) -> int:
        """The number the label's next ciphertext is filed under.

        Generations are dense from `FIRST` and `check` refuses a gap, so both
        writers -- minting a fresh secret and adopting an existing one -- have
        to count the same way.
        """
        found = self.generations(label)
        return found[-1] + 1 if found else FIRST

    def latest(self, label: str) -> int:
        found = self.generations(label)
        if not found:
            raise EscrowError(f'nothing escrowed for {label!r}; `{fill_command(label)}` puts one there')
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


def _next_generation(registry: Registry, row: Label) -> int:
    """The number the row's next ciphertext is filed under, or a refusal for a row that has its one.

    In front of both writers -- a fresh mint and an adopted value -- because
    the invariant is the row's and not the verb's: a label that holds one
    value for its lifetime holds it however the value came about.
    """
    found = registry.generations(row.name)
    if found and row.single:
        raise EscrowError(
            f'{row.name} holds one value for its lifetime and already holds generation {found[-1]}: its '
            f'readers take the latest generation alone, so a second would be adopted by the next build '
            f'while everything produced under the first stays openable only with the first. Rotating '
            f'{row.what} is a new label, not a new generation (credentials.md §2.2)'
        )
    return registry.next_generation(row.name)


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

    Reached through `credentials kit bootstrap`, which creates this row like any
    other §2 row and so is also the repair path for a kit that predates the
    escrow: `--only recovery`.

    Refuses to replace either half. Every ciphertext under `escrow/` opens with
    this key and nothing else, so overwriting it is losing all of them at once.
    """
    if kit.has(entry):
        raise EscrowError(
            f'{entry!r} already holds a recovery key, and every ciphertext under {DIRECTORY}/ opens with it: '
            'replacing it is `credentials kit rotate`, and writing the recipients file afresh for the key '
            'already there is `credentials kit rewrap`'
        )
    if registry.recipients_file.exists():
        raise EscrowError(
            f'{registry.recipients_file} already exists; it belongs to a recovery key already in a kit, and '
            're-writing it to name that key is `credentials kit rewrap`'
        )
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
    per-credential rotation a decision rather than a side effect. A label
    that holds one value for its lifetime has no next generation to mint
    once it holds one, and is refused by name (`_next_generation`).
    """
    row = _row(label)
    if not isinstance(row.origin, Generated):
        raise EscrowError(
            f'{label} is made in a console, not here, so there is nothing to draw; '
            f'`{fill_command(label)}` prints the steps and escrows what they produce'
        )
    generation = _next_generation(registry, row)
    secret = row.origin.mint()
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
    that holds nothing for it, next is first, which is the migration case --
    and for a label that holds one value for its lifetime, the only case
    (`_next_generation`).

    The value is checked against the register's shape for the label before
    anything is written. An import is the only way a value the escrow did not
    mint gets in, so it is also the only place where "this is not the secret
    you think it is" can be caught before the ciphertext is committed and
    trusted.
    """
    row = _row(label)
    row.validate(secret)
    generation = _next_generation(registry, row)
    path = _store(registry, label, secret, generation=generation, recipients=registry.recipients())
    log.info('escrow: adopted the existing %s as generation %d in %s', label, generation, path)
    return path


def announce(label: str) -> None:
    """Print the steps that create a console-made value, before it is asked for.

    Always, rather than only when a prompt follows: the steps are the
    register's answer to "where does this come from", and a run that supplies
    the value from a file is exactly the run whose operator has not just read
    them (`devices.announce`, for the rows delivered into a stack).

    A row drawn here has no steps to print, and that is not an error: the tree
    offers `record` for no such row, so this is called with one only by a
    caller that has already gone wrong somewhere it can be seen.
    """
    row = _row(label)
    if not isinstance(row.origin, Console):
        return
    log.warning('%s is neither minted nor drawn here; it comes from here:', row.what)
    for line in row.origin.steps.splitlines():
        log.warning('  %s', line)


def from_kit(kit: KdbxStore, label: str) -> str:
    """This row's value out of a kit written while the row was still a seed.

    The kit row carries one half the escrow does not: the App's client id, in
    `UserName`. That half is a public identifier the App's own page shows for
    as long as the App exists, so the register stores it nowhere -- but it is
    printed here, because this is the last command with any reason to open the
    kit for this row.
    """
    row = _row(label)
    origin = row.origin
    if not isinstance(origin, Console) or origin.kit is None:
        raise EscrowError(f'{label} was never held in a kit, so there is nothing to read out of one')
    if not kit.has(origin.kit.entry):
        # A kit is filled from §2's table and a rotation writes a new database
        # from the same table, so a kit holding no such entry is one written
        # since the row left that table -- an ordinary kit rather than a
        # damaged one, and the answer is the console rather than a repair.
        raise EscrowError(
            f'{kit.path} holds no {origin.kit.entry!r}: a kit written from the seed table as it stands now '
            f'carries no such row, so this value comes from the console instead -- pipe it into '
            f'`credentials derived {row_name(label)} record` without --from-kit'
        )
    value = kit.attachment(origin.kit.entry, origin.kit.filename).decode().strip()
    identifier = kit.describe(origin.kit.entry).get('UserName', '')
    if identifier:
        log.info(
            '%s: the kit row names %s as the client id, which is public and stays on the App page', label, identifier
        )
    return value


def record(vault: Vault, label: str, secret: str) -> Path:
    """Escrow a console-made value, unless this exact one is already filed.

    The counterpart of `generate` for a row nothing here can draw (§2.2): a
    value the console produced becomes the row's next generation, and a second
    value produced there later becomes the one after it, which is the whole of
    a rotation.

    **Idempotent by probing the product, not by bookkeeping** (§4.1). Whether
    the work is done is answered by opening what the registry holds and
    comparing, so re-running the command that moved a key out of a kit files no
    second copy of it, while a genuinely new key from the console is filed as
    the next generation. A checkpoint would answer the same question with "this
    ran", which stops being true the moment a key is replaced in a console.

    Opening the registry is what makes that possible, so this is the one writer
    that needs the recovery key rather than the recipients file alone.
    """
    row = _row(label)
    if not isinstance(row.origin, Console):
        raise EscrowError(
            f'{label} is drawn here rather than made in a console; `{fill_command(label)}` mints a new '
            f'generation of it, and `credentials derived {row_name(label)} import` takes on one that exists'
        )
    # Before anything is decrypted: a value that cannot be this row's secret is
    # refused where its source is still known, rather than after a walk that
    # opened every generation to compare it against.
    row.validate(secret)
    wanted = secret.strip()
    held = next(
        (
            generation
            for generation in vault.registry.generations(label)
            if vault.recover(label, generation).strip() == wanted
        ),
        None,
    )
    if held is not None:
        log.info('escrow: %s is already generation %d; nothing to file', label, held)
        return vault.registry.path(label, held)
    return adopt(vault.registry, label, secret)


def rewrap(registry: Registry, *, identities: Sequence[str], recipients: Sequence[str] | None = None) -> list[Path]:
    """Re-encrypt every ciphertext to `recipients`, changing no plaintext.

    This is what a kit rotation costs: nothing in production is touched, no
    consumer is re-run, and the retired recovery key opens nothing afterwards.

    Several identities are passed rather than one so the run is resumable —
    a re-run finds some files already under the successor and some still
    under the predecessor, and opens both. `rotate_recovery` is that re-run:
    it hands over the identity it finds in the successor kit beside the
    retired one. The recipients file is written last, so it names what the
    directory actually holds.
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


def holds_recovery(kit: KdbxStore, *, entry: str = RECOVERY_ENTRY) -> bool:
    """Whether `entry` holds a recovery key: the one field `Vault.open` reads, non-empty."""
    return kit.has(entry) and bool(kit.get(entry))


def rotate_recovery(kit: KdbxStore, successor: KdbxStore, registry: Registry, *, entry: str = RECOVERY_ENTRY) -> None:
    """Put a successor recovery key in the new kit and re-wrap the escrow to it.

    The successor is written before the re-wrap, so an interrupted rotation
    leaves the key that the half-re-wrapped registry needs sitting in a kit
    rather than nowhere. The re-run of that interrupted rotation is this same
    call against the same successor: a key already in the successor's row is
    the one re-wrapped to, never replaced, because ciphertexts the first run
    reached are under it and nothing else opens them. The re-wrap is handed
    both identities either way, which is what makes every mid-way state
    converge (`rewrap`), and the retired key is read from `kit` -- the one
    thing a re-run still needs the retired kit's row for.

    The recipient is derived from the identity rather than read from the row,
    for the reason no OCI row stores a fingerprint: a stored copy could only
    ever disagree with the key it describes.

    A successor row holding the retired identity itself is refused: nothing
    would rotate, and the file is the kit in hand or a copy of it.
    """
    retired = kit.get(entry)
    if holds_recovery(successor, entry=entry):
        secret = successor.get(entry)
        if secret == retired:
            raise EscrowError(
                f'{entry!r} in the successor holds the retired key itself; a successor is a new file, '
                'not the kit being rotated or a copy of it'
            )
        log.info('escrow: the successor already holds a recovery key; re-wrapping to it')
    else:
        identity = age.generate()
        successor.put(entry, identity.public, identity.secret)
        secret = identity.secret
    _ = rewrap(registry, identities=[secret, retired], recipients=[age.recipient(secret)])


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

    The identity does not print: it is the recovery key, which opens every
    ciphertext in the registry the other half names.
    """

    registry: Registry
    identity: str = field(repr=False)

    @classmethod
    def open(cls, kit: KdbxStore, registry: Registry | None = None, *, entry: str = RECOVERY_ENTRY) -> Vault:
        return cls(registry=registry if registry is not None else Registry.open(), identity=kit.get(entry))

    def recover(self, label: str, generation: int | None = None) -> str:
        """One escrowed secret, latest generation unless another is named."""
        number = generation if generation is not None else self.registry.latest(label)
        return age.decrypt(self.registry.path(label, number), [self.identity])
