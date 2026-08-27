# Credential Register

The single place that describes every credential the system runs on:
where it is born, what it may touch, where it is stored, who consumes
it, and when it rotates. It sits at the docs root because credentials
cross every layer boundary (cluster / physical / declarative /
framework); the owning design doc holds each credential's *why*, this
register holds the *inventory*. Values never appear here — only the
facts about them.

## 1. Rules

1.  **Two tiers, and only two.** A credential is either **kit-held**
    (§2) — offline, touched at bring-up and at rotation and never in
    between, either a seed capable of minting others or the recovery
    key that opens what §2.2 escrows — or **derived** (§3): minted
    from a seed, or generated at random and escrowed, and either way
    delivered straight into its consumer's slot by a script. There is
    no third category and no middle ground: a credential that is
    neither is a bug.
2.  **The offline store is not a staging area.** Derived credentials
    never land in it, not even temporarily during bring-up. A minted
    value goes from the API response into its slot inside one script
    run; if a stage fails, the fix is to re-run the stage, not to park
    a secret somewhere. Escrow (§2.2) is not a staging area either: a
    generated secret reaches its slot in the same run that commits its
    ciphertext, and that ciphertext is a recovery copy nothing reads
    at runtime.
3.  **One credential, one row — same change.** Introducing a
    credential and adding its register row is a single change (the
    alert/playbook rule's sibling).
4.  **Fine-grained scope, stated per row.** Every credential is
    scoped to the minimum its consumer needs; when the platform
    cannot scope that far, the row records the excess it carries.
    The kit rows are the deliberate exception — a seed's scope is
    *large by construction* (it must be able to mint its successors)
    and the recovery key's is larger still (it opens every escrowed
    label at once), which is exactly why nothing consumes either at
    runtime.
5.  **Every seed mints its own successor where the platform allows**
    (the table in §2). Rotation is then a script, not a checklist, and the
    platforms that cannot do it are named rather than forgotten.
6.  **Storage channels are a closed set.** Offline store (the kit's
    own rows, §2) · **escrow** (§2.2 — a committed ciphertext of a
    generated secret, opened only from the kit) · Pulumi config
    secret (provider-credential channel,
    cluster-infra.md §1.1) · **Pulumi state** · SealedSecret
    (in-cluster consumption) · CI Environment secret (the per-stack
    GitHub Environments and the `drill` Environment, ci.md §2) ·
    ops-repo secret · **workstation slot** · on-box (delivered by
    provisioning, e.g. Butane-embedded). A row names its channel(s); a
    credential living anywhere else is misplaced.

    A **workstation slot** is the local half of a credential: a file
    under the checkout's git-ignored `.credentials/` (§4.4), written by
    a `credentials` or `state-backend` command and read
    non-interactively afterward — by `mise.toml` building a `pulumi`
    run's environment, or by a script that must not stop to ask. It is
    the channel for what CI holds as an Environment secret and a
    workstation needs anyway: the Pulumi passphrase, the state
    backend's `operator` bundle, the `github` stack's admin token.
    Deliberately not the desktop secret store, which is where account
    roots go (§2) — a root is interactive and rare, so a store that
    asks a session to unlock suits it, while these are read on *every*
    `pulumi` run by a template that can neither prompt nor unlock a
    keyring. A slot holds only what a command can write again — the
    passphrase is recovered from escrow, the bundle re-issued, the
    token re-pasted — so losing one costs a command and never a
    credential.

    The two Pulumi channels are separate rows because their exposure
    is opposite. **Config secret** lives in `Pulumi.<stack>.yaml` and
    is committed: its ciphertext is public the moment the repo is, so
    it carries only what must exist *before* a program can run —
    provider credentials. **State** lives in the state backend's
    Postgres and never enters git, which makes it the stronger of the
    two and the right home for what a program *generates* (Talos
    machine secrets, ZeroTier identities, restic repository
    passwords). One passphrase protects both, and that passphrase is
    escrowed to the kit's recovery key (§2.2) — so either channel
    opens from the kit and from nothing else.
7.  **Provisioning is scripted.** Minting and distributing a
    credential is an executable procedure — a `credentials`
    subcommand (§4), never a documented sequence of console clicks.
8.  **Boundary**: per-app secrets (OIDC clients, app API keys) follow
    the two-channel rule (cluster-infra.md §1.1) and are enumerated by
    the program itself — out of register scope. The register tracks
    infra and cross-system credentials.

## 2. Seeds

