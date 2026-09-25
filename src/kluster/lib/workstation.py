"""The checkout-local secret directory, and how a file gets into it.

The mechanics only: find the checkout root, keep the directory `0700`, and put
a secret into it as a `0600` file. What each file *is* — a seed kit, a
passphrase, a client bundle — belongs to whichever package owns that file; the
credentials scripts name their slots in `kluster.scripts.credentials.workstation`
and the `physical` stack's libvirt transport names its working files beside the
component that writes them.

One directory holds all of it, for two reasons:

-   **A checkout carries everything local it needs.** No per-machine
    environment wiring, and no artifact of this system outside the tree it
    belongs to. Moving a workstation is `git clone` plus copying one
    directory.
-   **One thing to protect.** `.credentials/` is `0700`, and so is every
    level under it that a write passes through; `.gitignore` covers it in one
    line, and there is exactly one answer to "what on this machine is
    secret".

Which mode a file in it has depends on whether the file is a secret:

-   **A secret is `0600` from the moment it exists.** Everything `write` puts
    down is treated as one — a passphrase, a token file, a private key, and
    whatever else a caller hands it, public or not — and is created with that
    mode rather than narrowed to it afterwards, so no reader ever finds it
    wider.
-   **A public file may take the umask.** A CA or client certificate, a
    connection string that carries no credential, a pinned host key: the
    module that writes one may create it itself, and the `0700` directory
    above it is what keeps it the owner's.

The root is found by walking up to the `mise.toml` that defines the project,
which is the same directory mise calls `config_root` — so a path written here
and a path read by a mise template cannot drift apart. `repo_root` is the one
place that walk is written: every reader of the checkout in this package
derives from it rather than finding the root its own way.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

#: The one git-ignored directory. Named for what it holds rather than hidden
#: under a tool's name: it survives any of the tools that read from it.
DIRECTORY = '.credentials'


class WorkstationError(RuntimeError):
    """The checkout this code is running from cannot be located."""


def repo_root(module: Path = Path(__file__)) -> Path:
    """The checkout `module` sits in; by default, the one this package is running from.

    `mise.toml` is the marker because it is the file whose directory mise
    itself calls `config_root`: the templates in it and the code here resolve
    the same directory or the code refuses to guess. The nearest one wins, so a
    checkout nested inside another — a workspace under the primary's
    `.claude/workspaces/` — is its own root, not the one around it.
    """
    for candidate in module.resolve().parents:
        if (candidate / 'mise.toml').is_file():
            return candidate
    raise WorkstationError(f'no mise.toml above {module}: the workstation slots are relative to a checkout')


def directory() -> Path:
    """`.credentials/` in the checkout. Not created by looking at it."""
    return repo_root() / DIRECTORY


def secret_dir(path: Path) -> Path:
    """`path`, created `0700` where missing, and narrowed to its owner where it lies in `.credentials/`.

    A missing level is created `0700`, each one separately because
    `mkdir(parents=True)` applies the mode to the last one only, and a `0755`
    directory above a `0600` file still tells anyone with a shell that the file
    is there.

    A level that already exists is narrowed to its owner's bits when it is
    `.credentials/` or lies under it: that tree is this system's, and a copy of
    it brought over from another machine arrives with whatever mode the copy
    gave it. A level outside that tree — the directory a kit on removable media
    sits in, say — is the operator's and is left as it is. "Under" is decided
    on resolved paths, because `chmod` follows a symlink: a link inside the
    tree that points out of it, or a `..` that climbs out, names a directory
    the tree does not own.
    """
    if not path.is_dir():
        if path.parent != path and not path.parent.is_dir():
            _ = secret_dir(path.parent)
        path.mkdir(mode=0o700)
    try:
        own = directory().resolve()
    except WorkstationError:
        # Outside a checkout there is no `.credentials/` to hold to its mode.
        return path
    here = path.resolve()
    for level in (here, *here.parents):
        if not level.is_relative_to(own):
            break
        mode = stat.S_IMODE(level.stat().st_mode)
        if mode & 0o077:
            level.chmod(mode & 0o700)
            log.info('narrowed %s to %o', level, mode & 0o700)
    return path


def write(path: Path, value: str) -> Path:
    """Put a secret in a slot: `0600` from creation, newline-terminated, directory `0700`.

    The value goes into a sibling that `mkstemp` creates `0600`, which is then
    renamed over `path`. Creating the file with its mode is what leaves no
    moment at which the secret sits in a file the umask made wider, and the
    rename is what replaces an existing file — whatever mode it had — rather
    than writing into it; a symlink in the slot is replaced, not followed. The
    sibling is flushed to disk before the rename, so a run stopped part-way,
    or a machine that loses power, leaves the previous file or the whole new
    one, never half a secret.

    The trailing newline is what makes the file readable by everything that
    reads it — `read_file(...) | trim` in a mise template, `$(cat ...)` in a
    shell — and what keeps a hand-inspected file from looking truncated.
    """
    _ = secret_dir(path.parent)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.new')
    staged = Path(name)
    try:
        with os.fdopen(descriptor, 'w') as sink:
            _ = sink.write(value if value.endswith('\n') else value + '\n')
            sink.flush()
            os.fsync(sink.fileno())
        _ = staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)
    log.info('wrote %s', path)
    return path
