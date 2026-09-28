"""The acquisition chain: where a value the operator hands a run is found.

docs/credentials.md §2 writes the chain down, and it serves two kinds of value:
the account roots a mint borrows (`kluster.scripts.credentials.masters`), and
the operator passphrase every run of an operator stack is given
(`kluster.lib.stack_environment`). Each is looked up in this order, first hit
wins:

1.  the **desktop secret store** (the freedesktop Secret Service, or the
    platform's equivalent), one value per key;
2.  a **file** in the checkout's `.credentials/`, the layer a machine with no
    store falls back to;
3.  an **environment variable**, which is how a one-off shell hands a value in
    without writing it anywhere;
4.  a **prompt**, which is the caller's rather than this module's: it is not a
    lookup, and only the caller knows what to say while asking.

The store is reached here and nowhere else, so there is one secret-store
mechanism and not one per kind of value: the kit's master password
(`kluster.scripts.credentials.kdbx`) goes through the same three functions.
Nothing is written to it as a side effect of a run. Each write is a command
the operator ran by name -- `credentials kit password remember`, `credentials
root <name> remember`, and `credentials derived operator-passphrase generate`
and `recover` -- and a machine where none of them has run behaves as if it had
no store.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

#: Secret Service entries are keyed by (service, account); every value this
#: installation keeps there is under this service, and its account says which
#: value it is.
KEYRING_SERVICE = 'kluster-credentials'

#: The chain's layers, as a lookup reports where a value came from. Names
#: rather than an enum because their one job is to be read by an operator.
STORE = 'the secret store'
FILE = 'a token file'
ENVIRONMENT = 'the environment'

#: Reads the store layer: `remembered`, unless a caller reads it through a
#: name of its own.
Recall = Callable[[str], str | None]

#: Turns a layer's raw text into the value, or None where the layer holds
#: nothing. The default strips surrounding whitespace, a copy-paste artifact
#: for every value but a PEM, whose line structure is part of it.
Clean = Callable[[str], str | None]


def stripped(raw: str) -> str | None:
    """`raw` without its surrounding whitespace, or None where nothing is left."""
    value = raw.strip()
    return value or None


def remembered(account: str) -> str | None:
    """The value the desktop store holds under `account`, or None.

    A machine with no store is not an error -- it is the case every caller
    falls back from -- so a backend that is absent, locked or broken reads the
    same as a key that was never stored.
    """
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, account)
    except Exception as exc:  # noqa: BLE001 -- any backend failure is a miss
        log.debug('no secret store for %s: %s', account, exc)
        return None


def store(account: str, secret: str) -> None:
    """Put `secret` in the desktop store under `account`; raises where there is no store."""
    import keyring

    keyring.set_password(KEYRING_SERVICE, account, secret)
    log.info('stored %s in %s', account, keyring.get_keyring().name)


def unstore(account: str) -> bool:
    """Remove what the store holds under `account`; whether it held anything there to remove."""
    import keyring
    import keyring.errors

    try:
        keyring.delete_password(KEYRING_SERVICE, account)
    except keyring.errors.PasswordDeleteError:
        return False
    log.info('removed %s from the secret store', account)
    return True


def find(
    account: str, path: Path, variable: str, clean: Clean = stripped, recall: Recall | None = None
) -> tuple[str, str] | None:
    """A value and the layer it came from -- store, file, variable -- or None where no layer has it.

    A layer holding only whitespace counts as absent: a file someone
    truncated should cost the next layer, or a prompt, and not a refusal from
    whatever the empty value is handed to.
    """
    held = (recall or remembered)(account)
    if held is not None and (value := clean(held)) is not None:
        return value, STORE
    if path.is_file() and (value := clean(path.read_text())) is not None:
        return value, FILE
    handed = os.environ.get(variable)
    if handed is not None and (value := clean(handed)) is not None:
        return value, ENVIRONMENT
    return None
