# Physical Design: the State-Backend Appliance

The OCI **VM.Standard.E2.1.Micro** (Always Free, x86, 1 GB) running the
Pulumi `postgres://` state backend for every stack. It sits
**beneath** Pulumi — a bootstrap dependency that must exist before any
stack can act — so it is the one hand-created OCI resource, provisioned
from `deploy/state-backend/` in this repo. Design goal: a
**zero-maintenance appliance** — the box carries no state that
`pg_dump` + re-provision can't rebuild, and every operational path is
either automated or a written playbook (§7).

The availability domain is **chosen by asking which one offers the
shape**, not taken as the first one listed: this shape is offered in
exactly one of Phoenix's three ADs. Launching into either of the other
two returns `404 NotAuthorizedOrNotFound`, an error naming neither the
shape nor the domain, which reads like a permissions problem.

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
    if needed, instance launch with the rendered Ignition, and the
    trivial NSG — 5432/22 open, §4). Butane renders to Ignition at provision time (`butane` via
    mise). The Butane file is reviewed like any code: renovate opens
    pin-bump PRs against it (§2), humans merge them.
-   **The only apply path is re-provision.** No configuration agent,
    no SSH mutation: any change = PR to the Butane file → run
    `state-backend provision`, which terminates and relaunches when the
    running box no longer matches the commit (minutes of 5432 downtime
    — CI retries, local ops re-run). SSH exists (operator key in
    Ignition) for **diagnosis only** — `state-backend ssh` looks the
    address up and logs in, so reading a log does not start with
    finding an IP. Note the key set is
    `deploy/state-backend/operator-keys.txt`: a workstation whose key
    is not in it cannot reach the box at all, which is a re-provision
    to fix, not an `ssh-copy-id`. The address is reserved and the box
    is cattle, so each replace gives the same address a new host key
    and ssh reports a possible man-in-the-middle; the replace path
    therefore drops the destroyed box's key from `known_hosts` itself.
    The
    no-drift rule is what makes "the repo describes the box" true;
    the quarterly drill (§7.3) is what keeps that claim tested.
-   **The box decides that by carrying its own bill of materials.**
    At launch, the instance's metadata records a digest per component
    of what it was built from — the Butane file, the operator keys,
    each pin, the certificate identities, the dump key's id — and a
    converge run recomputes them and replaces the box when any differs,
    naming the ones that did. Certificates are compared by subject,
    public key and SANs rather than by bytes, because they are
    re-issued on every render and would otherwise read as permanent
    drift. A box with no such record (built before this existed) counts
    as drifted: silence is not evidence that it matches.
-   **The B2 dump key is one of those components, not a special case.**
    B2 returns an application key's secret once, so the box's copy
    cannot be read back and minting a replacement revokes what it is
    holding — a run that re-minted and then left the instance untouched
    broke the nightly dump silently until it next fired. So the key is
    minted only on the branch that launches a box, and the converge
    asks B2 whether the *recorded* key still exists with the scope
    settings.py wants: if it does not, the box cannot be handed the
    intended key without being rebuilt, which is the same replace as
    any other drift. **The dump key's lifetime is the instance's.**
    `--replace` remains for the case with no diff to find: rotating the
    dump key, or discarding a box broken in a way metadata cannot show.
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
    provision/re-provision, restore, key rotation. The
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
-   **Major upgrades take the rebuild path** (§7.2): final dump → pin
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
-   **Expiry is monitored, not remembered**: the ops repo's
    scheduled workflow (ci.md §3) asserts ≥30 days remaining on the
    server cert via an
    `openssl s_client` probe (no credentials needed), failing into the
    unified alert channel (architecture.md §4.3). Response: playbook §7.1.

## 4. Network exposure

**The appliance owns its own network.** A VCN, public subnet, internet
gateway, NSG and reserved public IP, all created by the provision script
and none of them the cluster's: the cluster VCN is a `physical`-stack
resource, and putting the box inside it would invert the dependency this
whole design exists to avoid (Pulumi needs the backend before it can
create anything). The isolation is a bonus, not the point. The **reserved**
public IP is load-bearing rather than tidy — the server certificate's SAN
is that literal address, so an ephemeral IP would invalidate the
certificate on every re-provision.

Public 5432 with TLS + scram + **mandatory client certificates** — the
client cert is the wall, and **the only wall** (decided 2026-08-24):
the NSG permits 5432 (and SSH, key-auth only) from anywhere. The
earlier GitHub-Actions-ranges allowlist died on arithmetic —
`api.github.com/meta` lists thousands of CIDRs against an NSG rule
quota in the hundreds, so the "coarse pre-filter" cannot be
expressed, aggregating it until it fits is theater, and a
home-/32-only rule would simply break CI. What an arbitrary IP
reaches is Postgres's TLS handshake rejecting certificate-less
clients; brute force buys nothing against cert auth, and the
Postgres-CVE surface is bounded by the auto-updating minor stream
(§2) plus the re-provision posture — the same appliance logic as
everything else on this box. (A scheduled workflow auto-editing
security rules was already rejected on standing-rent grounds; now
there is nothing for it to edit.)

