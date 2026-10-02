"""The workstation slots: the local half of a credential, inside the checkout.

`docs/credentials.md` §1 rule 6 names this storage channel. A **workstation
slot** is a repo-relative file under `.credentials/`, git-ignored, written by a
`credentials` command and read non-interactively afterwards — by `mise.toml`
when it builds a `pulumi` run's environment, or by a script that needs the
value without asking anybody for it.

The stack passphrase and the state-backend client bundle are deliberately not
in the desktop secret store: they are read on *every* `pulumi` run by a
template that cannot prompt, cannot unlock a keyring and cannot fail
gracefully, so a file is the shape that fits. `physical`'s passphrase is a
file for the same reason, though no template reads it: a `pulumi` run by hand
against `physical` names the file to `pulumi` (`PULUMI_CONFIG_PASSPHRASE_FILE`,
credentials.md §4.4), which a store entry could not be. The operator passphrase is read
by the `operator-stack` driver instead, through the acquisition chain
(`kluster.lib.acquisition`), so its slot is the chain's file layer, below the
store in the chain's order: where it lives on a machine whose store does not
hold it.

The names below are this package's; the directory they sit in and the modes
they are written with are `kluster.lib.workstation`, which the `physical`
stack's libvirt transport shares. A slot is durable and put there by a
`credentials` command; a working file the stack program rewrites on every run
is the other kind of thing in that directory (rfc-002 §8.4), and neither is
allowed to assume the other's lifetime.
"""

from __future__ import annotations

from pathlib import Path

from kluster.lib.stack_environment import OPERATOR_PASSPHRASE_SLOT, PHYSICAL_PASSPHRASE_SLOT
from kluster.lib.workstation import DIRECTORY, WorkstationError, directory, repo_root, secret_dir, write

__all__ = (
    'BUNDLE',
    'DIRECTORY',
    'KIT',
    'OPERATOR_PASSPHRASE',
    'PASSPHRASE',
    'PHYSICAL_PASSPHRASE',
    'ROOTS',
    'WorkstationError',
    'bundle_dir',
    'directory',
    'kit_path',
    'operator_passphrase_path',
    'passphrase_path',
    'physical_passphrase_path',
    'repo_root',
    'root_path',
    'secret_dir',
    'write',
)

#: The seed kit's default location (§2.1). `$KLUSTER_KDBX` overrides it, which
#: is how a kit kept on removable media or shared between checkouts is used.
KIT = 'kit.kdbx'

#: The Pulumi stack passphrase, recovered from the escrow (§2.2) and cached
#: here so a local `pulumi preview` needs neither the kit nor an eval.
PASSPHRASE = 'pulumi.passphrase'

#: The operator passphrase (§2.2), which encrypts the operator stacks and
#: nothing else. A second file rather than a second value in the first, because
#: the property it exists for is that it reaches no CI Environment: a value
#: nobody can push is easier to keep unpushed than a field of a value everybody
#: gets. Named where the driver that reads it names it.
OPERATOR_PASSPHRASE = OPERATOR_PASSPHRASE_SLOT

#: `physical`'s passphrase (§2.2), which encrypts that stack and nothing else.
#: A file of its own for the reason the operator passphrase has one: the
#: property it exists for is the set of Environments it reaches, which is
#: smaller than the stack passphrase's. Named where `stack_environment` names it.
PHYSICAL_PASSPHRASE = PHYSICAL_PASSPHRASE_SLOT

#: The account roots' file layer (`masters.py`), one file per field.
ROOTS = 'roots'

#: The state backend's `operator` client bundle: CA, certificate, key, URL.
BUNDLE = 'state-backend'


def kit_path() -> Path:
    return directory() / KIT


def passphrase_path() -> Path:
    return directory() / PASSPHRASE


def operator_passphrase_path() -> Path:
    return directory() / OPERATOR_PASSPHRASE


def physical_passphrase_path() -> Path:
    return directory() / PHYSICAL_PASSPHRASE


def root_path(name: str) -> Path:
    """The file layer of one account-root field (`masters.Field.file`)."""
    return directory() / ROOTS / name


def bundle_dir() -> Path:
    return directory() / BUNDLE
