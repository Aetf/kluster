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
    cluster-infra.md §1.1) · **Pulumi state** · SealedSecret
    (in-cluster consumption) · CI Environment secret (the per-stack
    GitHub Environments and the `drill` Environment, ci.md §2) ·
    ops-repo secret · on-box (delivered by provisioning, e.g.
    Butane-embedded). A row names its channel(s); a credential living
    anywhere else is misplaced.

    The two Pulumi channels are separate rows because their exposure
    is opposite. **Config secret** lives in `Pulumi.<stack>.yaml` and
    is committed: its ciphertext is public the moment the repo is, so
    it carries only what must exist *before* a program can run —
    provider credentials. **State** lives in the state backend's
    Postgres and never enters git, which makes it the stronger of the
    two and the right home for what a program *generates* (Talos
    machine secrets, ZeroTier identities, restic repository
    passwords). One passphrase protects both, and that passphrase is
    derived from the derivation seed — so either channel opens from the kit
    and from nothing else.
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

**Account roots are not in this set.** Console logins and their MFA
recovery codes for the five provider accounts (OCI tenancy, GitHub,
Cloudflare, Backblaze, ZeroTier Central) are a *precondition* of the
system rather than a credential it manages: nothing scripted reads
them, and every seed below is minted through a console exactly once
and self-reproduces from then on. They live in the operator's personal
estate — its own database, its own succession — and the seed kit
borrows them at two moments only: first bring-up, and re-seeding after
a total seed loss. The GitHub admin token the `github` stack applies
with (framework/github.md §1) belongs to this class rather than to
either tier below: it is an account root used from the workstation,
never minted from a seed and never pushed to a slot, which is exactly
why that stack is not something CI runs.

Three of them are nonetheless *used* by a script, because the seeds
they mint cannot be minted by anything smaller: an OCI API key
belonging to a user who may manage users, groups and policies in the
tenancy; a Cloudflare token carrying **User → API Tokens → Edit**, the
one permission that mints a token through the API; and the B2 account
master key, which re-seeds B2 after a total loss. **Each is handed
over through the desktop secret store, one credential at a time.**
`credentials master <root> remember` prompts for it and stores it
there; every later use — `bootstrap`, a seed's `create`, a re-seed —
looks it up and falls back to a prompt when the machine has no secret
store or has not been told this one. Neither the estate database nor
its master password is ever opened by anything in this repository, and
no account root reaches a file, an environment variable or a shell
history. `credentials master ls` says which roots are stored, without
printing a value.

Keeping them out is what makes §2.1's argument true rather than
aspirational: every row below has a designed
rotate-on-compromise path, so a compromised kit is answered by one
full rotation, while an account root has no such path and would leave
that rotation incomplete.

| Seed | What it mints | Self-reproducing |
| --- | --- | --- |
| OCI seed API key (its own user, group and policy: manage users, groups and policies in the tenancy) | The per-stack OCI users and their API keys | **Yes** — IAM creates users and keys, its own included |
| Cloudflare seed token (**API Tokens Write**) | The zone-scoped provider token, the DNS-01 token, the gateway's ACME token | **Yes** — `POST /user/tokens` mints a successor with the minting token's own policies |
| B2 seed key (`writeKeys`/`deleteKeys` + bucket admin) | The management key and every prefix-scoped writer key | **Yes** — `b2_create_key`. The account's *master* key is an account root and lives in the personal estate, borrowed only to re-seed |
| GitHub App private keys + **client ids** (**two** single-purpose Apps: dispatch, trigger — permissions are per-App; the JWT's `iss` is the client id, the numeric app id being deprecated for that use) | Installation tokens (8 h, minted per run) | No — key generation is console-only |
| ZeroTier Central API token | Nothing (it *is* the provider credential; ZT has no token API) | No — console-only |
| **Derivation seed** (32 random bytes) | Every locally-generated secret, by derivation (§2.2) | Generated, not minted (§2.2) |

The two "No" rows are the whole manual surface of a rotation: the
rotation script stops, prints what to create in which console, and
resumes when the new value is handed to it. Both of those pauses need
an account login, which is the one thing the kit deliberately does not
carry — so a rotation run by a successor starts in the personal estate
(§2.1), and the kit's README says so.

### 2.1 The offline kit: storage, backup, succession

-   **Form**: one **kit** in a sealed tamper-evident envelope — a USB
    stick carrying a **dedicated KeePassXC database** (the operator's
    existing tool) plus **paper** carrying that KDBX's master
    password, the few bootstrap facts, and the README. The dedicated
    database is not a copy of anything: it is the **canonical form of
    the seed set** — §2's rows live in it and only in it (key files as
    attachments), and nothing else does. The split from the
    daily-driver personal KDBX runs both ways and is the point: the
    kit holds only credentials a single rotation can replace, so an
    envelope compromise is answered by running that rotation, and the
    kit locations' security requirements stay modest enough that an
    off-site copy actually happens. Account roots stay in the personal
    estate for the mirrored reason — they cannot be rotated, and the
    repo's succession design stays scoped to the system. The principle
    stands — a kit only the
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
-   **Row shape**: `seeds/<name>`, one group deep. `UserName` holds the
    credential's public identifier — the half that appears in a console
    and is not a secret — and `Password` holds the secret, and nothing
    else does. Key material that is a *file* is an attachment: the two
    GitHub App PEM files, and the OCI API key. The OCI row is the one
    that needs more than those two fields, an API key there being five
    things: `UserName` is the user `OCID`, the PEM is the attachment,
    and the **tenancy `OCID` is a protected custom attribute** — the
    same protection class as the password field, because an `OCID` is
    an account identifier and a listing has no reason to hand it out. The
    remaining two are recovered rather than stored: the region is a
    constant in the code, and the fingerprint is a function of the
    public key, so a stored copy could only ever disagree with the key
    it describes.
