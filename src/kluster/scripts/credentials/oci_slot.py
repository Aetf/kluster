"""The OCI key the state-backend appliance signs with (docs/credentials.md §3).

A per-stack OCI key normally reaches its consumer as a Pulumi config secret,
because its consumer is a stack. `state-backend provision` is not a stack: it
is what *creates* the backend every config secret is stored behind, so it runs
from a workstation at bring-up and at every rebuild, and there is nothing yet
for a config secret to live in. Its key therefore goes into a **workstation
slot** (§1 rule 6, §4.4) — a file under the checkout's git-ignored
`.credentials/`, written by a `credentials` command and read afterwards
without asking anybody for anything.

The slot is an **OCI SDK configuration file plus the key beside it**, rather
than a shape of this repository's own, because the SDK is what signs with it:
the format is the SDK's, and a containerized `oci` CLI pointed at the same file
reads it unchanged. It holds the credential and nothing else: the compartment
the appliance acts in is a boundary this program decides rather than a property
of the key, so it lives in `conventions` and the provisioner reads it there — a
copy here could only go stale against it.

The slot is the two files side by side, and the reader here holds it to that:
it signs with the key beside the configuration it loaded, whatever the
configuration's `key_file` entry says (`read`). The entry is written all the
same, as an absolute path, because the SDK resolves it against nothing — it
opens the value as given, relative to whatever directory its reader was started
from rather than to the configuration file beside it — and a reader handed the
file alone needs a path that opens. That makes the entry a property of the
checkout that minted the key, and so a default rather than the answer: a
`.credentials/` copied to a checkout at another path provisions as it is, with
no edit and no re-mint (§4.4), and only a reader outside this program — the
`oci` CLI pointed at the copy — still follows the entry back to where the key
was minted.

There is one such slot, written and read here, for `state_backend.provision`.
A second workstation-only consumer would earn a parameter back; inventing one
now would only be a shape nothing has to satisfy.
"""

from __future__ import annotations

import configparser
import io
from pathlib import Path

from ... import conventions
from . import workstation
from .oci_iam import ApiKey

#: `.credentials/oci/<appliance>/`, one level deeper than it has to be so a
#: second consumer's slot would land beside this one rather than move it.
DIRECTORY = 'oci'

#: The profile `oci.config.from_file` reads when none is named. A slot holding
#: one credential has no use for a second.
PROFILE = 'DEFAULT'

CONFIG = 'config'
KEY = 'key.pem'

#: What the configuration says about the key, beside `key_file`: everything the
#: SDK signs with, and all `read` answers with.
CREDENTIAL = ('user', 'fingerprint', 'tenancy', 'region')


class SlotUnusable(RuntimeError):
    """The appliance's OCI slot is absent or incomplete; the message names the repair."""


def directory() -> Path:
    return workstation.directory() / DIRECTORY / conventions.STATE_BACKEND


def config_path() -> Path:
    return directory() / CONFIG


def key_path() -> Path:
    return directory() / KEY


def write(key: ApiKey) -> Path:
    """Fill the slot with a minted key, and return the configuration file's path.

    The key arrives whole rather than as five scalars, which is what `ApiKey`
    is for: the fingerprint is a function of the PEM, and a caller free to
    pass them separately is a caller free to make them disagree.

    The PEM is written before the configuration that names it, so a run
    interrupted between the two leaves a configuration file that is either
    absent or complete rather than one pointing at a key that is not there.
    """
    written = workstation.write(key_path(), key.private_key)
    # No interpolation, which is what the SDK's own reader does: the values
    # here are paths and OCIDs rather than a template, and the default
    # `BasicInterpolation` would refuse a checkout path containing a `%` --
    # after the key it describes is already live in the tenancy.
    profile = configparser.ConfigParser(interpolation=None)
    profile[PROFILE] = {**{name: str(getattr(key, name)) for name in CREDENTIAL}, 'key_file': str(written)}
    rendered = io.StringIO()
    profile.write(rendered)
    return workstation.write(config_path(), rendered.getvalue())


def read() -> dict[str, str]:
    """The slot's credential as the SDK's clients take one, signing with the key beside it.

    `oci.config.from_file` is not used, because it refuses a configuration
    whose `key_file` names no file — which is what the entry becomes the moment
    the slot is copied to a checkout at another path. The parse is the one it
    makes, with no interpolation. The answer is `CREDENTIAL` and the PEM in
    this directory, never the entry's value, and nothing else the file may
    have gained: a `key_content` added by hand would outrank the PEM in the
    SDK's signer, and every setting the SDK defaults, the client defaults
    itself.
    """
    location = config_path()
    parser = configparser.ConfigParser(interpolation=None)
    try:
        found = parser.read(location)
    except configparser.Error as exc:
        raise SlotUnusable(
            f'{location} does not parse ({exc}): `credentials derived oci-state-backend mint` rewrites it'
        ) from exc
    if not found:
        raise SlotUnusable(f'{location} cannot be read: `credentials derived oci-state-backend mint` rewrites it')
    profile = parser[PROFILE]
    missing = [name for name in CREDENTIAL if not profile.get(name)]
    if missing:
        raise SlotUnusable(
            f'{location} has no {", ".join(missing)} in its [{PROFILE}] profile: '
            '`credentials derived oci-state-backend mint` rewrites it'
        )
    key = key_path()
    if not key.is_file():
        raise SlotUnusable(
            f'{location} has no {KEY} beside it: copy the whole slot directory, or re-run '
            '`credentials derived oci-state-backend mint`'
        )
    return {**{name: profile[name] for name in CREDENTIAL}, 'key_file': str(key)}
