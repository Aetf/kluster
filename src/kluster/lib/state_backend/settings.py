"""Everything the appliance is pinned to.

Values a human chose once and renovate maintains afterwards. The appliance has
no configuration surface beyond this file, the Butane template it feeds and
the `state-backend` stack's configuration: a change here is a change to that
stack, applied by `operator-stack state-backend up`. One that moves what the
box is rendered from replaces the box, which waits for `--force`; a bump of
the image imports a new image and replaces nothing (physical/state-backend.md
§1).
"""

from __future__ import annotations

import datetime as dt

from kluster import conventions
from kluster.conventions import backup

# --- OCI ------------------------------------------------------------------

#: Every resource the stack declares carries the same prefix in its display
#: name, so the appliance's footprint is greppable in a console. The string
#: itself is a convention rather than a setting: the credentials package names
#: the same appliance, and one of the two would eventually be edited alone.
NAME = conventions.STATE_BACKEND

SHAPE = 'VM.Standard.E2.1.Micro'
BOOT_VOLUME_GB = 50
REGION = conventions.OCI_TENANCY.region

#: The appliance's own network (state-backend.md §4). Deliberately not the
#: cluster VCN: that one is a `physical` resource, and Pulumi cannot create it
#: before this box exists.
VCN_CIDR = '10.10.0.0/24'
SUBNET_CIDR = '10.10.0.0/24'

#: Fedora CoreOS, stable stream. `oraclecloud` is a first-class FCOS platform;
#: the qcow2 is imported as a custom image with PARAVIRTUALIZED launch mode.
FCOS_STREAM = 'stable'
FCOS_STREAM_URL = f'https://builds.coreos.fedoraproject.org/streams/{FCOS_STREAM}.json'

#: The release the `state-backend` stack imports its image from (rfc-006
#: ruling 7), and the digest the stream metadata publishes for that release's
#: compressed `oraclecloud` artifact, which the upload checks the download
#: against. A bump imports a new image and replaces nothing else: Zincati
#: keeps the running box current, and the image matters at the next launch.
FCOS_RELEASE = '44.20260913.3.2'
FCOS_ARTIFACT_SHA256 = '0906c9af259af314c2718fbb2384c2046c9a0b72cf1e1e14e9f3ed9dd016c2d3'
FCOS_ARTIFACT_URL = (
    f'https://builds.coreos.fedoraproject.org/prod/streams/{FCOS_STREAM}/builds/{FCOS_RELEASE}'
    f'/x86_64/fedora-coreos-{FCOS_RELEASE}-oraclecloud.x86_64.qcow2.xz'
)

#: The Object Storage bucket the image is uploaded to before OCI imports it.
IMAGE_BUCKET = f'{NAME}-images'

# --- The box --------------------------------------------------------------

#: Pinned to the major line; podman-auto-update follows the minor stream.
POSTGRES_IMAGE = 'docker.io/library/postgres:17'

#: The uid the official image runs Postgres as; the server key is owned by it.
POSTGRES_UID = 999

DATABASE = 'pulumi_state'

#: The client roles: the Common Names the CA issues client certificates to,
#: and the only roles the box admits over TCP. Neither is a superuser; both
#: act as the role that owns the state (state-backend.md §2). That role and
#: the superuser are named in the Butane template alone, since no client ever
#: connects as either.
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

#: The nightly dump, as a systemd calendar expression and as the period it
#: amounts to. Two spellings of one cadence: the timer reads the first, the
#: freshness probe reads the second, and a test holds the calendar form to the
#: period so they cannot drift apart.
DUMP_SCHEDULE = '*-*-* 02:30:00'
DUMP_PERIOD = dt.timedelta(days=1)

#: How old the newest dump may be before the probe calls the backup stale:
#: `conventions.backup.max_age` over the period above, which is the one rule
#: every scheduled backup's threshold follows (state-backend.md §5).
DUMP_MAX_AGE = backup.max_age(DUMP_PERIOD)

#: A scheduled reboot is never mistaken for an incident.
REBOOT_DAY = 'Tue'
REBOOT_TIME = '04:00'
REBOOT_WINDOW_MINUTES = 60

#: The appliance's reserved public IPv4 (state-backend.md §4): the address
#: every client bundle's connection string names and the server certificate's
#: SAN carries. A site fact that follows from the first reservation, recorded
#: here the way the compartment is recorded in `conventions.OCI_TENANCY`, so
#: that a probe run from another repository has an address to check without
#: holding a bundle. Public already, on 5432 and 22 and in the certificate.
#:
#: Recorded rather than looked up, and held: the stack's component refuses a
#: reservation that carries any other address, naming both
#: (`StateBackend._held_address`). A box at another address is a decision this
#: line records, not drift for a run to follow -- the probe and `state-backend
#: ssh` dial this constant with no OCI credential to look anything up, and
#: would otherwise report a dead certificate for a box alive elsewhere.
ADDRESS = '144.24.7.194'

# --- Backups --------------------------------------------------------------

#: Declared by the `state-backend` stack, whose state is committed to this
#: repository rather than kept in the backend whose dumps land here (rfc-006
#: §3).
B2_BUCKET = 'kluster-state-backend'

#: The prefix the bucket's lifecycle rule governs and the appliance uploads
#: under. What the uploader's *key* is confined to is `b2.dumps`, where every
#: other B2 role is stated too; it reads the same `conventions` entry, so the
#: grant and the retention cannot come apart.
B2_PREFIX = conventions.STATE_DUMP_PREFIX

#: Retention is a bucket lifecycle rule, which is what keeps the uploader's
#: key free of any delete capability (storage.md §4).
B2_RETENTION_DAYS = 30

#: The dump key the `state-backend` stack declares is named this, then `-`
#: and the generation its configuration names (rfc-006 §4.1).
B2_DUMP_KEY_NAME = f'{conventions.CLUSTER_NAME}-{NAME}-dump'

# --- The server certificate's renewal ---------------------------------------

#: The stack output carrying when the server certificate in the stack's
#: configuration expires, as an ISO 8601 instant. The stack program exports it
#: and the `operator-stack` driver reads it off every preview (rfc-006 §7).
CERTIFICATE_EXPIRY_OUTPUT = 'serverCertificateExpiry'

#: How much life the server certificate must have left for a run of the
#: `state-backend` stack to stay quiet about it, and the only home of that
#: number: the documents name the margin, never its value. Inside it, the run
#: names the reissue, `credentials derived state-backend-server issue`, and
#: the replacement that carries the new certificate.
#:
#: It is small against the certificate's validity, so a certificate spends a
#: small fraction of its life inside the margin. And it is wider than the
#: probe's alert margin (`kluster.scripts.state_backend.config.EXPIRY_ALERT_MARGIN`),
#: so an operator who runs the stack has been told well before the probe's
#: alert can fire.
RENEWAL_MARGIN = dt.timedelta(days=90)
