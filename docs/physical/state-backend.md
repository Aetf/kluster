# Physical Design: the State-Backend Appliance

The OCI **VM.Standard.E2.1.Micro** (Always Free, x86, 1 GB) running the
Pulumi `postgres://` state backend for all four stacks. It sits
**beneath** Pulumi — a bootstrap dependency that must exist before any
stack can act — so it is the one hand-created OCI resource, provisioned
from `deploy/state-backend/` in this repo. Design goal: a
**zero-maintenance appliance** — the box carries no state that
`pg_dump` + re-provision can't rebuild, and every operational path is
either automated or a written playbook (§7).

> **Status**: designed 2026-08-24, extracting and completing the
> appliance design begun in [framework/ci.md](../framework/ci.md) §1
> (which keeps the *decision* — what the backend is and why it lives
> here — and points at this document for everything about the box).
> Why-it-moved-off-the-homelab and the OCI-Container-Instances
> rejection live there. Not implemented.

## 1. OS & configuration management

**Fedora CoreOS, stable stream.** Verified facts (2026-08-24, official
FCOS docs): OCI is a supported platform (`coreos-installer download -p
oraclecloud`, x86_64), the qcow2 imports as a custom image
(PARAVIRTUALIZED launch mode), and Ignition is delivered as instance
`user_data` — FCOS reads it in place of cloud-init.

-   **Single source of truth: `deploy/state-backend/`** — one Butane
    file plus a provision script wrapping the `oci` CLI (image import
    if needed, instance launch with the rendered Ignition, NSG rule
    list). Butane renders to Ignition at provision time (`butane` via
    mise). The Butane file is reviewed like any code: renovate opens
    pin-bump PRs against it (§2), humans merge them.
-   **The only apply path is re-provision.** No configuration agent,
    no SSH mutation: any change = PR to the Butane file → hand-run
    re-provision (terminate + launch with the new `user_data`;
    minutes of 5432 downtime — CI retries, local ops re-run). SSH
    exists (operator key in Ignition) for **diagnosis only**. The
    no-drift rule is what makes "the repo describes the box" true;
    the quarterly drill (§7.4) is what keeps that claim tested.
-   **OS updates: Zincati defaults** — immediate finalize + reboot as
    stable-stream rollouts arrive. A reboot is a brief 5432 blip,
    accepted: Postgres shutdown is systemd-ordered, nothing corrupts,
    and the clients retry. No update-window machinery.
-   **Secrets ride Ignition, accepted**: the server TLS key (§3) and
    the B2 upload credential (§5) are in `user_data`. On this box
    that's fine where it wasn't for cluster nodes (audit H1): no
    untrusted workload runs here, so instance metadata is readable
    only by the instance itself and OCI principals with compartment
    read — who are root-equivalent for this box anyway. Rotating
    either secret is a re-provision.

## 2. Postgres

-   A **podman quadlet** unit, image pinned to the major line
    (`postgres:NN`) in the Butane file; a `podman auto-update` timer
    applies minor/patch releases — the same trust-the-stream posture
    as the OS, safe for the same reason (nothing outlives
    `pg_dump` + re-provision).
