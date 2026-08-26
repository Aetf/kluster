"""The workstation slots: the local half of a credential, inside the checkout.

`docs/credentials.md` §1 rule 6 names this storage channel. A **workstation
slot** is a repo-relative file under `.credentials/`, git-ignored, written by a
`credentials` command and read non-interactively afterwards — by `mise.toml`
when it builds a `pulumi` run's environment, or by a script that needs the
value without asking anybody for it.

It is deliberately not the desktop secret store. Roots are interactive and
rare, so a store that asks a session to unlock is right for them; the
passphrase and the state-backend client bundle are read on *every* `pulumi`
run by a template that cannot prompt, cannot unlock a keyring and cannot fail
gracefully. A file is the shape that fits.

One directory holds all of it, for two reasons:

-   **A checkout carries everything local it needs.** No per-machine
    environment wiring, and no artefact of this system outside the tree it
    belongs to. Moving a workstation is `git clone` plus copying one
    directory.
-   **One thing to protect.** The directory is `0700` and its files are
    `0600`, `.gitignore` covers it in one line, and there is exactly one
    answer to "what on this machine is secret".

The root is found by walking up to the `mise.toml` that defines the project,
which is the same directory mise calls `config_root` — so a path written here
and a path read by a mise template cannot drift apart.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .kdbx import KdbxError

log = logging.getLogger(__name__)

#: The one git-ignored directory. Named for what it holds rather than hidden
#: under a tool's name: it survives any of the tools that read from it.
DIRECTORY = '.credentials'

#: The seed kit's default location (§2.1). `$KLUSTER_KDBX` overrides it, which
#: is how a kit kept on removable media or shared between checkouts is used.
KIT = 'kit.kdbx'

#: The Pulumi state passphrase, derived from the derivation seed (§2.2) and
#: cached here so a local `pulumi preview` needs neither the kit nor an eval.
PASSPHRASE = 'pulumi.passphrase'

#: The account roots' file layer (`masters.py`), one file per field.
ROOTS = 'roots'

#: The state backend's `operator` client bundle: CA, certificate, key, URL.
BUNDLE = 'state-backend'

#: Where the bundle lived before it became a workstation slot. Read once, with
#: a warning, so a workstation that predates the move keeps working.
#: TODO(kluster-ops#34): delete this and its readers once every workstation has
#: re-run `state-backend bundle operator`.
LEGACY_BUNDLE_DIR = Path.home() / '.config' / 'kluster' / BUNDLE


def repo_root() -> Path:
    """The checkout this package is running from.

    `mise.toml` is the marker because it is the file whose directory mise
    itself calls `config_root`: the templates in it and the code here resolve
    the same directory or the code refuses to guess.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / 'mise.toml').is_file():
            return candidate
    raise KdbxError('no mise.toml above this package: the workstation slots are relative to a checkout')


def directory() -> Path:
    """`.credentials/` in the checkout. Not created by looking at it."""
    return repo_root() / DIRECTORY


def kit_path() -> Path:
    return directory() / KIT


def passphrase_path() -> Path:
    return directory() / PASSPHRASE


def root_path(name: str) -> Path:
    """The file layer of one account-root field (`masters.Field.file`)."""
    return directory() / ROOTS / name


def bundle_dir() -> Path:
    return directory() / BUNDLE


def secret_dir(path: Path) -> Path:
    """`path`, created `0700` along with every level of it that is missing.

    Each level is created separately because `mkdir(parents=True)` applies the
    mode to the last one only, and a `0755` directory above a `0600` file
    still tells anyone with a shell that the file is there.
    """
    if not path.is_dir():
        if path.parent != path and not path.parent.is_dir():
            _ = secret_dir(path.parent)
        path.mkdir(mode=0o700)
    return path


def write(path: Path, value: str) -> Path:
    """Put a secret in a slot: `0600`, newline-terminated, directory `0700`.

    The trailing newline is what makes the file readable by everything that
    reads it — `read_file(...) | trim` in a mise template, `$(cat ...)` in a
    shell — and what keeps a hand-inspected file from looking truncated.
    """
    _ = secret_dir(path.parent)
    _ = path.write_text(value if value.endswith('\n') else value + '\n')
    path.chmod(0o600)
    log.info('wrote %s', path)
    return path
