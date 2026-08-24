# Credential Register

The single place that describes every credential the system runs on:
where it is born, what it may touch, where it is stored, who consumes
it, and when it rotates. It sits at the docs root because credentials
cross every layer boundary (cluster / physical / declarative /
framework); the owning design doc holds each credential's *why*, this
register holds the *inventory*. Values never appear here — only the
facts about them.

## 1. Rules

1.  **One credential, one row — same change.** Introducing a
    credential and adding its register row is a single change (the
    alert/playbook rule's sibling). A credential without a row is a
    bug.
2.  **Fine-grained scope, stated per row.** Every credential is
    scoped to the minimum its consumer needs; when the platform
    cannot scope that far, the row records the excess it carries
    (pattern: the dispatch PAT's contents:write, architecture.md
    §4.3).
3.  **Storage channels are a closed set.** Offline store · Pulumi
    config secret (provider-credential channel, cluster-infra.md
    §1.1) · SealedSecret (in-cluster consumption) · CI Environment
    secret (the per-stack GitHub Environments and the `drill`
    Environment, ci.md §2) · alerts-repo Actions secret · on-box
    (delivered by provisioning, e.g. Butane-embedded). A row names
    its channel(s);
    a credential living anywhere else is misplaced.
4.  **Every key rotates or expires.** Each row carries a cadence or
    expiry and the reminder mechanism (platform e-mail, the ci.md §3
    expiry probe, or the annual drill); a retired key gets an
    earliest-destroy date (the age-key precedent, state-backend.md
    §5, generalized).
5.  **Provisioning is scripted.** Minting and distributing a
    credential is an executable procedure (the state-backend
    everything-is-a-script rule, generalized); each row references
    it once implementations land.
6.  **Boundary**: per-app secrets (OIDC clients, app API keys)
    follow the two-channel rule (cluster-infra.md §1.1) and are
    enumerated by the program itself — they are out of register
    scope. The register tracks infra and cross-system credentials.

## 2. Offline tier (never-in-automation)

Automation never sees these. Compromise response and destroy
discipline live with the owning doc; how they are physically held,
backed up, and inherited is §2.1.

### 2.1 The offline kit: storage, backup, succession

-   **Form**: one **kit** in a sealed tamper-evident envelope — a
    USB stick carrying a **dedicated KeePassXC database** (the
    operator's existing tool) plus **paper** carrying that KDBX's
    master password, the few bootstrap secrets, and the README.
    The dedicated database is not a copy of anything: it is the
    **canonical form of "the operator's offline store"** — §2's
    rows live in it and only in it (key files as attachments), and
    deliberately *not* in the daily-driver personal KDBX. Two
    reasons: an envelope compromise then exposes only infra keys,
    every one of which has a designed rotate-on-compromise path —
    not the personal estate, which has none — keeping the kit
    locations' security requirements modest enough that an off-site
    copy actually happens; and the repo's succession design stays
    scoped to the system (the README notes that the personal estate
    is arranged separately). The principle stands — a kit only the
    operator can decrypt fails succession by construction — and the
    paper satisfies it: the master password in the envelope opens
    the database for whoever holds the kit. Confidentiality still
    comes from physical custody; the KDBX layer adds one real
    property on top — a USB stick lost or copied *on its own*
    discloses nothing.
-   **Copies: two.** One at home, one off-site (locations are an
    implementation-time blank — a register note, not repo content).
    Losing the home to fire must not lose the recovery root
    (the same reasoning that keeps backups off the OCI tenancy).
-   **Contents = §2's rows plus a printed README**: the recovery
    entry points (this repo's URL, this document, the
    reverse-cold-standby runbook, the account list) written for a
    **technical reader who has never seen this system** — the README
    is what turns a bag of keys into something a successor can
    actually use. The account-root rows (MFA recovery codes) are
    what bootstrap everything else: GitHub gets the repos, the
    registrar gets the domains.
-   **Refresh discipline**: a rotation playbook's "update the
    offline store" step writes the dedicated KDBX — the single
    canonical location — and re-issuing the kit is **re-copying it
    to both USB sticks** (paper reprints only when the master
    password or a paper-held secret changed). The database's write
    events *are* the rotation events, so kit freshness rides the
    rotation playbooks with no separate upkeep. The **yearly offline day
    (operations.md §4) opens one kit and verifies it against §2's
    table** — a stale kit is a failed drill, same as any other.
-   **Succession**: the successor (an implementation-time blank)
    knows the kit locations and that this README exists — that is
    the entire protocol; no shared passphrases, no ceremony. The
    offline-day check includes re-reading the README with fresh
    eyes: instructions rot faster than keys.

| Credential | Purpose | Rotation / destroy |
| --- | --- | --- |
| State-backend CA private key | Signs server/client certs (state-backend.md §3) | ~10 y; compromise = regenerate + reissue all |
| age backup private keys (gen N, N−1) | Decrypt state-backend pg_dumps | Yearly generation; destroy retired key at rotation + 30 d |
| Pulumi state passphrase (escrow copy) | Recover state secrets if CI copy is lost | Rotate on compromise; working copy is a CI secret (§3) |
| sealed-secrets sealing key (offline export) | Recover every SealedSecret if the cluster is lost before a rebuild restores the controller | Re-export at Wave F's rotation and on any later rotation (export/restore runbook); working copy lives in-cluster (§3) |
| Account roots: OCI tenancy, GitHub, Cloudflare, Backblaze, ZeroTier Central | Console/owner access + MFA recovery | Per-platform hygiene; MFA recovery codes stored alongside |

## 3. Automation tier

| Credential | Scope | Lives in | Consumer | Rotation |
| --- | --- | --- | --- | --- |
| OCI API key | physical-stack user/compartment | Pulumi config secret + CI env | `physical` | Yearly; scripted re-mint |
| Cloudflare token (zones) | DNS edit, estate zones only | Pulumi config secret + CI env | `dns`, `apps` (CNAMEs) | Yearly |
| Cloudflare token (DNS-01) | `_acme-challenge` edit only | SealedSecret | cert-manager | Yearly |
| B2 key (management) | Bucket/key/lifecycle admin | Pulumi config secret + CI env | `physical` | Yearly |
| B2 keys (writers) | Prefix-scoped, `list+read+write`, **no `deleteFiles`** — deletes degrade to lifecycle-purged hides (audit H4, storage.md §4): VolSync, CNPG barman, etcd snapshots; the micro pg_dump key is `writeFiles` alone | SealedSecret (in-cluster) · CI env (etcd) · on-box (micro) | restic/barman, ci.md §3, micro cron | Yearly; scripted |
| UDM SSH key | gw-config push (host key pinned) | Pulumi config secret + CI env | `physical` | Yearly |
| UniFi API key | Dedicated local admin, Network API | Pulumi config secret + CI env | `physical` | Yearly |
| ZeroTier Central API token | The one network | Pulumi config secret + CI env | `physical` | Yearly |
| ZT CI member identities (`ci-deploy`, `ci-preview`) | One per concurrency domain (gateway.md §2.6), both `ci`-tagged and flow-rule-confined (§2.3) | CI env (generated in-state; both in `apps`, `ci-deploy` also in `physical-plan`/`physical`) | CI per-run join | With flow-rule changes or yearly |
| GitHub dispatch PAT | `kluster-alerts` contents:write (excess: can push there — accepted, architecture.md §4.3) | CI env | Alert producer step | GitHub expiry + reminder e-mail |
| HA webhook URL/ID | One notify endpoint | SealedSecret (alertmanager) · alerts-repo Actions secret | alertmanager; alerts-repo dispatch handler | On exposure; low value alone |
| Alertmanager read token | Read-only alert list at the gateway route | alerts-repo Actions secret | Issue-sync poller | Yearly |
| State-backend client certs (`ci`, `operator`) | postgres:// mTLS | CI env · operator machine | Pulumi state access | 2–3 y; ci.md §3 expiry probe |
| age drill key | Decrypt the **latest** pg_dump for the automated rebuild drill only (no new exposure: CI's client cert already reads the live DB) | `drill` Environment secret — **one slot**: its contract is latest-dump-only, so rotation forces a fresh dump then destroys the old key, no N−1 bookkeeping (state-backend.md §5) | Drill workflow (operations.md §4) | Yearly, independent of the offline generations |
| Drill-environment credentials (OCI drill-compartment user, B2 dump-prefix read-only key) | Create/destroy in the drill compartment; read dumps — nothing else | `drill` Environment secrets | Drill workflows | Yearly |
| Pulumi state passphrase | Decrypts state secrets | CI env (all stacks) | every `pulumi` run | Rotate on compromise; offline escrow (§2) |
| Talos machine secrets + talosconfig | Cluster PKI roots | Pulumi state (physical) · CI env (talosconfig for etcd snapshots) | Talos ops, ci.md §3 | Cluster lifetime; regenerate = rebuild |
| kubeconfig | cluster-admin | physical output → CI env | `k8s-base`, `apps` | With cluster CA |
| sealed-secrets sealing key | Decrypts every SealedSecret | In-cluster + offline export (playbook) | controller | Restored from legacy → rotated at Wave F, then on compromise |
| libvirt SSH identity | Homelab host, virsh only | Pulumi config secret + CI env | `physical` (worker VM) | Yearly |
| AdGuard API credentials | alice/bob rewrite API | Pulumi config secret + CI env | `apps` rewrites | Yearly |
| restic repo passwords | Per-PVC VolSync repos | SealedSecret (via `backed_pvc` helper) | VolSync | Stable; loss = repo loss, escrow with backups design |

## 4. Provisioning & distribution (`deploy/credentials/`)

The register's executable form — rule 5 made concrete:

-   **A slot map, checked in.** A declarative manifest in
    `deploy/credentials/` maps each register row to its target
    slots: GitHub Environment secret (repo + environment + name, set
    via `gh secret set --env`), alerts-repo Actions secret
    (`gh secret set -R`), Pulumi config secret
    (`pulumi config set --secret` per stack), SealedSecret
    (kubeseal → committed manifest path). The table above is the
    human-readable view; the slot map is the machine-readable one,
    and the two are checked against each other (below).
-   **One script per credential family**: mint → push to every slot
    in the map → verify (re-read slot metadata, or fire the
    consumer's probe). Idempotent, so **rotation playbooks call the
    same script** — rotation is a re-run, not a second procedure.
-   **Slot-drift probe**: a scheduled check compares the slot map
    against reality in both directions — `gh` secret listings and
    `pulumi config` keys. A live slot with no map entry, or a map
    entry with no live slot, raises an `actionable` alert. This
    (plus the expiry/destroy-date tripwires, operations.md §4)
    replaces the calendar register-review: a document nobody
    re-reads is how key sprawl happened last time.
-   **The `drill` Environment**: unattended drills need cloud
    credentials, but the `physical` Environment's required-reviewer
    gate must not be the thing a quarterly automation waits on — so
    drill workflows run in a dedicated Environment whose credentials
    are scoped to what a drill may touch (the OCI **drill
    compartment**, a dump-prefix **read-only** B2 key, the drill age
    key) and nothing else. No reviewer gate; the scope is the gate.
-   **Bootstrap order awareness**: "Pulumi config secret" rows exist
    before their stack's first `up`; GitHub slots need the repos to
    exist; SealedSecrets exist only after k8s-base restores the
    sealing key (migration.md).
