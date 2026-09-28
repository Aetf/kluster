"""Which backend and which passphrase a `pulumi` run against a stack is given.

`pulumi` reads its backend and its passphrase from the environment, one of
each per process, while this installation has more than one of each: every
stack CI deploys keeps its state in the appliance's backend under the stack
passphrase, and an **operator stack** (`conventions.identity.OPERATOR_STACKS`)
is encrypted under the operator passphrase, which no CI job holds, and keeps
its state where the census says — the same backend, or a checkpoint committed
to this repository (framework/pulumi.md §3.3). So the environment is a
function of the stack, and this module is that function, for the two kinds of
caller that start `pulumi` against a stack: the `operator-stack` driver, and
the `credentials` commands that write a stack's configuration, which need a
committed stack's backend as much as an `up` does.

**An operator stack's run takes these variables from here or not at all.**
`operator_variables` finds the passphrase through the acquisition chain and
reads the backend from the checkout's slots, and `process` builds the
environment a run starts with: the caller's own, less every variable that can
steer `pulumi` or its backend (`steers`), plus the stack's. The ambient
`PULUMI_CONFIG_PASSPHRASE` that `mise.toml` exports is the stack passphrase, a
`PULUMI_BACKEND_URL` a shell exported names some other backend, and a
`PULUMI_DIY_BACKEND_GZIP` would move a committed checkpoint to a file the
checks never read; none of them may reach an operator stack's run, and none
can once it is removed rather than overridden.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from kluster.conventions import identity
from kluster.lib import acquisition, bundle, pulumi_cli
from kluster.lib.workstation import DIRECTORY

BACKEND_URL_ENV = 'PULUMI_BACKEND_URL'
PASSPHRASE_ENV = 'PULUMI_CONFIG_PASSPHRASE'
#: Turns off the `backups/` copy a `file://` backend writes beside every update
#: (framework/pulumi.md §3.3); set for a committed stack.
DISABLE_BACKUPS_ENV = 'PULUMI_DIY_BACKEND_DISABLE_CHECKPOINT_BACKUPS'

#: The prefixes of the variables that can steer `pulumi` or its backend.
#: `PULUMI_` is the CLI's own namespace, where every release can add a switch
#: that moves or rewrites the state — the checkpoint's compression, retained
#: copies, the backend layout, another backend's token. `PG` is libpq's, which
#: the estate's Postgres backend reads for its host, user, password and TLS.
#: `AWS_`, `AZURE_STORAGE_` and `GOOGLE_APPLICATION_CREDENTIALS` are the
#: credentials of the cloud-bucket backends a `PULUMI_BACKEND_URL` can name.
#: An operator stack's run is started with none of these from its caller's
#: environment but `ALLOWED`, and the ones this module sets itself.
STEERING = ('PULUMI_', 'PG', 'AWS_', 'AZURE_STORAGE_', 'GOOGLE_APPLICATION_CREDENTIALS')

#: The caller's variables under `STEERING` a run keeps, each for a reason:
#: `PULUMI_HOME` is where the CLI keeps its plugins and its own credentials,
#: which a workstation and a test each put where they choose, and which holds
#: nothing of the stack's; `PULUMI_SKIP_UPDATE_CHECK` only quiets the CLI's
#: check for a newer release.
ALLOWED = frozenset({'PULUMI_HOME', 'PULUMI_SKIP_UPDATE_CHECK'})


def steers(name: str) -> bool:
    """Whether the caller's `name` is kept out of an operator stack's run."""
    return name.startswith(STEERING) and name not in ALLOWED


#: The **operator passphrase**, which every operator stack is encrypted under
#: and no CI job holds (credentials.md §2.2), is found through the acquisition
#: chain (`acquisition`), under these three names: its key in the desktop
#: secret store, its workstation slot (credentials.md §4.4), which
#: `kluster.scripts.credentials.workstation` takes from here, and the variable
#: a one-off shell hands it in. The register row whose `generate` and
#: `recover` put it in the store or the slot is named last; a case in
#: `tests/test_operator_stack.py` holds it to the escrow's own spelling.
OPERATOR_PASSPHRASE_ACCOUNT = 'operator-passphrase'
OPERATOR_PASSPHRASE_SLOT = 'operator.passphrase'
OPERATOR_PASSPHRASE_ENV = 'KLUSTER_OPERATOR_PASSPHRASE'
OPERATOR_PASSPHRASE_ROW = 'operator-passphrase'
#: The slot holding the `operator` client bundle, named likewise in
#: `kluster.scripts.credentials.workstation`.
BUNDLE_SLOT = 'state-backend'

#: The directory a committed stack's `file://` backend is rooted at, in the
#: checkout.
CHECKPOINTS = 'checkpoints'


class EnvironmentRefused(pulumi_cli.PulumiRefused):
    """This checkout cannot say what a run of the stack needs.

    A kind of `PulumiRefused`, the refusal of a `pulumi` run that cannot
    start, so a caller that already turns that one into its own terms turns
    this one too.
    """


def home(stack: str) -> identity.StateHome | None:
    """Where `stack` keeps its state, if it is an operator stack; None for every other stack.

    Read from the census on every call rather than bound at import, so the
    census is the one place the answer comes from.
    """
    return identity.OPERATOR_STACKS.get(stack)


def committed_url(checkout: Path) -> str:
    """The backend of a stack whose state is committed: `checkpoints/` in `checkout`.

    `metadata=skip` keeps the storage library from writing an `.attrs` file
    beside every file it writes, so the checkpoint is the one file in its
    directory that changes.
    """
    return f'{(checkout / CHECKPOINTS).absolute().as_uri()}?metadata=skip'


def checkpoint(checkout: Path, stack: str) -> Path | None:
    """The committed checkpoint of `stack` in `checkout`, or None before its first `stack init`.

    Found by the stack's name under whichever project directory holds it,
    which is the project's name in `Pulumi.yaml`: the one project this
    repository has. Two matches would mean two projects, which is refused
    rather than guessed between.
    """
    found = sorted((checkout / CHECKPOINTS / '.pulumi' / 'stacks').glob(f'*/{stack}.json'))
    if len(found) > 1:
        raise EnvironmentRefused(f'more than one checkpoint of the {stack} stack: {", ".join(map(str, found))}')
    return found[0] if found else None


def backend_variables(stack: str, *, checkout: Path | None, estate_url: str | None) -> dict[str, str]:
    """The backend a run against `stack` is pointed at: the committed one, or the estate's.

    `estate_url` is the appliance's connection string where the caller has
    one; a stack whose state is committed ignores it, and needs `checkout`
    instead, since its backend is a directory there.
    """
    if home(stack) is identity.StateHome.COMMITTED:
        if checkout is None:
            raise EnvironmentRefused(f'the {stack} stack keeps its state in a checkout, and none was named')
        return {BACKEND_URL_ENV: committed_url(checkout), DISABLE_BACKUPS_ENV: 'true'}
    return {} if estate_url is None else {BACKEND_URL_ENV: estate_url}


#: How the chain's last layer asks: the question in, the answer out, or None
#: where there is nobody to ask.
Ask = Callable[[str], str | None]


def ask_on_a_terminal(question: str) -> str | None:
    """`getpass` where standard input is a terminal; None where it is not.

    A run with nobody at a terminal -- a pipe, a job, a test -- is refused by
    name instead of waiting on a prompt nobody can answer.
    """
    return getpass.getpass(question) if sys.stdin.isatty() else None


def operator_passphrase(checkout: Path, ask: Ask = ask_on_a_terminal) -> str:
    """The operator passphrase: the first the chain finds for `checkout`, else asked for.

    The chain is credentials.md §2's, the account roots' own: the desktop
    secret store, then the slot in `checkout`, then the variable. A slot that
    is there and cannot be read is refused naming it, rather than passed over:
    it holds what the operator put there. Nothing found and nobody to ask is
    refused by name, and so is an empty answer: an
    empty passphrase handed to `pulumi` is refused by the stack file's salt,
    or, in a file that has lost its salt, adopted as the new key
    (credentials.md §4.2).
    """
    slot = checkout / DIRECTORY / OPERATOR_PASSPHRASE_SLOT
    try:
        found = acquisition.find(OPERATOR_PASSPHRASE_ACCOUNT, slot, OPERATOR_PASSPHRASE_ENV)
    except OSError as exc:
        raise EnvironmentRefused(
            f'the operator passphrase slot {slot} cannot be read ({exc.strerror}): restore its mode '
            f'with `chmod 600 {slot}`, or remove it and let the rest of the chain answer'
        ) from exc
    if found is not None:
        return found[0]
    answer = ask('the operator passphrase, which no layer of the chain holds on this machine: ')
    if answer is not None and answer.strip():
        return answer.strip()
    raise EnvironmentRefused(
        f'no operator passphrase on this machine: not in the desktop secret store, not in {slot}, not in '
        f'{OPERATOR_PASSPHRASE_ENV}, and nobody answered the prompt. `credentials derived '
        f'{OPERATOR_PASSPHRASE_ROW} recover` puts it in the store, or in the slot where there is no store, '
        'on a machine that holds the kit'
    )


def operator_variables(stack: str, checkout: Path) -> dict[str, str]:
    """Every variable a run of operator stack `stack` is given, for `checkout`.

    The passphrase is the operator passphrase (`operator_passphrase`). The
    backend is the stack's home: the estate's, reached with the `operator`
    client bundle whose URL and three files are all read from its one slot, or
    the committed one.
    """
    where = home(stack)
    if where is None:
        raise EnvironmentRefused(
            f'{stack} is not an operator stack; the census holds {sorted(identity.OPERATOR_STACKS)}'
        )
    variables = {PASSPHRASE_ENV: operator_passphrase(checkout)}
    if where is identity.StateHome.COMMITTED:
        return variables | backend_variables(stack, checkout=checkout, estate_url=None)
    slot = checkout / DIRECTORY / BUNDLE_SLOT
    url = bundle.backend_url_file(slot)
    if url is None:
        raise EnvironmentRefused(
            f'no client bundle in {slot}, so there is no backend to reach the {stack} stack in: '
            '`state-backend bundle operator` writes it'
        )
    return variables | {BACKEND_URL_ENV: url.read_text().strip()} | bundle.ssl_env(slot)


def process(base: Mapping[str, str], variables: Mapping[str, str]) -> dict[str, str]:
    """The environment a run starts with: `base` less every variable that steers `pulumi`, plus `variables`."""
    return {name: value for name, value in base.items() if not steers(name)} | dict(variables)
