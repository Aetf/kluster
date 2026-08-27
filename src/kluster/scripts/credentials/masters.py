"""The account roots: what they are made of, and where a script finds them.

`docs/credentials.md` §2 puts the account roots deliberately outside the seed
kit — they are a precondition of the system rather than a credential it
manages, and they have no designed rotate-on-compromise path. Three of them
are nonetheless *used* from the workstation: minting the OCI seed needs a
credential with more reach than any seed has, re-seeding B2 after a total loss
needs the account master key, and the `github` stack is applied with an
account-scoped token that can edit the protections guarding `main`
(framework/github.md §1). Cloudflare is not among them — the platform refuses
to let any token mint a token that carries token permissions, so its seed is
made in the dashboard and there is nothing left for a root to do.

Handing those over is what this module is. **One acquisition chain serves every
root**, in this order, first hit wins:

1.  the **desktop secret store**, where `credentials root <root> remember`
    puts it;
2.  the root's **token file**, a workstation slot (`workstation.py`) — the
    layer a non-interactive reader can use, which is how `mise.toml`
    materializes `GITHUB_TOKEN` for a `pulumi` run;
3.  the root's **environment variable**, which is how CI and a one-off shell
    hand a value in without writing it anywhere;
4.  a **console prompt**, which names the credential and how it is created.

Three properties decide the shape:

-   **The estate is never opened.** An earlier design pointed the scripts at
    the personal KeePassXC database and read one entry out of it, which means
    typing the master password of a database holding everything the operator
    owns so that `bootstrap` can read one row. Instead each root's fields live
    in the desktop secret store under their own keys, put there once by
    `credentials root <member> remember`, and a run reads exactly the fields
    it needs.
-   **A machine without a secret store still works.** Headless and CI runs
    fall through the file and the variable to a prompt, which names the
    credential and where it is created — a headless run is exactly the case
    where the operator cannot go and look it up.
-   **Per field, not per root.** Every layer is consulted for each field on
    its own, so a half-remembered root asks for the half that is missing and
    nothing else.

The secret-store layer is the same door as `credentials kit password remember`, in
other direction, and it goes through the same `kdbx` plumbing so there is one
secret-store mechanism rather than two.

The register below is machine-readable for the same reason `entries.py` is: a
root with no fields recorded here is a root the scripts cannot ask for, and a
field with no file and variable names is a layer of the chain that does not
exist for it.
"""

from __future__ import annotations

import getpass
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import kdbx, workstation
from .kdbx import KdbxError

log = logging.getLogger(__name__)

#: Reading a non-secret answer from the operator. Injected so tests need no
#: terminal; secrets go through `getpass` instead and never echo.
Prompt = Callable[[str], str]

#: Secret-store keys are namespaced so an account root can never collide with
#: a remembered database password, whose key is a filesystem path.
ACCOUNT_PREFIX = 'account-root'

#: The chain's layers, as they are reported by `stored` and printed by
#: `credentials root ls`. Names rather than an enum because their only job
#: is to be read by an operator.
STORE = 'the secret store'
FILE = 'a token file'
ENVIRONMENT = 'the environment'


class CredentialRejected(RuntimeError):
    """A provider refused a credential this code was handed.

    Distinct from a transport error: the call reached the API and the API said
    no, which is nearly always a wrong field rather than a wrong network.
    """


@dataclass(frozen=True)
class Field:
    """One part of an account root, and the four places it can come from.

    A root is rarely one string: an OCI API key is a tenancy, a user and a
    PEM. Each part is looked up on its own — its own secret-store key, its own
    file, its own variable — so a partially remembered root prompts for the
    missing part alone.
    """

    #: Key within the root; also the last component of the secret-store key.
    name: str
    #: What to ask for, in the operator's words.
    describes: str
    #: The file layer: a name under `.credentials/roots/` (`workstation.py`).
    file: str
    #: The environment layer: the variable a shell or a CI job hands it in.
    env: str
    #: `secret` never echoes; `file` is read from a path, because a PEM is not
    #: something anyone pastes into a prompt.
    kind: str = 'secret'
    #: Whether a tool reads the file layer without asking anybody — today,
    #: `mise.toml` building a `pulumi` run's environment. `remember` keeps
    #: these in the file rather than the secret store, because a template can
    #: open neither a keyring nor a prompt.
    materialized: bool = False

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

    #: The `credentials root <member>` name; matches the seed it mints, so
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
                Field(
                    'tenancy',
                    'the tenancy OCID',
                    kind='identifier',
                    file='oci.tenancy',
                    env='KLUSTER_OCI_TENANCY',
                ),
                Field(
                    'user',
                    'the OCID of the user the key belongs to',
                    kind='identifier',
                    file='oci.user',
                    env='KLUSTER_OCI_USER',
                ),
                Field(
                    'private-key',
                    'the API private key (PEM)',
                    kind='file',
                    file='oci.private-key',
                    env='KLUSTER_OCI_PRIVATE_KEY',
                ),
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
                Field(
                    'account-id',
                    'the account id (the master key id)',
                    kind='identifier',
                    file='b2.account-id',
                    env='KLUSTER_B2_ACCOUNT_ID',
                ),
                Field('key', 'the master application key', file='b2.key', env='KLUSTER_B2_KEY'),
            ),
        ),
        Root(
            member='github',
            title='GitHub admin token',
            console=(
                'github.com → Settings → Developer settings → Personal access\n'
                '  tokens → Tokens (classic) → Generate new token, scope `repo`.\n'
                "  It administers this account's repositories — branch protection,\n"
                '  rulesets, Environments and their gates — which is why the\n'
                '  `github` stack is applied from the workstation and never by CI\n'
                '  (framework/github.md §1), and why nothing mints it: it is an\n'
                '  account root from the personal estate, pushed to no slot.'
            ),
            fields=(
                # The one field a tool reads on its own: `mise.toml` turns the
                # file into `GITHUB_TOKEN` for `pulumi up -s github`, and `gh`
                # picks the same variable up inside this directory. The
                # variable name is the provider's rather than this repo's for
                # exactly that reason.
                Field(
                    'token',
                    'the admin personal access token',
                    file='github.token',
                    env='GITHUB_TOKEN',
                    materialized=True,
                ),
            ),
        ),
    )
}


