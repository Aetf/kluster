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

#: The box's Postgres image: the major line's tag, and the digest it named
#: when the pin last moved, which is what the box pulls and runs. The digest
#: changes with every rebuild of the tag -- a minor release, or a new Debian
#: base -- and renovate moves it by a pull request opened on the first of
#: the month, in a group of the appliance's image refreshes apart from its
#: releases; nothing on the box moves it. The tag is what the local client's
#: major is held to (`tests/test_postgres_client.py`). The three lines are
#: one pair for renovate's manager, which reads them in this order.
POSTGRES_REPOSITORY = 'docker.io/library/postgres'
POSTGRES_TAG = '17'
POSTGRES_DIGEST = 'sha256:3b0cda93be7c8dba2ed484d19393d93845dc3dafb88973e248ba8e6d99d784e1'
POSTGRES_IMAGE = f'{POSTGRES_REPOSITORY}:{POSTGRES_TAG}@{POSTGRES_DIGEST}'

#: The uid the official image runs Postgres as; the data directory, and the
#: directory its socket is shared through, are owned by it.
POSTGRES_UID = 999

#: The box's pooler, PgBouncer, pinned the way the Postgres image is: the
#: release's tag, and the digest it named when the pin last moved, which is
#: what the box pulls and runs. The image is the CloudNativePG project's
#: build, the project whose operator the cluster already runs. The tag is
#: rebuilt for every new base, and renovate moves the digest by a pull
#: request opened on the first of the month, as it moves the Postgres
#: image's, and the tag to a new release, with its digest, by a pull request
#: in the appliance's group on any day. The three lines are one pair for
#: renovate's manager, which reads them in this order.
PGBOUNCER_REPOSITORY = 'ghcr.io/cloudnative-pg/pgbouncer'
PGBOUNCER_TAG = '1.26.1'
PGBOUNCER_DIGEST = 'sha256:6c91fd7383b8b098bc0c3b71dba4e98bd568c0d301b9fc66027fbc23259c6a7d'
PGBOUNCER_IMAGE = f'{PGBOUNCER_REPOSITORY}:{PGBOUNCER_TAG}@{PGBOUNCER_DIGEST}'

#: The uid and gid the pooler's image runs PgBouncer as. The server key is
#: owned by the uid, and the gid is what reaches the directory Postgres's
#: socket is shared through.
PGBOUNCER_UID = 998
PGBOUNCER_GID = 996

#: The state's database, and the name of the pooler's transaction entry for
#: it, which every `pulumi` session and every query is pooled through. The
#: name is the database's because Pulumi's backend prefixes every key with
#: the database its URL names.
DATABASE = 'pulumi_state'
#: The pooler's session entry for the same database: one server process,
#: `operator`'s alone, which `pg_dump` and `pg_restore` connect through
#: (`kluster.lib.bundle.Purpose.DUMP`).
SESSION_DATABASE = f'{DATABASE}_session'

#: The client roles: Common Names the CA issues client certificates to, and
#: roles the pooler admits from the network. Neither is a superuser; both act
#: as the role that owns the state (state-backend.md §2). That role and the
#: superuser are named in the Butane template alone, since no client ever
#: connects as either.
CI_ROLE = 'ci'
OPERATOR_ROLE = 'operator'
#: The role the cluster's daily upkeep of the state table connects as: the
#: census, the prune of expired rows and the ceiling. The CA issues client
#: certificates to it as well, and the pooler admits it from the network into
#: the transaction entry alone. It is no member of the owning role and acts
#: as itself: it reads and deletes rows of the state table, and holds the
#: lock on the table that `DELETE` carries, and nothing more
#: (state-backend.md §2). The roles a certificate can name are the two
#: client roles and this one.
UPKEEP_ROLE = 'upkeep'
#: The port clients reach the box on, where the pooler listens.
PORT = 5432

# --- How many statements Postgres runs at once ------------------------------
#
# The pooler's transaction entry admits at most `POOL_CAP` server
# connections to the state, so at most that many `pulumi` statements and
# queries run in Postgres at once, whatever the number of clients; a client
# beyond it waits in the pooler (state-backend.md §2). The cap is derived from
# what one statement costs and what the box has left for statements, never
# chosen, so each input below is a measurement or a setting of the box, and a
# change to any of them moves the cap through `_pool_cap`. Memory is in MiB,
# which is what `free -m` reports.

