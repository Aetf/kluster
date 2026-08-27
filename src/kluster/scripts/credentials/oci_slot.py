"""The OCI key a workstation-only consumer signs with (docs/credentials.md §3).

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
pointed at the same file behaves identically. The compartment travels in the
same file under `compartment-id`, which is not an SDK field — the SDK hands
back every key in the profile — so one path is the whole of what a
provisioning run has to be given.

The key file is named by absolute path, as the client bundle's certificates
are: the SDK expands nothing, so a checkout copied to another path re-runs the
mint rather than editing the file (§4.4).
"""

from __future__ import annotations

import configparser
import io
import logging
from pathlib import Path

from . import workstation

log = logging.getLogger(__name__)

#: The directory under `.credentials/` that holds them, one subdirectory per
#: consumer.
DIRECTORY = 'oci'

#: The one consumer with such a slot. Named here rather than imported from
#: `state_backend.settings`: that package depends on this one, and one string
#: is a cheaper price than the cycle (`lifecycle.URL_FILE` is the same trade).
STATE_BACKEND = 'state-backend'

#: The profile `oci.config.from_file` reads when none is named. A slot holding
#: one credential has no use for a second.
PROFILE = 'DEFAULT'

CONFIG = 'config'
KEY = 'key.pem'

#: Where the appliance's key lived before it became a workstation slot: the
#: XDG path the containerized `oci` CLI reads too. Still read, once and
#: loudly, so a workstation that predates the mint keeps provisioning.
#: TODO(kluster-ops#41): delete this and its reader once every workstation has
#: run `credentials derived oci state-backend`.
LEGACY_CONFIG_FILE = Path.home() / '.config' / 'oci' / 'config'


def directory(consumer: str) -> Path:
    return workstation.directory() / DIRECTORY / consumer


def config_path(consumer: str) -> Path:
    return directory(consumer) / CONFIG


def key_path(consumer: str) -> Path:
    return directory(consumer) / KEY


def write(
    consumer: str,
    *,
    tenancy: str,
    user: str,
    fingerprint: str,
    private_key: str,
    region: str,
    compartment_id: str,
) -> Path:
    """Fill the slot, and return the configuration file's path.

    The key is written before the configuration that names it, so a run
    interrupted between the two leaves a configuration file that is either
    absent or complete rather than one pointing at a key that is not there.
    """
    key = workstation.write(key_path(consumer), private_key)
    # No interpolation, which is what the SDK's own reader does: the values
    # here are paths and OCIDs rather than a template, and the default
    # `BasicInterpolation` would refuse a checkout path containing a `%` --
    # after the key it describes is already live in the tenancy.
    profile = configparser.ConfigParser(interpolation=None)
    profile[PROFILE] = {
        'user': user,
        'fingerprint': fingerprint,
        'tenancy': tenancy,
        'region': region,
        'key_file': str(key),
        'compartment-id': compartment_id,
    }
    rendered = io.StringIO()
    profile.write(rendered)
    return workstation.write(config_path(consumer), rendered.getvalue())


def config_file(consumer: str) -> Path | None:
    """The slot's configuration file, the path it superseded, or None for neither.

    A machine that still has a hand-written configuration where this one used
    to live keeps working, once and loudly: what is there is a complete answer,
    and the warning names the command that replaces it.

    TODO(kluster-ops#41): delete the fallback once every workstation has run
    `credentials derived oci state-backend`.
    """
    current = config_path(consumer)
    if current.is_file():
        return current
    if consumer == STATE_BACKEND and LEGACY_CONFIG_FILE.is_file():
        log.warning(
            'using the OCI configuration in %s: the appliance has a minted key of its own now, '
            'which `credentials derived oci state-backend` writes to %s',
            LEGACY_CONFIG_FILE,
            current,
        )
        return LEGACY_CONFIG_FILE
    return None