## 5. Backup

-   A systemd timer (quadlet) runs `pg_dump -Fc`, **age-encrypts**
    the dump — it holds every stack's ciphertext *and* salt — and
    uploads to B2 under the state-backend prefix with a
    **prefix-scoped key holding `writeFiles` alone** — the system's
    one genuinely write-only key: unlike restic, the uploader keeps
    no index to read (storage.md §4). Pruning is not the box's job:
    RPO ≤ 24 h is fine — state is re-derivable from reality
    (`pulumi refresh`/import) at worst.
-   **Retention, explicit: STANDARD class — daily, kept 30 days —
    enforced by a B2 lifecycle rule on the prefix** (Pulumi-managed
    with the bucket, storage.md §4), not by the uploader. That keeps
    the box's key free of delete/prune capability (the H4
    discipline), and it is what gives retired encryption keys a
    definite end of life (below).
-   Freshness is asserted **from outside** by the ops repo's
    scheduled workflow (object-age on the prefix, ci.md §3) — the
    box monitors nothing about itself.
-   **The age identity rotates by generations; no key is assumed
    immortal.** A generation is a *label*, not a stored file: the
    identity is derived from the derivation seed as
    `backup/age/<generation>` (credentials.md §2.2), so rotating one
    means deriving the next — and a retired seed stays in the kit until
    the last dump under it expires. Rotation is a designed path,
    not an emergency improvisation:
    -   **Every dump is encrypted to the two newest generations plus
        the ops-repo-held drill key** — age is natively multi-recipient,
        and all three public keys sit in the Butane file. The
        generational pair makes per-object key attribution
        unnecessary (deploys are intentionally manual, so git dates
        prove nothing about which key an object carries): any object
        in retention decrypts with the current *or* previous key,
        and 30 days after a rotation the current key alone covers
        the entire retention window. The **drill key**
        (operations.md §4, credentials.md) exists so the rebuild
        drill runs unattended; it adds no new *class* of exposure —
        the kluster CI's client cert already reads the live
        database, and the ops repo holding the key is fenced at
        the same private tier (architecture.md §4.3) — while the
        offline generations keep the survive-loss-of-GitHub role. It needs **no
        generational pair of its own**: its contract is decrypting
        the *latest* object only (retention coverage is the offline
        keys' job), so its rotation script swaps the Butane
        recipient, forces a fresh dump, verifies, and destroys the
        old key — one slot, no N−1 bookkeeping.
    -   **Rotate at least yearly** (and on compromise or custody
        change): generate generation N+1 offline, swap the Butane
        recipients `[N, N−1] → [N+1, N]`, re-provision — playbook
        §7.4, via the rotation script (§1). The path stays warm
        because it is the same re-provision as everything else.
    -   **Old keys get a definite end of life**: generation N−1
        becomes destroyable **30 days after the rotation to N+1** —
        every object it can uniquely decrypt has aged out, and
        everything newer also carries key N. The offline register
        records each generation with its rotate date and
        earliest-destroy date; destroying the key on that date is
        what actually ends the old generation's exposure. At most
        two *generational* private keys are ever live — the ops-repo-held
        drill key sits outside the generations. (The offline
        register is §2 of the
        [credential register](../credentials.md), which inventories
        every credential in the system.)
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
-   The ops repo's scheduled workflow (ci.md §3) asserts pg_dump
    freshness
    (object-age on B2) and server-cert expiry (≥30 days), alerting
    into the unified alert channel (architecture.md §4.3). Each alert maps to a playbook: stale
    dump → §7.3's restore path doubles as the diagnosis start; cert
    expiry → §7.1; an unreachable box → §7.3's rebuild is also the
    diagnosis path (there is no NSG allowlist to refresh — §4).
-   **Deliberately unmonitored, with rationale**: Zincati/update
    failures and disk fill. The DB is ~4 orders of magnitude under
    the disk, and OS staleness is bounded by the quarterly
    re-provision drill (§7.3), which always lands the current image.
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
-   **§7.2 Postgres major upgrade.** Trigger: renovate major pin PR.
    Outline: fresh dump → merge → re-provision (new major initdb's) →
    restore → clean `pulumi preview`.
-   **§7.3 Rebuild / DR drill (quarterly, automated in the ops repo).** §7.2
    minus the pin bump: provision a scratch micro from Butane,
    restore the latest age-encrypted B2 object via the **drill key**,
    verify, destroy — unattended, alert on failure (operations.md
    §4). One pass exercises B2 download, decryption,
    provision-from-Butane, restore, and cert delivery. The *offline*
    age identity is proven separately by the yearly rotation (§7.4),
    which inherently decrypts with it.
-   **§7.4 age identity rotation.** Trigger: yearly cadence, key
    compromise, custody change. Outline: generate N+1 offline +
    register entry (rotate / earliest-destroy dates) → swap Butane
    recipients `[N, N−1] → [N+1, N]` → re-provision → verify both
    decrypt paths → destroy N−1 on its date (compromise: drop the
    key from recipients now, fresh dump, early-delete old objects).
