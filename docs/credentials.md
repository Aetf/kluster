# Credential Register

The single place that describes every credential the system runs on:
where it is born, what it may touch, where it is stored, who consumes
it, and when it rotates. It sits at the docs root because credentials
cross every layer boundary (cluster / physical / declarative /
framework); the owning design doc holds each credential's *why*, this
register holds the *inventory*. Values never appear here — only the
facts about them.

## 1. Rules

1.  **Two tiers, and only two.** A credential is either a **seed**
    (§2) — held offline, capable of minting others, touched at
    bring-up and at rotation and never in between — or **derived**
    (§3): minted or computed from a seed by a script, delivered
    straight into its consumer's slot. There is no third category and
    no middle ground: a credential that is neither is a bug.
2.  **The offline store is not a staging area.** Derived credentials
    never land in it, not even temporarily during bring-up. A minted
    value goes from the API response into its slot inside one script
    run; if a stage fails, the fix is to re-run the stage, not to park
    a secret somewhere.
3.  **One credential, one row — same change.** Introducing a
    credential and adding its register row is a single change (the
    alert/playbook rule's sibling).
4.  **Fine-grained scope, stated per row.** Every credential is
    scoped to the minimum its consumer needs; when the platform
    cannot scope that far, the row records the excess it carries.
    Seeds are the deliberate exception — a seed's scope is *large by
    construction* (it must be able to mint its successors), which is
    exactly why nothing consumes a seed at runtime.
5.  **Every seed mints its own successor where the platform allows**
    (the table in §2). Rotation is then a script, not a checklist, and the two
    platforms that cannot do it are named rather than forgotten.
6.  **Storage channels are a closed set.** Offline store (seeds only)
    · Pulumi config secret (provider-credential channel,
    cluster-infra.md §1.1) · SealedSecret (in-cluster consumption) ·
    CI Environment secret (the per-stack GitHub Environments and the
    `drill` Environment, ci.md §2) · ops-repo secret · on-box
    (delivered by provisioning, e.g. Butane-embedded). A row names its
    channel(s); a credential living anywhere else is misplaced.
7.  **Provisioning is scripted.** Minting and distributing a
    credential is an executable procedure — a `credentials`
    subcommand (§4), never a documented sequence of console clicks.
8.  **Boundary**: per-app secrets (OIDC clients, app API keys) follow
    the two-channel rule (cluster-infra.md §1.1) and are enumerated by
    the program itself — out of register scope. The register tracks
    infra and cross-system credentials.

## 2. Seeds

Automation never consumes these; it only *unseals* them, for the
length of one bring-up or one rotation. The set is deliberately tiny —
everything in §3 grows out of it.

| Seed | What it mints | Self-reproducing |
| --- | --- | --- |
| Account roots: OCI tenancy, GitHub, Cloudflare, Backblaze, ZeroTier Central (console access + MFA recovery/bypass codes) | The seeds below, through their consoles | Manual (they *are* the root) |
| OCI seed API key (IAM-manage in tenancy) | The per-stack OCI users and their API keys | **Yes** — IAM creates users and keys, its own included |
| Cloudflare seed token (**API Tokens Write**) | The zone-scoped provider token, the DNS-01 token, the gateway's ACME token | **Yes** — `POST /user/tokens` mints a same-permission successor |
| B2 seed key (`writeKeys`/`deleteKeys` + bucket admin) | The management key and every prefix-scoped writer key | **Yes** — `b2_create_key`. The account's *master* key stays an account root, used only to re-seed |
| GitHub App private keys + **client ids** (**two** single-purpose Apps: dispatch, trigger — permissions are per-App; the JWT's `iss` is the client id, the numeric app id being deprecated for that use) | Installation tokens (8 h, minted per run) | No — key generation is console-only |
| ZeroTier Central API token | Nothing (it *is* the provider credential; ZT has no token API) | No — console-only |
| **Root seed** (32 random bytes) | Every locally-generated secret, by derivation (§2.2) | Generated, not minted (§2.2) |

The two "No" rows are the whole manual surface of a rotation: the
rotation script stops, prints what to create in which console, and
resumes when the new value is handed to it.

### 2.1 The offline kit: storage, backup, succession

-   **Form**: one **kit** in a sealed tamper-evident envelope — a USB
    stick carrying a **dedicated KeePassXC database** (the operator's
    existing tool) plus **paper** carrying that KDBX's master
    password, the few bootstrap facts, and the README. The dedicated
    database is not a copy of anything: it is the **canonical form of
    the seed set** — §2's rows live in it and only in it (key files as
    attachments), and deliberately *not* in the daily-driver personal
    KDBX. Two reasons: an envelope compromise then exposes only infra
    seeds, every one of which has a designed rotate-on-compromise path
    — not the personal estate, which has none — keeping the kit
    locations' security requirements modest enough that an off-site
    copy actually happens; and the repo's succession design stays
    scoped to the system (the README notes that the personal estate is
    arranged separately). The principle stands — a kit only the
    operator can decrypt fails succession by construction — and the
    paper satisfies it: the master password in the envelope opens the
    database for whoever holds the kit. Confidentiality still comes
    from physical custody; the KDBX layer adds one real property on
    top — a USB stick lost or copied *on its own* discloses nothing.
-   **Copies: two.** One at home, one off-site at a friend's (the
    exact locations are a register note, not repo content). Losing the
    home to fire must not lose the recovery root (the same reasoning
    that keeps backups off the OCI tenancy). The master copy of the
    database lives on the operator workstation, where the
    `credentials` scripts read and write it; re-issuing the kit is a
    copy onto both sticks.