-   **Major upgrades take the rebuild path** (§7.3): final dump → pin
    bump → re-provision (fresh data dir, initdb'd by the new major) →
    restore. At tens of MB of state, owning `pg_upgrade` machinery
    buys nothing. (The *in-cluster* CNPG databases are the opposite
    case — their major-upgrade policy is workloads.md §4.)
-   Config: TLS on, `scram-sha-256`, `pg_hba` requiring certificate
    auth (§3); data directory on the boot volume (the DB is four
    orders of magnitude smaller than the disk).

## 3. PKI: a tiny offline CA

(Decisions from ci.md §1, 2026-08-24; this is the owning section now.)

-   **One single-purpose private CA** (~10-year validity), generated
    offline; the CA key never touches the micro, CI, or Pulumi state —
    it lives at the never-in-automation tier with the B2 master
    credential (storage.md §4). Exactly three certificates under it:
-   **Server cert, 2–3 years, SAN = the micro's reserved public IP.**
    Clients connect by literal IP with `sslmode=verify-full` (libpq
    matches IP SANs), keeping the state-backend hot path free of any
    DNS dependency — the backend stays reachable when Cloudflare or
    the `dns` stack is itself the thing being repaired.
-   **Two client certs — `ci` and `operator`**; keys held as CI
    Environment secrets / local mise env respectively.
-   **No CRL/OCSP.** At three certificates, revocation infrastructure
    is standing rent for nothing: the compromise response is
    "regenerate the CA, reissue all three, re-provision" — playbook
    §7.1.
-   **Expiry is monitored, not remembered**: the scheduled workflow
    (ci.md §3) asserts ≥30 days remaining on the server cert via an
    `openssl s_client` probe (no credentials needed), failing into the
    HA push channel. Response: playbook §7.1.

## 4. Network exposure

Public 5432 with TLS + scram + **mandatory client certificates** — the
client cert is the wall. The OCI NSG is a **coarse pre-filter, not the
wall**: 5432 narrowed to a *snapshot* of the published GitHub Actions
ranges (`api.github.com/meta`) plus the home uplink's current /32 for
local runs. No automatic refresh — the ranges drift, and when a shift
breaks CI the symptom is a **connection timeout** (not an auth
failure); the response is playbook §7.2. A scheduled workflow
auto-editing security rules was considered and rejected: a standing
OCI write-credential to save a rare two-line edit fails the
standing-rent test.

## 5. Backup

-   A systemd timer (quadlet) runs `pg_dump -Fc`, **age-encrypts**
    the dump — it holds every stack's ciphertext *and* salt — and
    uploads to B2 under the state-backend prefix with a
    **prefix-scoped, write-capable key** (storage.md §4 key
    discipline). Retention follows the STANDARD-class vocabulary
    (workloads.md §3). RPO ≤ 24 h is fine — state is re-derivable
    from reality (`pulumi refresh`/import) at worst.
-   Freshness is asserted **from outside** by the scheduled workflow
    (object-age on the prefix, ci.md §3) — the box monitors nothing
    about itself.
-   **The age identity is deliberately independent of the CA.**
    Deriving one from the other was considered (age can encrypt to
    ssh-ed25519 recipients, so one shared ed25519 key was possible)
    and rejected: coupling makes either key's compromise or rotation
    drag the other along, the two artifacts have different lifetimes
    (CA ~10 years; backup identity indefinite), and the saving is one
    line in the offline register. Both live at the
    never-in-automation tier.

## 6. Monitoring

Everything observable lives **outside** the box:

-   Every CI job and local `pulumi` operation is an implicit
    5432 + TLS + auth probe — backend-down is discovered by the first
    thing that needs it, which is the only thing that cares.
-   The scheduled workflow (ci.md §3) asserts pg_dump freshness
    (object-age on B2) and server-cert expiry (≥30 days), alerting
    into the HA push channel. Each alert maps to a playbook: stale
    dump → §7.4's restore path doubles as the diagnosis start; cert
    expiry → §7.1; connection timeouts → §7.2.
-   **Deliberately unmonitored, with rationale**: Zincati/update
    failures and disk fill. The DB is ~4 orders of magnitude under
    the disk, and OS staleness is bounded by the quarterly
    re-provision drill (§7.4), which always lands the current image.
    If either ever bites first, that is the signal to add the probe —
    not before.

## 7. Playbooks

Per the alert discipline (architecture.md §4.2): an automation alert
without a written response is not shipped. The four for this box:

### 7.1 Certificate rotation / CA reissue

Trigger: the expiry alert (<30 days), or key compromise.

1.  On the offline CA medium: issue a new server cert (SAN = the
    reserved public IP, 2–3 years). **Compromise only**: regenerate
    the CA first, then reissue all three certs.
2.  Update the server cert + key in the Butane file (the key via the
    provision-time secret mechanism, not committed); re-provision
    (§1).
3.  **Compromise only**: distribute the new `ci` client cert to the
    CI Environment secrets and `operator` to the local mise env.
4.  Verify: `psql "sslmode=verify-full …"` as operator; re-run the
    last failed CI job if any; the expiry probe is green on the next
    scheduled run.

### 7.2 NSG range refresh

Trigger: CI or local `pulumi` operations failing with **connection
timeouts** (auth errors mean something else — see §7.1).

1.  `curl https://api.github.com/meta | jq .actions` for the current
    ranges; `curl ifconfig.me` from the home network for the /32.
2.  Update the NSG rules (console or `oci network nsg rules update`);
    the intended rule list lives in `deploy/state-backend/` — update
    it in the same change.
3.  Verify: re-run the failed job / operation.

### 7.3 Postgres major upgrade

Trigger: renovate opens a major pin-bump PR against the Butane file.

1.  Take a manual `pg_dump` (or verify the latest nightly B2 object
    is fresh — same age-encrypted artifact either way).
2.  Merge the PR; re-provision. The fresh data directory is initdb'd
    by the new major.
3.  Restore the dump over the operator connection.
4.  Verify: `pulumi preview` on any stack reads clean.

### 7.4 Rebuild / DR drill (quarterly)

The §7.3 procedure minus the pin bump, sourced from the latest
**age-encrypted B2 object** — which exercises, in one pass: the B2
download path, the offline age identity, provision-from-Butane, the
restore, and cert delivery. Success = clean `pulumi preview`. One
drill covers disaster recovery, the major-upgrade path, and
certificate rotation, because all three are the same re-provision.