#: Available memory on the running box at idle: the lower of two readings
#: (kluster-ops#784), the higher being `BOX_IDLE_AVAILABLE_MIB +
#: BOX_IDLE_SWING_MIB`.
BOX_IDLE_AVAILABLE_MIB = 367
#: How far the idle readings moved between them, which is the margin the
#: budget keeps: a budget closer to the edge than this is inside the box's
#: own noise.
BOX_IDLE_SWING_MIB = 52

#: Postgres's `shared_buffers`, set on the box rather than left to the
#: image's default so that the budget reads the value the box runs with. It
#: is counted whole, as if none of it were resident at idle.
SHARED_BUFFERS_MIB = 128

#: The peak memory of the server process serving one write of a checkpoint,
#: per byte of the value it writes, and the baseline of every server process
#: (state-backend.md §2). Of the backend's statements the write costs most.
WRITE_MULTIPLE = 5.8
BACKEND_BASELINE_MIB = 3

#: The largest row the cap is derived for, in bytes: a bound the state
#: table is held to, not a reading of it (state-backend.md §2). It is set
#: as high as it goes while the cap still covers the most statements CI
#: was measured to run at once, rounded clear of the value where the cap
#: would fall, and its room over the largest value `state-backend census
#: --ceiling` prints outlasts the month between two digest refreshes, so a
#: new bound rides a replacement the box takes anyway. The ceiling fails
#: naming any row over it, a row the cap was not derived for, and the bound
#: is then set again by the same rule. A row's size here is its value's,
#: the size the ceiling reads: the bytes a client writes and reads, which
#: is what each multiple below is per byte of, and not the smaller size
#: TOAST's compression stores it at. Every row is one a statement through
#: the pooler can touch -- a listing sizes each key it finds -- and one
#: each dump reads, so the one value bounds all three.
LARGEST_ROW_BYTES = 3_000_000

#: The nightly dump reaches Postgres on the box's local socket, around the
#: pooler, so its memory is outside the cap: the peak of the Postgres
#: container while `pg_dump -Fc` reads a row, per byte of the row.
DUMP_MULTIPLE = 3.4

#: A dump or restore through the pooler's session entry, the one server
#: process beside the cap: a read's peak, per byte of the row it reads.
SESSION_MULTIPLE = 2.1

#: The pooler's own memory: its resident set with no client, which a
#: statement streaming through it does not move, and what each connected
#: client adds to it.
POOLER_BASE_MIB = 17
POOLER_PER_CLIENT_MIB = 0.044
#: How many client connections the pooler holds at once; the next is
#: refused. It is what Postgres's own default `max_connections` admitted
#: before the pooler, so the box refuses a client at no lower count than
#: it did.
MAX_CLIENTS = 100

_MIB = 2**20


def _pool_cap() -> int:
    """How many statements may run in Postgres at once: the budget over what one statement costs, rounded down.

    The budget is what the idle box has available, less the margin, less
    `shared_buffers`, the nightly dump's reader, the session entry's server
    process and the pooler at its client limit. Refused below one, because
    the pooler reads a cap of zero as no cap at all.
    """
    row = LARGEST_ROW_BYTES / _MIB
    budget = (
        BOX_IDLE_AVAILABLE_MIB
        - BOX_IDLE_SWING_MIB
        - SHARED_BUFFERS_MIB
        - DUMP_MULTIPLE * row
        - (SESSION_MULTIPLE * row + BACKEND_BASELINE_MIB)
        - (POOLER_BASE_MIB + MAX_CLIENTS * POOLER_PER_CLIENT_MIB)
    )
    statement = WRITE_MULTIPLE * row + BACKEND_BASELINE_MIB
    cap = int(budget // statement)
    if cap < 1:
        raise ValueError(f'the box has {budget:.0f} MiB for statements and one costs {statement:.0f} MiB')
    return cap


POOL_CAP = _pool_cap()

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

#: The retention rule of the state table (state-backend.md §2), which
#: `state-backend census --prune-expired` enforces (`table.expired`). No row
#: younger than `EXPIRY_AGE` expires: two dump periods, so a row that old has
#: stood through two scheduled dumps and one missed run leaves it in the
#: other, and half a period past `DUMP_MAX_AGE`, so while the dump-age probe
#: passes the newest dump holds every row a prune takes. Past that age a
#: checkpoint copy expires, and so does an update record that is not among
#: the newest `HISTORY_RECORDS` of its stack's history directory: the page
#: the reading of every stack's history asks for (sources-of-truth.md §4).
EXPIRY_AGE = 2 * DUMP_PERIOD
HISTORY_RECORDS = 1000

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