-   **Contents = §2's rows plus a printed README**: the recovery entry
    points (this repo's URL, this document, the reverse-cold-standby
    runbook, the account list) written for a **technical reader who
    has never seen this system**. The account-root rows are what
    bootstrap everything else: GitHub gets the repos, the registrar
    gets the domains.
-   **Opened twice in a system's life**: at bring-up (§4.1) and at
    rotation (§4.2) — plus the yearly offline day, which opens one kit
    and verifies it against §2's table. It is emphatically **not** a
    day-2 operations database: no runbook outside those three asks for
    it, because no runtime credential is in it.
-   **Refresh discipline**: rotation writes a **new database file**
    (§4.2), and re-issuing the kit is copying that file onto both
    sticks (paper reprints only when the master password changed).
-   **Succession**: the successor (**Miu**) knows the kit locations
    and that this README exists — that is the entire protocol. The
    offline-day check includes re-reading the README with fresh eyes:
    instructions rot faster than keys.

### 2.2 The root seed: one secret behind every generated key

Some secrets are not minted by any provider — a passphrase, a CA key,
a backup encryption key. Storing each one would turn the kit back into
a growing token drawer, so instead **one 32-byte root seed** is stored
and each secret is **derived from it** with HKDF-SHA256 under a stable
label:

| Label | Derived secret |
| --- | --- |
| `pulumi/passphrase` | Pulumi state passphrase |
| `state-backend/ca` | State-backend CA private key |
| `state-backend/cert/<name>` | Server and client key material under that CA |
| `backup/age/<generation>` | age identity for pg_dump encryption |
| `restic/<namespace>/<pvc>` | That volume's restic repository password |

Consequences, all deliberate:

-   **Nothing per-volume is stored anywhere.** A new `backed_pvc`
    invents no secret: its repo password is a pure function of its
    identity, so the program can re-derive any repository's password
    during a restore without a lookup.
-   **Asymmetric keys are derived, so their algorithms are
    constrained**: X25519 for age and **P-256 for the state-backend
    PKI** (a private scalar is a deterministic function of the seed).
    RSA is excluded — deterministic RSA keygen is a footgun, not a
    feature.
-   **The sealed-secrets sealing key is not a recovery root.** It is
    the controller's own generated RSA key; losing it costs a re-seal,
    not data, because every sealed value is itself derived or
    re-mintable. Re-sealing is a script, not an archaeology project —
    which is why no offline export of it exists.
-   **A retired root seed outlives its rotation.** Rotating the root
    re-derives everything going forward but cannot retroactively
    re-encrypt existing backups, so the previous seed stays in the kit
    (marked with its earliest-destroy date) until the last backup
    encrypted under it has expired.

## 3. Derived credentials

Everything here is minted or derived by a `credentials` subcommand and
delivered straight to the slot named in its row. None of it is stored
offline; none of it is copied by hand.

