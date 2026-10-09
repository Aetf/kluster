"""The files a `credentials derived` command commits beside the Butane template, as the stack reads them.

Three files in `machine/` are written by a command and committed rather than
hand-edited: the box's SSH host key, public half (`HOST_KEY`); the backup
generations' recipients (`BACKUP_RECIPIENTS`); and the drill identity's
recipient (`DRILL_RECIPIENT`). The command that writes each is a script's,
since writing one needs the escrow or a configuration only a script opens; the
stack program reads all three, and a script imports no component, so the
readers live here (style/pulumi.md, "Layering") and the writers import them.

**Each reader is the file's grammar and nothing more.** Whether a recipient is
one the pinned `age` parses takes the tool, which a script has and a program
need not, so every recipient reader takes that check as an argument and holds
the line to the form `age-keygen` draws (`native`) whatever check it is
handed. A refusal names the line by its number and never quotes it: the
likeliest wrong line is a private half pasted where the public one goes.

**Absent is a refusal here.** The stack renders the box from these files, and
a box rendered without its host key's pin or its backup recipients is one
nobody can reach or one whose dumps nobody can open, so the reader names the
commands that write the file instead of answering empty. The writers' own
reading of an absent file as "nothing on file yet" is theirs to make
(`kluster.scripts.credentials.derived`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kluster import conventions
from kluster.lib import config as lib_config

from . import render, settings

#: The directory the Butane template sits in, where these files are kept.
MACHINE_DIRECTORY = Path(render.__file__).with_name(render.MACHINE)
#: The box's SSH host key, public half: what `state-backend ssh` pins, what a
#: reader compares a fingerprint against without the passphrase, and what the
#: stack holds its configured host key to.
HOST_KEY = MACHINE_DIRECTORY / 'host-key.txt'
#: The backup generations' public halves, one line per generation the box
#: encrypts its dumps to: the escrow label, then the recipient.
BACKUP_RECIPIENTS = MACHINE_DIRECTORY / 'backup-recipients.txt'
#: The drill identity's public half, one line.
DRILL_RECIPIENT = MACHINE_DIRECTORY / 'drill-recipient.txt'

#: The escrow's label for a backup generation, as the recipients file names
#: it: `backup/age/<N>`. The escrow's own spelling is
#: `kluster.scripts.credentials.escrow.BACKUP`, which a test holds equal.
BACKUP_LABEL_PREFIX = 'backup/age'
#: The first backup generation; the window never names one before it.
FIRST_GENERATION = 1

#: What every native recipient's encoding starts with, and what every native
#: identity's does, upper-cased.
PUBLIC_PREFIX = 'age1'
SECRET_STEM = 'AGE-SECRET-KEY-'

#: The `credentials derived` rows that write the files, as a refusal names
#: them. The command tree's own spellings are in
#: `kluster.scripts.credentials.derived`, which a test holds equal.
HOST_KEY_ROW = f'{conventions.STATE_BACKEND}-host-key'
DRILL_RECIPIENT_ROW = f'{conventions.DRILL}-age-identity'

_BACKUP_LABEL = re.compile(rf'{re.escape(BACKUP_LABEL_PREFIX)}/[1-9][0-9]*\Z')

#: A recipient check: the value, and the name a refusal says in its place.
#: Raises when the value is not a recipient.
Check = Callable[[str, str], None]


class Refused(ValueError):
    """A committed file is absent, or holds something its writer does not write."""


def backup_row(label: str) -> str:
    """The `credentials derived` row that writes `label`'s line: `backup-age-<N>`."""
    return label.replace('/', '-')


def backup_window() -> tuple[str, ...]:
    """The backup generations the box encrypts its dumps to, newest first.

    The current generation (`settings.AGE_GENERATION`) and the one before it,
    clamped at the first, which is the escrow's own window
    (`kluster.scripts.credentials.escrow.backup_labels`); a test holds the two
    equal.
    """
    current = settings.AGE_GENERATION
    return tuple(f'{BACKUP_LABEL_PREFIX}/{number}' for number in (current, current - 1) if number >= FIRST_GENERATION)


def native(value: str, name: str) -> None:
    """Refuse `value` unless it has the form of a native X25519 recipient, what `age-keygen` draws.

    `age1` and no second `1`, since neither `age` nor bech32's data alphabet
    holds one: a plugin recipient needs its plugin on the box, which installs
    `age` alone, and a post-quantum one cannot be mixed with the classic
    recipients every dump is encrypted to beside it. A value holding an
    identity's stem anywhere is refused first, and said to be one.
    """
    if SECRET_STEM in value.upper():
        raise Refused(f'{name} holds an age identity, the private half, where a recipient goes')
    if not (value.startswith(PUBLIC_PREFIX) and value.count('1') == 1):
        raise Refused(f'{name} is not a native `{PUBLIC_PREFIX}…` age recipient')


