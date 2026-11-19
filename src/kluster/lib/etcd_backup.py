"""The etcd restore-verify's committed recipient file, as the stack that encrypts the snapshots to it reads it.

`credentials derived etcd-drill-age-identity generate` draws the
restore-verify's age identity, seals the private half into `k8s-base` and
writes the public half to `conventions.backup.ETCD_DRILL_RECIPIENT`, which is
committed beside the ciphertext. `k8s-base` reads it and encrypts every etcd
snapshot to it beside the escrowed backup generations
(`components.backup.etcd`). A stack imports no script, so the reader lives
here (style/pulumi.md, "Layering").

**The grammar is the state backend's drill recipient's**
(`kluster.lib.state_backend.committed`): one native X25519 recipient, `#`
comments and blank lines aside, held to the form `age-keygen` draws whatever
check the caller hands it, and refused with `committed.Refused`, naming the
line by where it is and never quoting it, since the likeliest wrong line is
the private half pasted where the public one goes. The file is not the state
backend's, so it is not read there, but one form and one refusal cover every
committed recipient.

**Absent is a refusal.** A snapshot encrypted without the recipient is one
the restore-verify cannot open, so the reader names the command that writes
the file instead of answering empty.
"""

from __future__ import annotations

from pathlib import Path

from kluster import conventions
from kluster.conventions.backup import ETCD_DRILL_RECIPIENT
from kluster.lib import config as lib_config
from kluster.lib.state_backend import committed

__all__ = ('DRILL_RECIPIENT_ROW', 'drill_recipient')

#: The `credentials derived` row that writes the file, as a refusal names it.
#: The command tree's own spelling is
#: `kluster.scripts.credentials.derived.ETCD_DRILL_AGE_IDENTITY_ROW`, which a
#: test holds equal.
DRILL_RECIPIENT_ROW = f'{conventions.sealed.ETCD_DRILL_IDENTITY.name}-age-identity'


def drill_recipient(path: Path | None = None, *, check: committed.Check = committed.form_only) -> str:
    """The restore-verify's recipient: the one line `path` holds, `conventions.backup.ETCD_DRILL_RECIPIENT` by default."""
    path = ETCD_DRILL_RECIPIENT if path is None else path
    if not path.is_file():
        raise committed.Refused(
            f'no {path}: `credentials derived {DRILL_RECIPIENT_ROW} generate` writes it, and it is committed'
        )
    try:
        found = lib_config.lines(path, "the etcd restore-verify's age recipient")
    except ValueError as exc:
        raise committed.Refused(str(exc)) from None
    if len(found) != 1:
        raise committed.Refused(
            f'{path} holds {len(found)} recipients, and the restore-verify has one identity: '
            f'`credentials derived {DRILL_RECIPIENT_ROW} generate` is what replaces it'
        )
    (value,) = found
    name = f'the line in {path}'
    check(value, name)
    committed.native(value, name)
    return value
