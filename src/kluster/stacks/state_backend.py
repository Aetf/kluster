"""The `state-backend` stack: the appliance whose Postgres keeps every other stack's state (rfc-006 §4).

An operator stack whose state is committed (framework/pulumi.md §3.3): the
backend it declares cannot hold its own state, so the checkpoint lives under
`checkpoints/` in this repository and a run of it goes through the
`operator-stack` driver alone. Its configuration is filled by the
`credentials derived` rows the appliance runs on (credentials.md §3).

**This program is wiring** (style/pulumi.md): it builds the stack's two
providers from its configuration, reads the stable keys the box is rendered
from, and declares the appliance as one component.
"""

from __future__ import annotations

from pathlib import Path

import pulumi
import pulumi_b2 as b2
import pulumi_oci as oci

from kluster import conventions
from kluster.components.state_backend import Keys, StateBackend
from kluster.lib import stack_environment
from kluster.lib import workstation as lib_workstation

#: Where this stack's configuration holds what this program reads. Bare, and
#: so in this project's namespace, for the reason every stack's provider keys
#: are (`stacks/dns.py`). The `credentials derived` rows that write them spell
#: the same keys (`kluster.scripts.credentials.derived`), and a test holds the
#: two spellings equal.
#:
#: The OCI signing configuration's secrets; the tenancy and the region are
#: the account's, in `conventions`.
OCI_USER_OCID = 'ociUserOcid'
OCI_FINGERPRINT = 'ociFingerprint'
OCI_PRIVATE_KEY = 'ociPrivateKey'
#: The stack's own B2 management key, both halves.
B2_KEY_ID = 'b2ApplicationKeyId'
B2_KEY = 'b2ApplicationKey'
#: The stable keys of rfc-006 §5: the server's key and certificate, the CA's
#: certificate, and the box's SSH host key.
SERVER_KEY = 'serverKey'
SERVER_CERTIFICATE = 'serverCertificate'
CA_CERTIFICATE = 'caCertificate'
SSH_HOST_KEY = 'sshHostKey'

#: The dump key's generation. Bumping it is the dump key's rotation, a
#: committed edit that replaces the key and so the box (rfc-006 §4.1). Absent
#: is the first generation.
DUMP_KEY_GENERATION = 'dumpKeyGeneration'
FIRST_DUMP_KEY_GENERATION = 1


async def main() -> None:
    config = pulumi.Config()
    compartment_id = conventions.OCI_TENANCY.compartments[conventions.STATE_BACKEND].require()
    tenancy_id = conventions.OCI_TENANCY.tenancy_ocid
    region = conventions.OCI_TENANCY.region

    # Both providers are the appliance's, built here from the stack's own
    # configuration, at the line that reads each credential (rfc-002 §8.1).
    # The user's OCID and the key's fingerprint are secrets too, though the
    # schema marks neither: they name the principal the key signs as
    # (rfc-006 §3.3).
    cloud = oci.Provider(
        f'{conventions.STATE_BACKEND}-oci',
        region=region,
        tenancy_ocid=tenancy_id,
        user_ocid=config.require_secret(OCI_USER_OCID),
        fingerprint=config.require_secret(OCI_FINGERPRINT),
        private_key=config.require_secret(OCI_PRIVATE_KEY),
    )
    backups = b2.Provider(
        f'{conventions.STATE_BACKEND}-b2',
        application_key_id=config.require_secret(B2_KEY_ID),
        application_key=config.require_secret(B2_KEY),
    )

    # Read in the clear, because the component holds the keys to each other
    # before it declares anything (`refuse_out_of_step`); it hands the
    # secrets on only inside the instance's secret `metadata`.
    keys = Keys(
        ca_certificate=config.require(CA_CERTIFICATE),
        server_certificate=config.require(SERVER_CERTIFICATE),
        server_key=config.require(SERVER_KEY),
        ssh_host_key=config.require(SSH_HOST_KEY),
    )

    _ = StateBackend(
        conventions.STATE_BACKEND,
        compartment_id=compartment_id,
        tenancy_id=tenancy_id,
        region=region,
        keys=keys,
        dump_key_generation=config.get_int(DUMP_KEY_GENERATION, FIRST_DUMP_KEY_GENERATION),
        # The `operator` client bundle's slot: it authenticates to the
        # estate's backend rather than to a provider of this stack, so it is
        # read from the workstation and handed down, never copied into
        # configuration (credentials.md §1, rule 6).
        bundle_dir=lib_workstation.directory() / stack_environment.BUNDLE_SLOT,
        # A run starts in the checkout, where `state-backend dump` writes a
        # dump too; `.gitignore` names the file.
        dump_directory=Path.cwd(),
        opts=pulumi.ResourceOptions(providers=[cloud, backups]),
    )