Automation never consumes these; it only *unseals* them, for the
length of one bring-up, one rotation or one recovery. The set is
deliberately tiny — everything in §3 grows out of it or is opened by
it.

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

Three of them are nonetheless *used* from the workstation. Two by a
script, because the seeds they mint cannot be minted by anything
smaller: an OCI API key belonging to a user who may manage users,
groups and policies in the tenancy, and the B2 account master key,
which re-seeds B2 after a total loss. The third by a provider — the
GitHub admin token, which a `pulumi up -s github` reaches for. (Cloudflare
has no such root: its only job would have been minting the seed, and
the platform forbids that. A token minted through the API may not carry
token-management permissions, so nothing can mint a credential of the
seed's own class.)

**One acquisition chain serves all three**, consulted per field rather
than per root, first hit wins:

1.  the **desktop secret store**, one credential at a time;
2.  the root's **token file**, a workstation slot (§1 rule 6) — the
    layer a non-interactive reader can use;
3.  the root's **environment variable**, which is how CI or a one-off
    shell hands a value in without writing it anywhere;
4.  a **console prompt**, which names the credential and prints how it
    is created.

Because the chain runs per field, a root half-held asks for the half
that is missing and nothing else, and the file and variable names are
recorded in the register itself (`masters.py`) rather than being
conventions a reader has to reconstruct.

`credentials master <root> remember` is the only thing that ever writes
a root, and which layer it writes follows from who reads it. A root a
*script* asks for goes to the secret store, so a value that can stay
out of the filesystem does — falling back to the token file on a machine
that has no store at all, which is what makes `remember` meaningful on a
headless box. The GitHub token goes to its file, because what reads it
is a `mise.toml` template that can open neither a keyring nor a prompt;
a second copy in the store would be exposure bought for nothing.
`credentials master ls` says which roots this machine holds and which
layer each came from, printing no values, and `credentials master
<root> forget` removes both writable layers. Neither the estate
database nor its master password is ever opened by anything in this
repository.

Keeping them out is what makes §2.1's argument true rather than
aspirational: every row below has a designed
rotate-on-compromise path, so a compromised kit is answered by one
full rotation, while an account root has no such path and would leave
that rotation incomplete.

| Kit row | What it mints | Self-reproducing |
| --- | --- | --- |
| OCI seed API key (its own user, group and policy: manage users, groups and policies in the tenancy) | The per-stack OCI users and their API keys | **Yes** — IAM creates users and keys, its own included |
| Cloudflare seed token (**API Tokens Write**, and **Zone Read** on all zones) | The zone-scoped provider token, the DNS-01 token, the gateway's ACME token | No — a minted token may not carry token permissions, so no token can mint this one |
| B2 seed key (`writeKeys`/`deleteKeys` + bucket admin) | The management key and every prefix-scoped writer key | **Yes** — `b2_create_key`. The account's *master* key is an account root and lives in the personal estate, borrowed only to re-seed |
| GitHub App private keys + **client ids** (**two** single-purpose Apps: dispatch, trigger — permissions are per-App; the JWT's `iss` is the client id, the numeric app id being deprecated for that use) | Installation tokens (8 h, minted per run) | No — key generation is console-only |
| ZeroTier Central API token | Nothing (it *is* the provider credential; ZT has no token API) | No — console-only |
| **Recovery key** (an age X25519 identity; its public recipient is committed here, its private half is kit-only) | Nothing — it *opens* the escrowed copy of every locally-generated secret (§2.2) | Generated, not minted (§2.2) |

The "No" rows are the whole manual surface of a rotation: the
rotation script stops, prints what to create in which console, and
resumes when the new value is handed to it. Each of those pauses needs
an account login, which is the one thing the kit deliberately does not
carry — so a rotation run by a successor starts in the personal estate
(§2.1), and the kit's README says so.

Three rows are console-only because the platform has no API for what
they are: the two GitHub Apps' private keys and the ZeroTier Central
token. The Cloudflare row is console-only for a different reason,
and the distinction matters to anyone reading the API and wondering
why it is not used — the call exists and is refused. A sub-token "is
not allowed to have permissions to manage other tokens"
([Cloudflare's own
documentation](https://developers.cloudflare.com/fundamentals/api/how-to/create-via-api/)),
and *API Tokens Write* is exactly what the seed carries, so any
successor minted from the seed would be a token that cannot mint.
Bring-up and rotation are therefore the same dashboard visit: **User →
API Tokens → Create Token → Create Additional Tokens**, with **Zone →
Zone → Read** on all zones added to the template's own **User → API
Tokens → Edit**, and the superseded token deleted on the same page once
the new kit is written. The added permission is what lets the seed turn
a zone name into the id a minted policy names, so the scripts refuse a
seed whose zone listing is empty at the moment it is pasted in rather
than at the first mint. A permission added to a token that already
exists does not extend the value already in hand, so the way to correct
a seed's permissions is to make a new token and record it, not to edit
the old one — an operator who already holds a token of that template as
an "account root" holds the seed only if it carries both permissions
already. What the seed *does* mint is
§3's tokens, which carry zone permissions and no token permissions —
the class the platform does allow.

**Which OCI identity API touches the OCI row.** A tenancy with identity
domains keeps users, groups, group membership and user credentials in
the domain; the legacy endpoints for them are a conversion shim over it,
and the shim refuses — sometimes always, sometimes intermittently, and
sometimes only for a field it cannot represent. So everything the domain
owns goes through the identity-domains client, and the legacy identity
client keeps two jobs and only those: the concepts that are IAM's own
rather than the domain's — policies, compartments, `list_domains` — and
being the whole of the identity API in a tenancy that has no domains,
where every call falls back to it unchanged. The fallback runs **both
ways**: either side has been seen to refuse a call that the other side
then accepted, so a refusal is a reason to try the other client rather
than to stop, and the direction only says which one is tried first.
Within the domains API
a call reaches the caller's own user through the self-service endpoints,
which authorize on authentication alone, and anybody else's through the
administrative ones, which need domain-admin rights — the account root
has those and the seed does not, which is why everything the seed does to
itself is self-service (§4.3).

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
    else does. The recovery key (§2.2) needs nothing beyond that
    shape: its public recipient is the identifier, its private identity
    is the secret. Key material that is a *file* is an attachment: the two
    GitHub App PEM files, and the OCI API key. The OCI row is the one
    that needs more than those two fields, an API key there being five
    things: `UserName` is the user `OCID`, the PEM is the attachment,
    and the **tenancy `OCID` is a protected custom attribute** — the
    same protection class as the password field, because an `OCID` is
    an account identifier and a listing has no reason to hand it out. The
    remaining two are recovered rather than stored: the region is a
    constant in the code, and the fingerprint is a function of the
    public key, so a stored copy could only ever disagree with the key
    it describes. One further attribute on that row is not part of the
    key at all: the **identity domain `URL`**, because retiring a
    superseded API key goes through the identity-domains API and that
    API is addressed per tenancy rather than per region (§4.3).
-   **Contents = §2's rows plus a printed README**: the recovery entry
    points (this repo's URL, this document, the reverse-cold-standby
    runbook) written for a **technical reader who has never seen this
    system**. Because the account roots are not in the kit, the README
    carries the one pointer that keeps succession unbroken: which
    provider accounts exist, that their credentials live in the
    personal estate, and that the estate's own succession is arranged
    separately. Without it a successor can open every seed and still
    not reach the console-only rotations (the two Apps, ZeroTier and
    Cloudflare), or re-seed after a total loss.
-   **Opened twice in a system's life**: at bring-up (§4.1) and at
    rotation (§4.2) — plus the yearly offline day, which opens one kit
    and verifies it against §2's table. It is emphatically **not** a
    day-2 operations database: no runbook outside those three asks for
    it, because no runtime credential is in it.
-   **Refresh discipline**: rotation writes a **new database file**
    (§4.2) and re-encrypts the escrow registry to the new kit's
    recovery key in the same run, and re-issuing the kit is copying
    that file onto both sticks (paper reprints only when the master
    password changed).
-   **Succession**: the successor (**Miu**) knows the kit locations
    and that this README exists — that is the entire protocol. The
    offline-day check includes re-reading the README with fresh eyes:
    instructions rot faster than keys.

### 2.2 The recovery key: one identity behind every generated secret

Some secrets are not minted by any provider — a passphrase, a CA key,
a backup encryption key. Storing each one would turn the kit back into
a growing token drawer, so instead the kit holds **one recovery key**,
an age identity, and each such secret is **generated at random and
escrowed**: the plaintext goes to the slot §3 names for it, and a copy
encrypted to that identity's public recipient is committed to this
repository.

The escrow registry is a directory of ciphertexts —
`escrow/<label>/<gen>.age`, one file per generation of one label, with
the recipients they are encrypted to in `escrow/RECIPIENTS`. A
*generation* is one rotation of that label's value: the highest is
current, and the older ones stay for as long as anything still answers
to them — a dump encrypted to a superseded age identity, a certificate
issued under a superseded CA.

| Label | Escrowed secret |
| --- | --- |
| `pulumi/passphrase` | Pulumi state passphrase |
| `alertmanager/read` | Bearer token the issue-sync poller presents (§3) |
| `state-backend/ca` | State-backend CA private key |
| `backup/age/<generation>` | age identity for pg_dump encryption |

Two things are called a generation on that table, and they are not the
same: `backup/age/<generation>` names the *backup* generation
(state-backend.md §5), which is part of the label, and each such label
holds one identity for its lifetime — so its escrow has one generation
of its own, and rotating the backup key means a new label rather than a
new generation under the old one.

That table is the whole of it: **only durable roots are escrowed.** A
value earns a row by being one whose loss loses data or forces a
production rotation, and which nothing upstream can re-mint. Leaf
certificates under the state-backend CA fail the second test — their
keys are generated at issuance and never escrowed, because the CA
re-issues them (§3) — and so do the secrets a *program* generates,
restic repository passwords among them: Pulumi state already stores
those (rule 6), and escrowing them would buy nothing.

Consequences, all deliberate:

-   **Generation and escrow are one act.** No command produces a random
    secret without committing its ciphertext in the same run, so a
    secret that reached a slot but not the registry is a state the
    scripts do not create and `credentials escrow check` (§4) exists to
    catch. A value born outside that path — one a predecessor hands
    over, or one that predates the model — joins the registry by being
    imported as a generation, not by being replaced (§4.2).
-   **Nothing reads escrow at runtime.** Opening a ciphertext needs the
    recovery private key, which is in the kit and nowhere else, and it
    happens during bring-up, rotation, provisioning, or a recovery —
    the Alertmanager token is read by a running poller, the key that
    could open its escrow never is. That is what keeps §1's rule 4 —
    nothing consumes a kit row at runtime — true rather than
    aspirational.
-   **Rotating the kit is not rotating production.** The value in the
    slot is random, so re-encrypting its escrow to a new recovery key
    changes nothing any consumer sees. This is the property a derived
    secret cannot have: a value computed from a stored seed *is* a
    function of that seed, so replacing the seed for custody reasons —
    a new kit, a new custodian, a stick out of its envelope — replaces
    the state passphrase (re-encrypting every stack) and the
    state-backend CA (a re-provision, restore-shaped,
    state-backend.md §7) along with it. Escrow separates the two
    events: rotating one credential is a new generation of one label
    adopted by one consumer (§4.2), and rotating the kit is
    re-encryption and nothing else.
-   **The ciphertexts are committed to a public repository on
    purpose**, for the same reason `Pulumi.<stack>.yaml` is (rule 6):
    what protects them is the recovery private key, and a registry that
    travels with the repository is one a clone already carries. It
    also fixes the blast radius — a leaked recovery private key is a
    leak of every escrowed label at once, which is why §4.2 treats it
    as a full production rotation.
-   **State is therefore a data-safety dependency, not a
    convenience.** A restic repository's password exists only in state
    and in the SealedSecret rendered from it. Lose both the backend and
    its dumps and the B2 backups survive with nothing to open them —
    which is why the recovery chain (kit → recovery key →
    `escrow/backup/age/<generation>` → the dump in B2 → state) is
    drilled rather than assumed (operations.md §4).
-   **Escrow constrains no algorithm.** An escrowed key is generated
    the way its consumer wants it and stored as the bytes it is, so the
    state-backend PKI's curve and age's X25519 are each their own
    design's choice (state-backend.md) rather than a consequence of how
    the kit stores them.
-   **The sealed-secrets sealing key is not a recovery root.** It is
    the controller's own generated RSA key; losing it costs a re-seal,
    not data, because every sealed value is itself escrowed or
    re-mintable. Re-sealing is a script, not an archaeology project —
    which is why no offline export of it exists.
-   **A retired recovery key owes nothing forward.** Rotating the kit
    re-encrypts every generation in the registry to the successor
    identity (§4.2), and no secret is a function of the key that
    wrapped it, so backups keep opening under the same age identities
    and the retired kit is destroyable as soon as that re-encryption is
    verified. There is no waiting period tied to the retention of
    anything.

## 3. Derived credentials

Everything here is minted from a seed, generated and escrowed (§2.2),
or generated by the program that owns it, and delivered straight to the
slot named in its row. None of it is stored offline — an escrowed
secret's ciphertext is not the offline store — and none of it is copied
by hand.

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
| Pulumi state passphrase | generated, escrowed as `pulumi/passphrase` | Decrypts state secrets | escrow · CI env (all stacks) · workstation slot | every `pulumi` run |
| State-backend CA | generated, escrowed as `state-backend/ca` | Issues every certificate below | escrow (private half) · on-box and every bundle (the certificate) | certificate issuance |
| State-backend certificates (server, `ci`, `operator`) | issued from the CA, keys generated at issuance and never escrowed | postgres:// mTLS | on-box (server) · CI env · workstation slot (the `operator` bundle) | Pulumi state access |
| age backup identity | generated, escrowed as `backup/age/<generation>` | Decrypts state-backend pg_dumps | escrow · on-box (public half) · ops-repo `drill` Environment (private half, latest-dump-only) | micro cron, drill workflow |
| restic repo passwords | generated in-state (`backed_pvc`) | Per-PVC repos | Pulumi state · SealedSecret (via `backed_pvc`) | VolSync |
| Talos machine secrets + talosconfig | generated by `physical` | Cluster PKI roots | Pulumi state · CI env · ops-repo secret | Talos ops, etcd snapshot workflow |
| kubeconfig | `physical` output | cluster-admin | CI env | `k8s-base`, `apps` |
| UDM SSH key, libvirt SSH identity | generated | gw-config push (host key pinned) / virsh only | Pulumi config secret + CI env | `physical` |
| UniFi API key | Dedicated local admin | Network API | Pulumi config secret + CI env | `physical` |
| AdGuard API credentials | AdGuard admin (no scoped API — audit L11) | alice/bob rewrite API | Pulumi config secret + CI env | `apps` rewrites |
| Alertmanager read token | generated, escrowed as `alertmanager/read` | `GET /api/v2/alerts` only, by HTTPRoute method+path+header match | escrow · ops-repo secret · HTTPRoute spec (Pulumi config secret at render) | Issue-sync poller |
| HA webhook URL/ID | Home Assistant | One notify endpoint | SealedSecret · ops-repo secret | alertmanager, dispatch handler |
| Drill-environment credentials | OCI seed key, B2 seed key | Drill compartment; dump-prefix read-only | ops-repo `drill` Environment | Drill workflows |

Rows whose "From" is a seed rotate by re-running their subcommand.
Rows whose "From" is an escrow label rotate by generating the next
generation of that label (§4.2), which the row's own consumer then
adopts. Rows generated by a stack (Talos secrets, kubeconfig, ZT
identities) rotate with the resource that owns them.

## 4. The scripts

The register's executable form: `credentials`, a console script in
this repo (`src/kluster/scripts/credentials/`), one subcommand per
credential family plus the lifecycle commands below.

| Command | When |
| --- | --- |
| `credentials master <root> remember` | Once per machine and root, before a bring-up or a re-seed that needs it. Keeps one account root (§2) where its readers reach it. Skipping it costs a prompt, not a failure. |
| `credentials master ls` | Which roots this machine holds, and which layer of the chain each comes from. Prints no values. |
| `credentials master <root> forget` | Removes one root from the secret store and from its token file. |
| `credentials master github remember` | Once per workstation that applies the `github` stack. Writes the token file `mise.toml` turns into `GITHUB_TOKEN`; nothing in this repository can recreate the value, so this is where it enters. |
| `credentials bootstrap` | Bring-up, from nothing or from a partial kit. Resumable: re-running skips what is already there. |
| `credentials bootstrap --only <member>` | One seed was lost. Re-creates that row alone. |
| `credentials escrow init` | Once per kit: creates the recovery key (§2.2) and writes `escrow/RECIPIENTS`. `bootstrap` does it while filling a new kit, so this is the repair path for a kit that predates escrow. |
| `credentials seed oci domain` | Once, on a kit written before the OCI row carried its identity domain (§4.3). Borrows the OCI account root; every rotation after it needs nothing but the kit. |
| `state-backend provision` | After the kit exists; every stack needs the backend before it can act. |
| `eval "$(credentials escrow env)"` | Whenever a shell needs to reach the backend. Recovers the passphrase from escrow, reads the URL from the bundle. |
| `credentials derived cloudflare zones` | After the kit and the state backend exist. Mints the zone-scoped Cloudflare token (§3) from the seed and writes it into the `dns` stack's config, together with the account id the stack requires; the stack file is then committed. Re-running it rotates that token. |
| `credentials escrow recover <label> [--stdout]` | Reading an escrowed secret back out. `recover pulumi/passphrase` is the common one: it fills the passphrase slot (§4.4) so `mise.toml` finds it and a local preview needs no offline database; `--stdout` prints instead of writing, for a pipe into another machine. |
| `state-backend bundle operator --address <ip>` | Once per workstation, or after a certificate reissue. Writes the client bundle into its slot; `state-backend provision` ends by doing the same thing. |
| `credentials escrow generate <label>` | Rotating one escrowed credential (§4.2). Generates a new value, writes it to the slot §3 names and commits its ciphertext as the label's next generation — one act, no other label touched. |
| `credentials escrow import <label>` | Escrows a value that already exists as the label's next generation, changing nothing a consumer holds (§4.2). |
| `credentials rotate --into <new kit>` | Rotation (§4.2). Writes a new database; the retired one stays. |
| `credentials escrow rewrap` | After a `rotate` wrote a new recovery key. Re-encrypts every generation in the registry to it and updates `escrow/RECIPIENTS`; no plaintext changes. |
| `credentials escrow check` | Any time, kit or no kit: every label the register names is present, every ciphertext parses, generations are monotonic. It opens nothing, which is what lets CI run it. |
| `credentials kdbx ls` / `show` | Looking without changing. |
| `credentials kdbx remember` | Once per machine, so a run that lasts minutes is not guarded by a password typed into it. The password is proven against the kit before it is stored; `forget` removes it again. |

`credentials --help` carries the same ordering, because a command list
shaped like the register answers neither "where do I start" nor "which
of these destroys something". Every
minting subcommand is **mint → push to every slot in the map →
verify**, and therefore idempotent: rotation is a re-run, not a second
procedure. `escrow generate` keeps that shape — **generate → escrow →
push → verify** — and drops the idempotence deliberately: a re-run
produces a new generation, which is exactly what rotating that label
means.

§3's minted rows are `credentials derived <row> <credential>`, one
command per row, and its escrowed rows are `credentials escrow
generate <label>`, one per label. A row is implemented when its
consumer exists: minting a credential
that has no slot to be delivered into would park a secret, which rule 2
forbids. The zones token is delivered today; the DNS-01 token and the
gateway's ACME token join it with cert-manager and the gateway, and the
CI Environment half of every row joins it with the Environments
themselves (ci.md §2).

The zones token's scope is not a list in the script: it is the estate's
zones as `conventions` names them, resolved to zone ids through the seed
at mint time, so adding a zone there and re-running the command is the
whole procedure for widening it. The push writes two keys, because a
provider credential alone does not identify the account that owns those
zones: `cloudflare:apiToken` as a config secret and the account id in
plain text. The script writes the account id under the unqualified key
`cloudflareAccountId`, which `pulumi config set` and `pulumi.Config()`
both resolve against the project's own name — so the committed file reads
`kluster-py:cloudflareAccountId`, and the project name lives in
`Pulumi.yaml` rather than a second time in the script.

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

### 4.1 Bring-up

Bring-up is a sequence of commands rather than one command: each stage
leaves behind the artifact the next one reads, and each is separately
re-runnable. `credentials --help` prints the same order.

**One database is opened: the kit.** The account roots the minters
borrow are not in a database this repository reads — they come from the
chain in §2: the **desktop secret store**, a token file, an environment
variable, or a prompt where a machine has none of them. The kit's own
master password is asked of the same store first
(`credentials kdbx remember` puts it there),
because a run that then goes on for minutes should not be guarded by a
password typed into a process nobody is watching. Nothing is ever
written to the store implicitly: a `remember` command is the only thing
that puts a value there.

1.  `credentials master <root> remember` — once per machine and root,
    for the account roots the mints borrow (§2). Skipping it costs a
    prompt rather than a failure, which is also how a headless run works.
2.  `credentials bootstrap` — fills the kit with every §2 row, the
    recovery key included, creating the kit if it is absent. A row
    whose platform can mint it is minted; the rest stop and print their
    console steps. This command touches the kit and nothing else.
3.  `state-backend provision` — the Pulumi state backend, which every
    stack needs before it can act, and the first thing to escrow (§2.2):
    it generates the CA and the age identity, commits their ciphertexts,
    and the appliance's Ignition carries what is public about them — the
    server certificate issued under that CA and the age identity's public
    half — plus a B2 dump key minted from the B2 seed. The run ends by
    writing the `operator` client bundle into its workstation slot (§4.4).
4.  `eval "$(credentials escrow env)"` — the two variables a `pulumi`
    run needs: `PULUMI_CONFIG_PASSPHRASE`, recovered from escrow and
    stored in no slot, and `PULUMI_BACKEND_URL`, read from the bundle the
    previous stage wrote.
5.  `credentials derived cloudflare zones` — mints the zone-scoped
    Cloudflare token from the seed into the `dns` stack's config, which
    is then committed. One §3 row per command, and re-running one rotates
    that row.

A stage that fails is re-run; nothing is parked. Once the last one is
done, the kit goes back in its envelope.

**What is not built yet** (`kluster-ops#1`): everything in §3 except the
zones token, and every slot that is not a Pulumi config secret. The
per-stack OCI keys and the B2 management key have minting
code but no command that pushes them; the device credentials (UDM SSH
key, libvirt identity, UniFi API key, AdGuard credentials), the
in-cluster secrets (the DNS-01 token and the writer keys, sealable only
once `k8s-base` has the sealed-secrets controller up) and the CI
Environment half of every row (ci.md §2) have neither. Restic passwords
will not join them: `backed_pvc` generates its own into state and seals
it, so a new volume needs no credentials run (rule 6). Until those
commands exist, a bring-up delivers the seed kit, the state backend and
the zones token, and the rest of §3 is design rather than procedure. The
escrow registry and its command family are design here too, in the same
sense: their implementation is `kluster-ops#37`.

**Resumable by probing, not by bookkeeping.** `bootstrap` asks whether
each row is already in the kit and skips it if so, so an interrupted run
is resumed by re-running the same command, and `--only <member>` is the
repair path when a single seed is lost. `state-backend provision`
compares the running appliance against the repository the same way. A
checkpoint file would record "this ran" — which stops being true the
moment someone deletes a key in a console, and the run after that would
skip the repair.

**The console steps live in the register, not in a runbook.** Each §2
row that no API can create carries the instructions for creating it
(`entries.py`), so `bootstrap` prints them at the moment it stops
rather than sending the operator to look for a document.

### 4.2 `credentials rotate`

Two rotations live here, and the model's point is that they are
independent: rotating **the kit** (this command) touches no production
value, and rotating **one credential** (`credentials escrow generate`,
below) touches no other credential.

`--only <member>` re-runs one row; the default rotates the whole kit.
It **writes a new database file** (`--into`), and the retired one
is left byte-for-byte as it was: unseal the old, have each seed mint its
successor, generate a fresh recovery key, and write all of it into
the new database. The GitHub App private keys, the ZeroTier token and
the Cloudflare seed token are explicit pauses — the script prints the
console steps and waits.

**The new recovery key is adopted by re-encryption.** `credentials
escrow rewrap` opens every generation in the registry with the retired
identity and writes it back encrypted to the successor's recipient,
updating `escrow/RECIPIENTS`. No plaintext changes, so no consumer is
touched, no stack is re-encrypted and no appliance is re-provisioned —
that commit and the new database are the whole of a kit rotation.

**What a run proves is that the new kit's seeds work, one row at a
time.** A seed that mints its own successor (OCI, B2) authenticates *as*
that successor before the predecessor's key is retired, so a run
interrupted anywhere leaves a working seed in one kit or the other; the
console-made Cloudflare token is checked for both permissions the seed
needs (§2) while the operator is still on the page that fixes either.
The rows that are only pasted in — the two App private keys and the
ZeroTier token — are stored as given, so a wrong value there surfaces at
its first use rather than during the rotation.

Nothing beyond the kit is touched. The §3 credentials minted from
the retired seeds keep working, and each is replaced by re-running its
own command against the new kit: rotation neither re-mints them nor
inspects a slot, and the slot verification §4's slot map describes is
not built either (`kluster-ops#1`). The escrowed credentials keep
working for a stronger reason — their values are unchanged, only their
wrapping is.

**The retired kit is destroyable once nothing is owed to it**: once
`rewrap` has covered every generation and `credentials escrow check`
passes, no ciphertext in the registry opens only with the retired
recovery key, and every seed the retired database holds has a live
successor. That is the whole criterion: a property to verify, not a date
to wait out.

**Rotating one credential is a generation, not a kit.** `credentials
escrow generate <label>` produces a new random value, writes it to the
slot §3 names for that row and commits its ciphertext as the label's
next generation; adopting it is that one consumer's business — a
re-provision for the state-backend CA, a re-seal or a secret update for
the Alertmanager token, the recipient swap that state-backend.md §5
describes for an age generation. The recovery key is not involved,
and no other label moves.

**A value that already exists is imported rather than replaced.**
`credentials escrow import <label>` escrows what a slot or a predecessor
already holds as that label's next generation. That is how a kit written
before escrow joins the model: the live state passphrase, CA key and age
identities become first generations, nothing production-facing rotates,
and the superseded kit is destroyable as soon as `escrow check` and a
recovery of each imported label prove the registry holds them.

**A compromised recovery key is a full production rotation.** The
registry is public, so an attacker who holds that key holds every
escrowed value; re-wrapping is no answer, because it changes none of
them. The order is a new kit first — so that what comes next is wrapped
to a key the attacker does not have — and then a new generation of every
label, adopted consumer by consumer. No design escapes this: one offline
secret able to reproduce every generated credential is what buys the
ability to recover them at all, and the derivation seed concentrated
exposure the same way.

Rotation cadence lives with the drill program (operations.md §4); the
yearly offline day verifies the current kit against §2.

### 4.3 Retiring an OCI API key

A tenancy with identity domains refuses `DELETE
/users/{id}/apiKeys/{fingerprint}` — `IdcsConversionError: Client is
unauthorized` — to the account root and to the key's own user alike,
so the legacy call cannot retire anything. Retirement goes through the
identity domain's **self-service** endpoints instead
(`list_my_api_keys` / `delete_my_api_key`), which act only on the
caller's own user and require authentication rather than a policy.
That is why the sweep of superseded keys runs as the seed and not as
the root, and why the seed needs no permission beyond the three
statements in its policy.

Those endpoints live on a per-tenancy URL, which is discovered once —
`list_domains` in the tenancy compartment, an administrator's call on
the legacy identity client (§2) — at the moment the account root is
already in hand, and stored on the row. Rotation reads it there. A row written before that
attribute existed tries to discover it as the seed and, where the
tenancy refuses, warns and names `credentials seed oci domain`: the
one-time repair that borrows the root, records the `URL`, and returns
routine rotation to needing nothing but the kit. A rotation whose
retirement is refused is not a failed rotation — the successor is
minted, verified and stored, and the key that could not go is a
console errand.

### 4.4 What a workstation keeps, and where

Everything local a checkout needs lives in one git-ignored directory
beside `mise.toml`, so a second machine is a clone plus a copy and
never a hunt for per-machine environment wiring:

| `.credentials/` | What it is | Written by |
| --- | --- | --- |
| `kit.kdbx` | The seed kit (§2.1), on the workstation that holds one. Not a slot — the offline store, whose canonical copies are the two envelopes. `$KLUSTER_KDBX` overrides the path, for a kit on removable media. | `credentials bootstrap` |
| `pulumi.passphrase` | The state passphrase (§2.2), cached so a local preview needs neither the kit nor an `eval`. | `credentials escrow recover pulumi/passphrase` |
| `roots/<root>.<field>` | An account root's token file — the second layer of §2's chain. Today `github.token` is the one a tool reads. | `credentials master <root> remember` |
| `state-backend/` | The `operator` client bundle: CA, certificate, key, and the URL naming them. The key is `0600`, which libpq insists on. | `state-backend provision`, `state-backend bundle operator` |

The directory is `0700` and its files are `0600`. Only the kit is
irreplaceable, and it is irreplaceable in the envelopes rather than
here; every other entry is recovered, re-issued or re-pasted by the
command in the right-hand column, so a lost `.credentials/` costs a
few commands and no credential.

`mise.toml` reads three of them — the passphrase, the backend URL and
the GitHub token — falling back to whatever the environment already
holds, which is how the same file serves CI, where all three arrive as
Environment secrets. Because each is read by template rather than by a
program, nothing on that path can prompt: that is the whole reason
these are files and not secret-store entries (§1 rule 6).

**Moving to another workstation** is copying the directory (`rsync -a`),
minus `kit.kdbx` unless that machine is meant to hold the kit — one
copy in place of the mixture of an `rsync` under `~/.config`, a piped
passphrase and a handwritten token file that it replaces. The one
thing the copy assumes is that both checkouts sit at the same path:
libpq expands nothing, so the bundle's URL names its three certificate
files absolutely, and a checkout somewhere else needs those paths in
`state-backend/backend-url` corrected — three edits in a plain string,
or a re-run of `state-backend bundle operator` on a machine that holds
the kit.

Two of these slots had other homes before, and both old locations are
still read — the bundle by `credentials`, with a warning naming the
move; the passphrase and token files by `mise.toml`, silently, because
a template has no way to warn. A workstation that predates the move
therefore keeps working untouched, and converges by running the
commands above once. The fallbacks are marked in the code and in
`.gitignore` for deletion (`kluster-ops#34`).