-   **Contents = §2's rows plus a printed README**: the recovery entry
    points (this repo's URL, this document, the reverse-cold-standby
    runbook) written for a **technical reader who has never seen this
    system**. Because the account roots are not in the kit, the README
    carries the one pointer that keeps succession unbroken: which
    provider accounts exist, that their credentials live in the
    personal estate, and that the estate's own succession is arranged
    separately. Without it a successor can open every seed and still
    not reach the two console-only rotations, or re-seed after a total
    loss.
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

### 2.2 The derivation seed: one secret behind every generated key

Some secrets are not minted by any provider — a passphrase, a CA key,
a backup encryption key. Storing each one would turn the kit back into
a growing token drawer, so instead **one 32-byte derivation seed** is
stored, and each secret is **derived from it** with HKDF-SHA256 under a
stable label:

| Label | Derived secret |
| --- | --- |
| `pulumi/passphrase` | Pulumi state passphrase |
| `state-backend/ca` | State-backend CA private key |
| `state-backend/cert/<name>` | Server and client key material under that CA |
| `backup/age/<generation>` | age identity for pg_dump encryption |

Consequences, all deliberate:

-   **Every label here is consumed offline.** The four derivations
    happen during bring-up, rotation, or provisioning; no running
    program holds the derivation seed, which is what keeps §1's rule 4
    ("nothing consumes a seed at runtime") true rather than aspirational.
    Secrets a *program* generates — restic repository passwords among
    them — belong in Pulumi state (rule 6), not on this table: state
    already stores them, so deriving them would buy nothing and cost a
    seed on the runtime path.
-   **State is therefore a data-safety dependency, not a
    convenience.** A restic repository's password exists only in state
    and in the SealedSecret rendered from it. Lose both the backend and
    its dumps and the B2 backups survive with nothing to open them —
    which is why the recovery chain (kit → derivation seed → age identity →
    the dump in B2 → state) is drilled rather than assumed
    (operations.md §4).
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
-   **A retired derivation seed outlives its rotation.** Rotating it
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
| Pulumi state passphrase | derivation seed | Decrypts state secrets | CI env (all stacks) | every `pulumi` run |
| State-backend CA + certs (`ci`, `operator`) | derivation seed | postgres:// mTLS | on-box (server) · CI env · operator machine | Pulumi state access |
| age backup identity | derivation seed | Decrypts state-backend pg_dumps | on-box (public half) · ops-repo `drill` Environment (private half, latest-dump-only) | micro cron, drill workflow |
| restic repo passwords | generated in-state (`backed_pvc`) | Per-PVC repos | Pulumi state · SealedSecret (via `backed_pvc`) | VolSync |
| Talos machine secrets + talosconfig | generated by `physical` | Cluster PKI roots | Pulumi state · CI env · ops-repo secret | Talos ops, etcd snapshot workflow |
| kubeconfig | `physical` output | cluster-admin | CI env | `k8s-base`, `apps` |
| UDM SSH key, libvirt SSH identity | generated | gw-config push (host key pinned) / virsh only | Pulumi config secret + CI env | `physical` |
| UniFi API key | Dedicated local admin | Network API | Pulumi config secret + CI env | `physical` |
| AdGuard API credentials | AdGuard admin (no scoped API — audit L11) | alice/bob rewrite API | Pulumi config secret + CI env | `apps` rewrites |
| Alertmanager read token | derivation seed (`alertmanager/read`) | `GET /api/v2/alerts` only, by HTTPRoute method+path+header match | ops-repo secret · HTTPRoute spec (Pulumi config secret at render) | Issue-sync poller |
| HA webhook URL/ID | Home Assistant | One notify endpoint | SealedSecret · ops-repo secret | alertmanager, dispatch handler |
| Drill-environment credentials | OCI seed key, B2 seed key | Drill compartment; dump-prefix read-only | ops-repo `drill` Environment | Drill workflows |