def _account(root: Root, field: Field) -> str:
    return f'{ACCOUNT_PREFIX}/{root.member}/{field.name}'


def _clean(field: Field, raw: str) -> str | None:
    """A layer's raw text as a value, or None when there is nothing in it.

    Surrounding whitespace is a copy-paste artefact everywhere except a PEM,
    where the text *is* the value and the line structure is part of it. An
    empty layer is treated as absent rather than as an empty credential: a
    file someone truncated should cost a prompt, not a provider refusal.
    """
    value = raw if field.kind == 'file' else raw.strip()
    return value if value.strip() else None


def _find(root: Root, field: Field) -> tuple[str, str] | None:
    """One field's value and the layer it came from, or None if no layer has it.

    The order is the chain (module docstring): store, file, variable. The
    prompt is not here because it is not a lookup — `load` asks, `stored`
    reports, and only one of them may talk to the operator.
    """
    remembered = kdbx.remembered(_account(root, field))
    if remembered is not None and (value := _clean(field, remembered)) is not None:
        return value, STORE
    path = workstation.root_path(field.file)
    if path.is_file() and (value := _clean(field, path.read_text())) is not None:
        return value, FILE
    handed = os.environ.get(field.env)
    if handed is not None and (value := _clean(field, handed)) is not None:
        return value, ENVIRONMENT
    return None


def stored(root: Root) -> dict[str, str | None]:
    """Which layer holds each of the root's fields, and None where none does.

    Answers "will this run ask me anything" without disclosing a value, and
    reports every field as absent on a machine with no store, no files and no
    variables at all.
    """
    return {field.name: found[1] if (found := _find(root, field)) is not None else None for field in root.fields}


def load(root: Root, prompt: Prompt) -> Credential:
    """The root's values: from the first layer that has each, else asked.

    The fallback prints the console steps first. A run that reaches here on a
    headless machine is one where the operator cannot open the app and look,
    so the prompt has to carry what the app would have shown.
    """
    values: dict[str, str] = {}
    announced = False
    for field in root.fields:
        found = _find(root, field)
        if found is not None:
            values[field.name] = found[0]
            continue
        if not announced:
            log.warning('%s is not on this machine; it is created like this:', root.title)
            for line in root.console.splitlines():
                log.warning('  %s', line)
            log.warning('`credentials root %s remember` keeps it, so this is asked once.', root.member)
            announced = True
        values[field.name] = field.ask(prompt, root.title)
    return Credential(root=root, values=values)


def _keep(root: Root, field: Field, value: str) -> None:
    """Put one field where its readers can reach it — one layer, not two.

    A field a *tool* reads on its own goes to its file: a mise template can
    open neither a keyring nor a prompt, and a second copy in the secret store
    would be exposure bought for nothing. Everything else goes to the secret
    store, which is the layer a script can use and a backup cannot copy —
    falling back to the file on a machine that has no store at all, so
    `remember` means something on a headless box too.
    """
    if field.materialized:
        _ = workstation.write(workstation.root_path(field.file), value)
        return
    try:
        kdbx.store(_account(root, field), value)
    except Exception as exc:  # noqa: BLE001 - any backend failure is "no store here"
        log.warning('no desktop secret store (%s); keeping %s in its token file instead', exc, field.name)
        _ = workstation.write(workstation.root_path(field.file), value)


def remember(root: Root, prompt: Prompt) -> list[str]:
    """Ask for every field and keep it. Returns their names.

    Explicit, like every other write: a value is where it is because someone
    asked for it to be, never as a side effect of a run that happened to read
    it.
    """
    log.info('%s is created like this:', root.title)
    for line in root.console.splitlines():
        log.info('  %s', line)
    names: list[str] = []
    for field in root.fields:
        _keep(root, field, field.ask(prompt, root.title))
        names.append(field.name)
    return names


def forget(root: Root) -> None:
    """Remove every field of one root, from the secret store and from its files.

    Both writable layers, because `remember` may have used either: forgetting
    a root has to leave nothing behind on the machine. The environment layer is
    the caller's shell and not this command's to unset.
    """
    removed = 0
    for field in root.fields:
        try:
            kdbx.unstore(_account(root, field))
        except KdbxError:
            pass
        else:
            removed += 1
        path = workstation.root_path(field.file)
        if path.is_file():
            path.unlink()
            log.info('removed %s', path)
            removed += 1
    if not removed:
        raise KdbxError(f'{root.title} is not in the secret store, and has no token file')
