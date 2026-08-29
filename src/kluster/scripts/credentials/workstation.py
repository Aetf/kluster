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

The names below are this package's; the directory they sit in and the modes
they are written with are `kluster.lib.workstation`, which the `physical`
stack's libvirt transport shares. A slot is durable and put there by a
`credentials` command; a working file the stack program rewrites on every run
is the other kind of thing in that directory (rfc-002 §8.4), and neither is
allowed to assume the other's lifetime.
"""

from __future__ import annotations

from pathlib import Path

from kluster.lib.workstation import DIRECTORY, WorkstationError, directory, repo_root, secret_dir, write

__all__ = (
    'BUNDLE',
    'DIRECTORY',
    'KIT',
    'LEGACY_BUNDLE_DIR',
    'PASSPHRASE',
    'ROOTS',
    'WorkstationError',
    'bundle_dir',
    'directory',
    'kit_path',
    'passphrase_path',
    'repo_root',
    'root_path',
    'secret_dir',
    'write',
)

#: The seed kit's default location (§2.1). `$KLUSTER_KDBX` overrides it, which
#: is how a kit kept on removable media or shared between checkouts is used.
KIT = 'kit.kdbx'

#: The Pulumi state passphrase, recovered from the escrow (§2.2) and cached
#: here so a local `pulumi preview` needs neither the kit nor an eval.
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


def kit_path() -> Path:
    return directory() / KIT


def passphrase_path() -> Path:
    return directory() / PASSPHRASE


def root_path(name: str) -> Path:
    """The file layer of one account-root field (`masters.Field.file`)."""
    return directory() / ROOTS / name


def bundle_dir() -> Path:
    return directory() / BUNDLE
