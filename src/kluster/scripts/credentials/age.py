"""The `age` command line tool, as this repository uses it.

Two things are age-encrypted here: every secret in the escrow
(`escrow.py`), and the state-backend's nightly pg_dump on the box itself
(physical/state-backend.md §5). This module is the first of those, and the
recipients for the second.

**The tool, not a binding.** Encryption and key generation shell out to the
pinned `age` and `age-keygen` binaries (mise.toml). The wire format's value is
that its reference implementation can still read an old ciphertext years from
now on a machine that has none of this repository, so a second implementation
of it — a Python binding, or a hand-written encoder — buys nothing and can
disagree.

**The private half never lands on a filesystem.** Both `age --decrypt` and
`age-keygen -y` take `-` for their identity argument and read it from standard
input, so a recovery key exists only in this process and in the tool's memory.
Recipients are public and travel on argv; ciphertexts travel as files, because
standard input is spoken for.

A backup generation is a **label with a stored ciphertext**, not a derivation:
the identity behind `backup/age/<generation>` is random at creation, its age
ciphertext is committed under `escrow/`, and that ciphertext is the only copy.
Rotating is generating the next one and re-provisioning; losing its ciphertext
is losing every dump encrypted to it, which is the property `escrow check`
exists to defend.
"""

from __future__ import annotations

import subprocess as sp
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: The pinned binaries. Named rather than inlined so a failure can say which
#: tool was missing and where it is pinned.
BINARY = 'age'
KEYGEN = 'age-keygen'

#: Long enough for a cold start, short enough that a hung tool fails the run
#: rather than the operator's afternoon. Every call here is bytes-in,
#: bytes-out with no network.
TIMEOUT = 30

SECRET_PREFIX = 'AGE-SECRET-KEY-1'
PUBLIC_PREFIX = 'age1'

#: ASCII armour, because a ciphertext in the escrow is a file git carries.
ARMOR_BEGIN = '-----BEGIN AGE ENCRYPTED FILE-----'
ARMOR_END = '-----END AGE ENCRYPTED FILE-----'


class AgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
    """One age key pair. The secret is uppercase, as age writes it."""

    secret: str
    public: str


def _run(argv: list[str], *, stdin: str) -> str:
    try:
        proc = sp.run(argv, input=stdin, capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError as exc:
        raise AgeError(f'{argv[0]} is not on PATH; mise.toml pins it, so run under `mise x -- ...`') from exc
    if proc.returncode != 0:
        raise AgeError(f'{argv[0]} refused: {proc.stderr.strip() or f"exit {proc.returncode}"}')
    return proc.stdout


def generate() -> Identity:
    """A fresh identity from the tool that defines the format.

    The public half is computed back out of the secret rather than read off
    the comment line `age-keygen` prints beside it: the pair is then proven by
    the same call every later recipient lookup makes.
    """
    printed = _run([KEYGEN], stdin='')
    secret = next((line.strip() for line in printed.splitlines() if line.startswith(SECRET_PREFIX)), '')
    if not secret:
        raise AgeError(f'{KEYGEN} printed no identity')
    return Identity(secret=secret, public=recipient(secret))


def recipient(secret: str) -> str:
    """The public half of an identity.

    Computed by the tool rather than in-process: a mistake here would produce
    a recipient that looks like a key and encrypts to nobody, which is the one
    class of mistake nothing downstream can catch.
    """
    public = _run([KEYGEN, '-y', '-'], stdin=secret.strip() + '\n').strip()
    if not public.startswith(PUBLIC_PREFIX):
        raise AgeError(f'{KEYGEN} returned {public!r}, which is not a recipient')
    return public


def encrypt(plaintext: str, recipients: Sequence[str]) -> str:
    """Armoured ciphertext readable by every recipient given.

    Multi-recipient is the point rather than a nicety: it is what lets the
    escrow be re-wrapped for a successor custodian, and what lets a dump be
    readable by two generations at once.
    """
    if not recipients:
        raise AgeError('no recipient to encrypt to')
    argv = [BINARY, '--encrypt', '--armor']
    for value in recipients:
        argv += ['--recipient', value]
    return _run(argv, stdin=plaintext)


def decrypt(path: Path, identities: Sequence[str]) -> str:
    """The plaintext of a ciphertext *file*, opened by whichever identity fits.

    A path rather than bytes because standard input carries the identities,
    which is what keeps them off every filesystem. Several identities are
    accepted so a re-wrap can run without first knowing which key a given
    file is currently under.
    """
    if not identities:
        raise AgeError(f'no identity to open {path} with')
    stdin = ''.join(f'{value.strip()}\n' for value in identities)
    return _run([BINARY, '--decrypt', '--identity', '-', str(path)], stdin=stdin)


def is_armoured(text: str) -> bool:
    """Whether this looks like an age file at all — the check that needs no key."""
    stripped = text.strip()
    return stripped.startswith(ARMOR_BEGIN) and stripped.endswith(ARMOR_END)
