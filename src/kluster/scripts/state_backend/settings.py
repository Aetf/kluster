"""Everything the appliance is pinned to.

Values a human chose once and renovate maintains afterwards. The appliance has
no configuration surface beyond this file and the Butane template it feeds:
changing anything here means re-provisioning, which is the only apply path
this box has (physical/state-backend.md §1).
"""

from __future__ import annotations

from kluster import conventions

# --- OCI ------------------------------------------------------------------

#: Every resource this script creates carries the same prefix, so the
#: appliance's footprint is greppable in a console and safe to clean up. The
#: string itself is a convention rather than a setting: the credentials
#: package names the same appliance, and one of the two would eventually be
#: edited alone.
NAME = conventions.STATE_BACKEND

SHAPE = 'VM.Standard.E2.1.Micro'
BOOT_VOLUME_GB = 50
REGION = conventions.OCI_REGION

#: The appliance's own network (state-backend.md §4). Deliberately not the
#: cluster VCN: that one is a `physical` resource, and Pulumi cannot create it
#: before this box exists.
VCN_CIDR = '10.10.0.0/24'
SUBNET_CIDR = '10.10.0.0/24'

#: Fedora CoreOS, stable stream. `oraclecloud` is a first-class FCOS platform;
#: the qcow2 is imported as a custom image with PARAVIRTUALIZED launch mode.
FCOS_STREAM = 'stable'
FCOS_STREAM_URL = f'https://builds.coreos.fedoraproject.org/streams/{FCOS_STREAM}.json'

# --- The box --------------------------------------------------------------

#: Pinned to the major line; podman-auto-update follows the minor stream.
POSTGRES_IMAGE = 'docker.io/library/postgres:17'

#: The uid the official image runs Postgres as; the server key is owned by it.
POSTGRES_UID = 999

DATABASE = 'pulumi_state'
CI_ROLE = 'ci'
OPERATOR_ROLE = 'operator'
PORT = 5432

#: age, fetched once at first boot and verified against this hash.
AGE_VERSION = 'v1.3.1'
AGE_URL = f'https://github.com/FiloSottile/age/releases/download/{AGE_VERSION}/age-{AGE_VERSION}-linux-amd64.tar.gz'
AGE_SHA256 = 'bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377'

#: Dumps are encrypted to this generation and the one before it, so any object
#: in retention opens with the current or the previous key. Bumping this is
#: what rotates the backup identity: the escrow expects a ciphertext for each
#: generation the window names (`credentials derived check`), and the window is
#: clamped at the first, there being nothing before it.
AGE_GENERATION = 1

DUMP_SCHEDULE = '*-*-* 02:30:00'

#: A scheduled reboot is never mistaken for an incident.
REBOOT_DAY = 'Tue'
REBOOT_TIME = '04:00'
REBOOT_WINDOW_MINUTES = 60

# --- Backups --------------------------------------------------------------

#: Created by the provision script rather than by Pulumi, for the same reason
#: the VCN is: the dumps must have somewhere to land before Pulumi exists.
B2_BUCKET = 'kluster-state-backend'
B2_PREFIX = conventions.STATE_DUMP_PREFIX
B2_DUMP_KEY_NAME = 'kluster-state-dump'

#: Retention is a bucket lifecycle rule, which is what keeps the uploader's
#: key free of any delete capability (storage.md §4).
B2_RETENTION_DAYS = 30
