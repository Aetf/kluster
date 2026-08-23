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
-   **OS updates: Zincati `periodic` strategy** — reboots confined to
    a weekly maintenance window (exact window chosen at
    implementation), not finalized the moment a rollout arrives:
    the blip is the same either way, but a *scheduled* blip is never
    mistaken for an incident and never coincides with someone
    mid-operation. A reboot is a brief 5432 outage, accepted:
    Postgres shutdown is systemd-ordered, nothing corrupts, and the
    clients retry.
-   **Every hand operation is a script.** The box is outside Pulumi,
    but not outside version control: `deploy/state-backend/` carries
    the executable form of every operation this document names —
    provision/re-provision, NSG sync, restore, key rotation. The
    playbooks (§7) *invoke* these scripts; a procedure that exists
    only as prose in a playbook is a bug.
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
    unified alert channel (architecture.md §4.3). Response: playbook §7.1.

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
    **prefix-scoped, write-only key**. Pruning is not the box's job:
    RPO ≤ 24 h is fine — state is re-derivable from reality
    (`pulumi refresh`/import) at worst.
-   **Retention, explicit: STANDARD class — daily, kept 30 days —
    enforced by a B2 lifecycle rule on the prefix** (Pulumi-managed
    with the bucket, storage.md §4), not by the uploader. That keeps
    the box's key free of delete/prune capability (the H4
    discipline), and it is what gives retired encryption keys a
    definite end of life (below).
-   Freshness is asserted **from outside** by the scheduled workflow
    (object-age on the prefix, ci.md §3) — the box monitors nothing
    about itself.
-   **The age identity rotates by generations; no key is assumed
    immortal.** Rotation is a designed path, not an emergency
    improvisation:
    -   **Every dump is encrypted to the two newest generations** —
        age is natively multi-recipient, and both public keys sit in
        the Butane file. This makes per-object key attribution
        unnecessary (deploys are intentionally manual, so git dates
        prove nothing about which key an object carries): any object
        in retention decrypts with the current *or* previous key,
        and 30 days after a rotation the current key alone covers
        the entire retention window.
    -   **Rotate at least yearly** (and on compromise or custody
        change): generate generation N+1 offline, swap the Butane
        recipients `[N, N−1] → [N+1, N]`, re-provision — playbook
        §7.5, via the rotation script (§1). The path stays warm
        because it is the same re-provision as everything else.
    -   **Old keys get a definite end of life**: generation N−1
        becomes destroyable **30 days after the rotation to N+1** —
        every object it can uniquely decrypt has aged out, and
        everything newer also carries key N. The offline register
        records each generation with its rotate date and
        earliest-destroy date; destroying the key on that date is
        what actually ends the old generation's exposure. At most
        two private keys are ever live.
    -   **Compromise variant**: drop the compromised key from the
        recipients entirely (don't keep dual-encrypting to it),
        take a fresh dump, delete the old objects early (their
        hidden versions persist ≤30 days by the anti-ransomware
        floor — accepted: the dump's payload is still
        passphrase-encrypted underneath, so a leaked age key alone
        reads nothing).
-   **The age identity is deliberately independent of the CA.**
    Deriving one from the other was considered (age can encrypt to
    ssh-ed25519 recipients, so one shared ed25519 key was possible)
    and rejected: coupling makes either key's compromise or rotation
    drag the other along, and the two rotate on different triggers
    and cadences (CA: expiry/compromise over ~years; age: yearly
    generations). The saving would be one line in the offline
    register. Both live at the never-in-automation tier.

## 6. Monitoring

Everything observable lives **outside** the box:

-   Every CI job and local `pulumi` operation is an implicit
    5432 + TLS + auth probe — backend-down is discovered by the first
    thing that needs it, which is the only thing that cares.
-   The scheduled workflow (ci.md §3) asserts pg_dump freshness
    (object-age on B2) and server-cert expiry (≥30 days), alerting
    into the unified alert channel (architecture.md §4.3). Each alert maps to a playbook: stale
    dump → §7.4's restore path doubles as the diagnosis start; cert
    expiry → §7.1; connection timeouts → §7.2.
-   **Deliberately unmonitored, with rationale**: Zincati/update
    failures and disk fill. The DB is ~4 orders of magnitude under
    the disk, and OS staleness is bounded by the quarterly
    re-provision drill (§7.4), which always lands the current image.
    If either ever bites first, that is the signal to add the probe —
    not before.

## 7. Playbooks

Per the alert discipline (architecture.md §4.3), each alert above maps
to a playbook. **This section is the design-level census** — title,
trigger, and outline only, enough to show what must exist; the
executable playbooks (the §1 scripts plus their runbooks in
`deploy/state-backend/`) are written with the implementation.

-   **§7.1 Certificate rotation / CA reissue.** Trigger: expiry alert
    (<30 days) or key compromise. Outline: issue offline (compromise:
    regenerate CA, reissue all three certs, redistribute client
    certs) → update Butane → re-provision → `verify-full` check.
-   **§7.2 NSG range refresh.** Trigger: connection *timeouts* from
    CI/local (auth errors are §7.1's domain). Outline: NSG-sync
    script against current `api.github.com/meta` + home /32 → re-run
    the failed job.
-   **§7.3 Postgres major upgrade.** Trigger: renovate major pin PR.
    Outline: fresh dump → merge → re-provision (new major initdb's) →
    restore → clean `pulumi preview`.
-   **§7.4 Rebuild / DR drill (quarterly).** §7.3 minus the pin bump,
    sourced from the latest age-encrypted B2 object — one pass
    exercises B2 download, the offline age identity,
    provision-from-Butane, restore, and cert delivery.
-   **§7.5 age identity rotation.** Trigger: yearly cadence, key
    compromise, custody change. Outline: generate N+1 offline +
    register entry (rotate / earliest-destroy dates) → swap Butane
    recipients `[N, N−1] → [N+1, N]` → re-provision → verify both
    decrypt paths → destroy N−1 on its date (compromise: drop the
    key from recipients now, fresh dump, early-delete old objects).
