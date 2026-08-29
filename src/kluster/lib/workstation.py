"""The checkout-local secret directory, and how a file gets into it.

The mechanics only: find the checkout root, and write `0600` into `0700`. What
each file *is* — a seed kit, a passphrase, a client bundle — belongs to
whichever package owns that file; the credentials scripts name their slots in
`kluster.scripts.credentials.workstation` and the `physical` stack's libvirt
transport names its working files beside the component that writes them.

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

log = logging.getLogger(__name__)

#: The one git-ignored directory. Named for what it holds rather than hidden
#: under a tool's name: it survives any of the tools that read from it.
DIRECTORY = '.credentials'


class WorkstationError(RuntimeError):
    """The checkout this code is running from cannot be located."""


def repo_root() -> Path:
    """The checkout this package is running from.

    `mise.toml` is the marker because it is the file whose directory mise
    itself calls `config_root`: the templates in it and the code here resolve
    the same directory or the code refuses to guess.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / 'mise.toml').is_file():
            return candidate
    raise WorkstationError('no mise.toml above this package: the workstation slots are relative to a checkout')


def directory() -> Path:
    """`.credentials/` in the checkout. Not created by looking at it."""
    return repo_root() / DIRECTORY


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