| Credential | From | Scope | Slot | Consumer |
| --- | --- | --- | --- | --- |
| OCI API key (per stack) | OCI seed key | `physical` user/compartment | Pulumi config secret + CI env | `physical` |
| Cloudflare token (zones) | CF seed token | DNS edit, estate zones only | Pulumi config secret + CI env | `dns`, `apps` |
| Cloudflare token (DNS-01) | CF seed token | `_acme-challenge` edit only | SealedSecret | cert-manager |
| Cloudflare token (gateway ACME) | CF seed token | zone-scoped, gateway's own issuance | gw-config device secret | UDM caddy |
| B2 management key | B2 seed key | Bucket/key/lifecycle admin, **no file capabilities** | Pulumi config secret + CI env | `physical` |
| B2 writer keys | B2 seed key (via `physical`) | Prefix-scoped, `list+read+write`, **no `deleteFiles`** — deletes degrade to lifecycle-purged hides (audit H4): VolSync, CNPG barman, etcd snapshots | SealedSecret · ops-repo secret · on-box | restic/barman, ops-repo workflow, micro cron |
| B2 dump key (micro) | B2 seed key | `writeFiles` alone, dump prefix | on-box (Ignition) | state-backend pg_dump timer |
| GitHub installation tokens | The two App private keys | Per-run, 8 h; dispatch App = contents:write on `kluster-ops`, trigger App = actions:write on `kluster` | never stored — minted in-run | Alert producer step, weekly drift trigger |
| ZT CI member identities (`ci-deploy`, `ci-preview`) | generated in-state (`zerotier_identity`) | One per concurrency domain, `ci`-tagged and flow-rule-confined (gateway.md §2.3) | CI env | CI per-run join |
| Pulumi state passphrase | root seed | Decrypts state secrets | CI env (all stacks) | every `pulumi` run |
| State-backend CA + certs (`ci`, `operator`) | root seed | postgres:// mTLS | on-box (server) · CI env · operator machine | Pulumi state access |
| age backup identity | root seed | Decrypts state-backend pg_dumps | on-box (public half) · ops-repo `drill` Environment (private half, latest-dump-only) | micro cron, drill workflow |
| restic repo passwords | root seed | Per-PVC repos | SealedSecret (via `backed_pvc`) | VolSync |
| Talos machine secrets + talosconfig | generated by `physical` | Cluster PKI roots | Pulumi state · CI env · ops-repo secret | Talos ops, etcd snapshot workflow |
| kubeconfig | `physical` output | cluster-admin | CI env | `k8s-base`, `apps` |
| UDM SSH key, libvirt SSH identity | generated | gw-config push (host key pinned) / virsh only | Pulumi config secret + CI env | `physical` |
| UniFi API key | Dedicated local admin | Network API | Pulumi config secret + CI env | `physical` |
| AdGuard API credentials | AdGuard admin (no scoped API — audit L11) | alice/bob rewrite API | Pulumi config secret + CI env | `apps` rewrites |
| Alertmanager read token | root seed (`alertmanager/read`) | `GET /api/v2/alerts` only, by HTTPRoute method+path+header match | ops-repo secret · HTTPRoute spec (Pulumi config secret at render) | Issue-sync poller |
| HA webhook URL/ID | Home Assistant | One notify endpoint | SealedSecret · ops-repo secret | alertmanager, dispatch handler |
| Drill-environment credentials | OCI seed key, B2 seed key | Drill compartment; dump-prefix read-only | ops-repo `drill` Environment | Drill workflows |

Rows whose "From" is a seed rotate by re-running their subcommand.
Rows generated by a stack (Talos secrets, kubeconfig, ZT identities)
rotate with the resource that owns them.

## 4. The scripts

The register's executable form: `credentials`, a console script in
this repo (`src/kluster/scripts/credentials/`), one subcommand per
credential family plus the two lifecycle commands below. Every
subcommand is **mint → push to every slot in the map → verify**, and
therefore idempotent: rotation is a re-run, not a second procedure.

-   **A slot map, checked in.** A declarative manifest maps each §3 row
    to its target slots: GitHub Environment secret (repo + environment
    + name), ops-repo secret, Pulumi config secret (per stack),
    SealedSecret (kubeseal → committed manifest path), on-box
    (rendered into Butane). §3 is the human-readable view; the slot
    map is the machine-readable one, and the two are checked against
    each other.
-   **Slot-drift probe**: an ops-repo scheduled workflow compares the
    slot map against reality in both directions — `gh` secret listings
    and `pulumi config` keys. A live slot with no map entry, or a map
    entry with no live slot, raises an `actionable` alert. This (plus
    the expiry/destroy-date tripwires, operations.md §4) replaces the
    calendar register-review.

### 4.1 `credentials bringup`

One command, one master-password prompt, and the cluster's entire
credential estate exists. Stages run in dependency order, each pushing
into slots that exist by the time it runs:

1.  **Local derivations** — passphrase, state-backend CA and certs,
    age identity: derived from the root seed (§2.2), no network.
2.  **State backend** — the micro's Ignition carries the server cert
    and the B2 dump key; the `ci`/`operator` client certs go to their
    slots. The backend must exist before any stack config does.
3.  **Provider credentials** — OCI per-stack keys, Cloudflare tokens,
    B2 management key: minted from their seeds into Pulumi config
    secrets and GitHub Environment secrets.
4.  **Device credentials** — UDM SSH key, libvirt identity, UniFi API
    key, AdGuard credentials.
5.  **In-cluster secrets** — only after `k8s-base` brings the
    sealed-secrets controller up: DNS-01 token, restic passwords,
    writer keys, sealed and committed.

A stage that fails is re-run; nothing is parked. Once the last stage
verifies, the kit goes back in its envelope.

### 4.2 `credentials rotate`

`--family <name>` re-runs one family; `--all` rotates the whole seed
set and **writes a new database file**: unseal the old, have each
seed mint its successor, derive a new root seed, write the new
database, verify every slot against it, then record the old file's
earliest-destroy date (§2.2's backup-retention rule). The GitHub App
private key and the ZeroTier token are explicit pauses — the script
prints the console steps and waits.

Rotation cadence lives with the drill program (operations.md §4); the
yearly offline day verifies the current kit against §2.