def form_only(value: str, name: str) -> None:
    """The check a reader runs where no tool is asked: none beyond the form every reader holds a recipient to."""
    del value, name


def _require(path: Path, row: str) -> None:
    if not path.is_file():
        raise Refused(f'no {path}: `credentials derived {row} generate` writes it, and it is committed')


def public_host_key(private: str) -> str:
    """The `ssh-ed25519 AAAA…` line of an OpenSSH private key, refused unless it is ed25519."""
    key = serialization.load_ssh_private_key(private.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise Refused(f'the host key is a {type(key).__name__}, and ed25519 is what is pinned')
    return key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


def host_key(path: Path | None = None) -> str:
    """The committed public half: the one `ssh-ed25519` line `path` holds, `HOST_KEY` by default."""
    path = HOST_KEY if path is None else path
    _require(path, HOST_KEY_ROW)
    try:
        found = lib_config.lines(path, 'the host key')
    except ValueError as exc:
        raise Refused(str(exc)) from None
    if len(found) != 1 or not found[0].startswith('ssh-ed25519 '):
        raise Refused(f'{path} holds {len(found)} line(s), and its writer writes one `ssh-ed25519` key')
    return found[0]


def backup_recipients(path: Path | None = None, *, check: Check = form_only) -> dict[str, str]:
    """The recipients file, `BACKUP_RECIPIENTS` by default, as label → recipient.

    Each line is a backup label and the recipient of the identity it holds,
    `#` comments and blank lines aside. Every recipient passes `check`, and
    the form `native` holds it to besides. An absent file is refused naming
    the row of each generation in `backup_window()`, the current one first,
    since each writes its own line.
    """
    path = BACKUP_RECIPIENTS if path is None else path
    if not path.is_file():
        rows = ', '.join(f'`credentials derived {backup_row(label)} generate`' for label in backup_window())
        raise Refused(
            f'no {path}: it is committed, a line per generation the box encrypts to, '
            f"and each generation's row writes its line: {rows}"
        )
    found: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(lib_config.COMMENT):
            continue
        fields = stripped.split()
        if len(fields) != 2 or not _BACKUP_LABEL.match(fields[0]):
            raise Refused(
                f'line {number} of {path} is not a backup label under {BACKUP_LABEL_PREFIX}/ followed by its recipient'
            )
        label, value = fields
        if label in found:
            raise Refused(f'line {number} of {path} names {label} a second time')
        name = f'the recipient on line {number} of {path}'
        check(value, name)
        native(value, name)
        found[label] = value
    return found


def drill_recipient(path: Path | None = None, *, check: Check = form_only) -> str:
    """The drill identity's recipient: the one line `path` holds, `DRILL_RECIPIENT` by default."""
    path = DRILL_RECIPIENT if path is None else path
    _require(path, DRILL_RECIPIENT_ROW)
    try:
        found = lib_config.lines(path, 'the drill age recipient')
    except ValueError as exc:
        raise Refused(str(exc)) from None
    if len(found) != 1:
        raise Refused(
            f'{path} holds {len(found)} recipients, and the drill key has one slot: '
            f'`credentials derived {DRILL_RECIPIENT_ROW} generate --rotate` is what replaces it'
        )
    (value,) = found
    name = f'the line in {path}'
    check(value, name)
    native(value, name)
    return value


def age_recipients(
    *, backup: Path | None = None, drill: Path | None = None, check: Check = form_only
) -> tuple[str, ...]:
    """Every recipient the box encrypts its dumps to: the window's generations, newest first, then the drill's.

    The window's current generation must be on file; the one before it is
    taken where the file names it, since the escrow holds no predecessor
    until a rotation has happened. A line for a generation outside the
    window is refused, because the box would encrypt to a key the escrow's
    restore does not try.
    """
    backup = BACKUP_RECIPIENTS if backup is None else backup
    on_file = backup_recipients(backup, check=check)
    window = backup_window()
    current = window[0]
    if current not in on_file:
        raise Refused(
            f'{backup} names no recipient for {current}, the generation the box encrypts to: '
            f'`credentials derived {backup_row(current)} generate` writes it'
        )
    outside = sorted(set(on_file) - set(window))
    if outside:
        raise Refused(f'{backup} names {", ".join(outside)}, outside the window the box encrypts to')
    return (*(on_file[label] for label in window if label in on_file), drill_recipient(drill, check=check))