Rows whose "From" is a seed rotate by re-running their subcommand.
Rows generated by a stack (Talos secrets, kubeconfig, ZT identities)
rotate with the resource that owns them.

## 4. The scripts

The register's executable form: `credentials`, a console script in
this repo (`src/kluster/scripts/credentials/`), one subcommand per
credential family plus the lifecycle commands below.

| Command | When |
| --- | --- |
| `credentials master <root> remember` | Once per machine and root, before a bring-up or a re-seed that needs it. Stores one account root (§2) in the desktop secret store. Skipping it costs a prompt, not a failure. |
| `credentials master ls` | Which roots the store holds. Prints no values. |
| `credentials master <root> forget` | Removes one root from the store again. |
| `credentials bootstrap` | Bring-up, from nothing or from a partial kit. Resumable: re-running skips what is already there. |
| `credentials bootstrap --only <member>` | One seed was lost. Re-creates that row alone. |
| `state-backend provision` | After the kit exists; every stack needs the backend before it can act. |
| `eval "$(credentials derive env)"` | Whenever a shell needs to reach the backend. Derives the passphrase, reads the URL from the bundle. |
| `credentials derive passphrase > .pulumi.secret` | Once per workstation that develops without the kit. Caches the passphrase for `mise.toml` to read, so a local preview does not need the offline database open. |
| `credentials rotate --into <new kit>` | Rotation (§4.2). Writes a new database; the retired one stays. |
| `credentials kdbx ls` / `show` | Looking without changing. |
| `credentials kdbx remember` | Once per machine, so a run opening two databases asks for nothing. |

`credentials --help` carries the same ordering, because a command list
shaped like the register answers neither "where do I start" nor "which
of these destroys something". Every
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
credential estate exists. **One database is opened: the kit.** The
account roots the three minters borrow are not in a database this
repository reads — they come from the **desktop secret store**, one
credential at a time (§2), or from a prompt where there is no store.
The kit's own master password is asked of the same store first
(`credentials kdbx remember` puts it there), because a run that then
goes on for minutes should not be guarded by a password typed into a
process nobody is watching. Nothing is ever written to the store
implicitly: a `remember` command is the only thing that puts a value
there.

Stages run in dependency order, each pushing
into slots that exist by the time it runs:

1.  **Local derivations** — passphrase, state-backend CA and certs,
    age identity: derived from the derivation seed (§2.2), no network.
2.  **State backend** — the micro's Ignition carries the server cert
    and the B2 dump key; the `ci`/`operator` client certs go to their
    slots. The backend must exist before any stack config does.
3.  **Provider credentials** — OCI per-stack keys, Cloudflare tokens,
    B2 management key: minted from their seeds into Pulumi config
    secrets and GitHub Environment secrets.
4.  **Device credentials** — UDM SSH key, libvirt identity, UniFi API
    key, AdGuard credentials.
5.  **In-cluster secrets** — only after `k8s-base` brings the
    sealed-secrets controller up: DNS-01 token, writer keys, sealed
    and committed. Restic passwords are not here: `backed_pvc`
    generates its own into state and seals it, so a new volume needs
    no credentials run (rule 6).

A stage that fails is re-run; nothing is parked. Once the last stage
verifies, the kit goes back in its envelope.

**Resumable by probing, not by bookkeeping.** Each stage asks whether
its output exists and skips if it does, so an interrupted bootstrap is
resumed by re-running the same command, and `--only <member>` is the
repair path when a single seed is lost. A checkpoint file would record
"this ran" — which stops being true the moment someone deletes a key in
a console, and the run after that would skip the repair.

**The console steps live in the register, not in a runbook.** Each §2
row that no API can create carries the instructions for creating it
(`entries.py`), so `bootstrap` prints them at the moment it stops
rather than sending the operator to look for a document.

### 4.2 `credentials rotate`

`--only <member>` re-runs one row; the default rotates the whole seed
set. It **writes a new database file** (`--into`), and the retired one
is left byte-for-byte as it was: unseal the old, have each
seed mint its successor, derive a new derivation seed, write the new
database, verify every slot against it, then record the old file's
earliest-destroy date (§2.2's backup-retention rule). The GitHub App
private key and the ZeroTier token are explicit pauses — the script
prints the console steps and waits.

Rotation cadence lives with the drill program (operations.md §4); the
yearly offline day verifies the current kit against §2.
