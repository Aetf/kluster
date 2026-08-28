"""The OCI key the state-backend appliance signs with (docs/credentials.md §3).

A per-stack OCI key normally reaches its consumer as a Pulumi config secret,
because its consumer is a stack. `state-backend provision` is not a stack: it
is what *creates* the backend every config secret is stored behind, so it runs
from a workstation at bring-up and at every rebuild, and there is nothing yet
for a config secret to live in. Its key therefore goes into a **workstation
slot** (§1 rule 6, §4.4) — a file under the checkout's git-ignored
`.credentials/`, written by a `credentials` command and read afterwards
without asking anybody for anything.

The slot is an **OCI SDK configuration file plus the key it names**, rather
than a shape of this repository's own, because the SDK is the whole of the
reader: `oci.config.from_file` needs no adapter, and a containerized `oci` CLI
pointed at the same file behaves identically. It holds the credential and
nothing else: the compartment the appliance acts in is a boundary this program
decides rather than a property of the key, so it lives in `conventions` and
the provisioner reads it there — a copy here could only go stale against it.

The `key_file` entry is an absolute path because the SDK resolves it against
nothing: it opens the value as given, relative to whatever directory the reader
was started from rather than to the configuration file beside it. The path is
therefore a property of where this checkout sits, and a checkout copied
somewhere else re-runs the mint rather than having its slot edited (§4.4).

There is one such slot, and it is written here and read in
`state_backend.provision` — including the fallback to the path this one
superseded, which lives with that reader so the two die together. A second
workstation-only consumer would earn a parameter back; inventing one now would
only be a shape nothing has to satisfy.
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
    profile[PROFILE] = {
        'user': key.user,
        'fingerprint': key.fingerprint,
        'tenancy': key.tenancy,
        'region': key.region,
        'key_file': str(written),
    }
    rendered = io.StringIO()
    profile.write(rendered)
    return workstation.write(config_path(), rendered.getvalue())
