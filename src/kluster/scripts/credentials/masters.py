"""The account roots: what they are made of, and where a script finds them.

`docs/credentials.md` §2 puts the account roots deliberately outside the seed
kit — they are a precondition of the system rather than a credential it
manages, and they have no designed rotate-on-compromise path. Two of them are
nonetheless *used* by scripts: minting the OCI seed needs a credential with
more reach than any seed has, and re-seeding B2 after a total loss needs the
account master key. Cloudflare is not among them — the platform refuses to
let any token mint a token that carries token permissions, so its seed is
made in the dashboard and there is nothing left for a root to do.

Handing those over is what this module is. Two properties decide its shape:

-   **The estate is never opened.** An earlier design pointed the scripts at
    the personal KeePassXC database and read one entry out of it, which means
    typing the master password of a database holding everything the operator
    owns so that `bootstrap` can read one row. Instead each root's fields live
    in the desktop secret store under their own keys, put there once by
    `credentials master <member> remember`, and a run reads exactly the fields
    it needs.
-   **A machine without a secret store still works.** Headless and CI runs
    fall through to a prompt, which names the credential and where it is
    created — a headless run is exactly the case where the operator cannot go
    and look it up.

This is the same door as `credentials kdbx remember`, in the other direction,
and it goes through the same `kdbx` plumbing so there is one secret-store
mechanism rather than two.

The register below is machine-readable for the same reason `entries.py` is: a
root with no fields recorded here is a root the scripts cannot ask for.
"""

from __future__ import annotations

import getpass
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import kdbx
from .kdbx import KdbxError

log = logging.getLogger(__name__)

#: Reading a non-secret answer from the operator. Injected so tests need no
#: terminal; secrets go through `getpass` instead and never echo.
Prompt = Callable[[str], str]

#: Secret-store keys are namespaced so an account root can never collide with
#: a remembered database password, whose key is a filesystem path.
ACCOUNT_PREFIX = 'account-root'


class CredentialRejected(RuntimeError):
    """A provider refused a credential this code was handed.

    Distinct from a transport error: the call reached the API and the API said
    no, which is nearly always a wrong field rather than a wrong network.
    """


@dataclass(frozen=True)
class Field:
    """One part of an account root.

    A root is rarely one string: an OCI API key is a tenancy, a user and a
    PEM. Each part is stored under its own secret-store key, so a partially
    remembered root prompts for the missing part alone.
    """

    #: Key within the root; also the last component of the secret-store key.
    name: str
    #: What to ask for, in the operator's words.
    describes: str
    #: `secret` never echoes; `file` is read from a path, because a PEM is not
    #: something anyone pastes into a prompt.
    kind: str = 'secret'

    def ask(self, prompt: Prompt, title: str) -> str:
        match self.kind:
            case 'file':
                raw = prompt(f'{title} — path to {self.describes}: ').strip()
                if not raw:
                    raise KdbxError(f'{title}: {self.describes} is required')
                return Path(raw).expanduser().read_text()
            case 'secret':
                value = getpass.getpass(f'{title} — {self.describes}: ').strip()
            case _:
                value = prompt(f'{title} — {self.describes}: ').strip()
        if not value:
            raise KdbxError(f'{title}: {self.describes} is required')
        return value


@dataclass(frozen=True)
class Root:
    """One account root (credentials.md §2), and what it is made of."""

    #: The `credentials master <member>` name; matches the seed it mints, so
    #: `seed oci create` and `master oci remember` speak of the same account.
    member: str
    #: Human name, used in every prompt and log line.
    title: str
    #: How it is created, printed when a run has to ask for it. An account
    #: root is made in a console once and never minted, so these steps have
    #: nowhere else to live.
    console: str
    fields: tuple[Field, ...]

    def field(self, name: str) -> Field:
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        raise KdbxError(f'{self.title} has no field {name!r}')


@dataclass(frozen=True)
class Credential:
    """One account root's values, held for the length of one run."""

    root: Root
    values: dict[str, str]

    def __getitem__(self, name: str) -> str:
        value = self.values.get(name)
        if not value:
            raise KdbxError(f'{self.root.title}: no {name}')
        return value


ROOTS: dict[str, Root] = {
    root.member: root
    for root in (
        Root(
            member='oci',
            title='OCI account root API key',
            console=(
                'cloud.oracle.com → Identity → Users → your own user → API keys → Add.\n'
                '  The user must be in Administrators, or carry policies to manage\n'
                '  users, groups and policies in the tenancy: minting the seed means\n'
                '  creating a user, its group, its policy and its API key, and\n'
                '  reading the tenancy identity domain the seed retires keys through.\n'
                '  Download the private key; the console shows the tenancy and user\n'
                '  OCIDs in the configuration-file preview beside it.'
            ),
            fields=(
                Field('tenancy', 'the tenancy OCID', kind='identifier'),
                Field('user', 'the OCID of the user the key belongs to', kind='identifier'),
                Field('private-key', 'the API private key (PEM)', kind='file'),
            ),
        ),
        Root(
            member='b2',
            title='B2 account master key',
            console=(
                'backblaze.com → Account → Application Keys → the master\n'
                '  application key at the top of the page. Its key id is the\n'
                '  account id; the key itself is shown once, when it is generated.'
            ),
            fields=(
                Field('account-id', 'the account id (the master key id)', kind='identifier'),
                Field('key', 'the master application key'),
            ),
        ),
    )
}


def _account(root: Root, field: Field) -> str:
    return f'{ACCOUNT_PREFIX}/{root.member}/{field.name}'


def stored(root: Root) -> dict[str, bool]:
    """Which of the root's fields the secret store currently holds.

    Answers "will this run ask me anything" without disclosing a value, and
    reports every field as absent on a machine with no store at all.
    """
    return {field.name: kdbx.remembered(_account(root, field)) is not None for field in root.fields}


def load(root: Root, prompt: Prompt) -> Credential:
    """The root's values: from the secret store where it has them, else asked.

    The fallback prints the console steps first. A run that reaches here on a
    headless machine is one where the operator cannot open the app and look,
    so the prompt has to carry what the app would have shown.
    """
    values: dict[str, str] = {}
    announced = False
    for field in root.fields:
        remembered = kdbx.remembered(_account(root, field))
        if remembered is not None:
            values[field.name] = remembered
            continue
        if not announced:
            log.warning('%s is not in the secret store; it is created like this:', root.title)
            for line in root.console.splitlines():
                log.warning('  %s', line)
            log.warning('`credentials master %s remember` stores it, so this is asked once.', root.member)
            announced = True
        values[field.name] = field.ask(prompt, root.title)
    return Credential(root=root, values=values)


def remember(root: Root, prompt: Prompt) -> list[str]:
    """Ask for every field and put it in the secret store. Returns their names.

    Explicit, like every other write to the store: a value is there because
    someone asked for it to be, never as a side effect of a run that happened
    to read it.
    """
    log.info('%s is created like this:', root.title)
    for line in root.console.splitlines():
        log.info('  %s', line)
    names: list[str] = []
    for field in root.fields:
        kdbx.store(_account(root, field), field.ask(prompt, root.title))
        names.append(field.name)
    return names


def forget(root: Root) -> None:
    """Remove every field of one root from the secret store."""
    missing = 0
    for field in root.fields:
        try:
            kdbx.unstore(_account(root, field))
        except KdbxError:
            missing += 1
    if missing == len(root.fields):
        raise KdbxError(f'{root.title} is not in the secret store')
