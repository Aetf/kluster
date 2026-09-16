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
    GitHub Environments and the `drill` Environment, ci.md §3) ·
    ops-repo secret · `kluster` repository secret (the one slot that
    belongs to no stack, and is therefore readable by every workflow in
    the repository — ci.md §3) · **workstation slot** · on-box
    (delivered by provisioning, e.g. Butane-embedded) · device secret
    (pushed to the gateway as a file beside its nspawn units,
    physical/gateway.md §1). A row names its channel(s); a
    credential living anywhere else is misplaced.

    A **workstation slot** is the local half of a credential: a file
    under the checkout's git-ignored `.credentials/` (§4.4), written by
    a `credentials` or `state-backend` command and read
    non-interactively afterward — by `mise.toml` building a `pulumi`
    run's environment, or by a script that must not stop to ask. It is
    the channel for what CI holds as an Environment secret and a
    workstation needs anyway: the Pulumi passphrase and the state
    backend's `operator` bundle. Deliberately not the desktop secret
    store, which is where account roots go (§2) — a root is interactive
    and rare, so a store that asks a session to unlock suits it, while
    these are read on *every* `pulumi` run by a template that can
    neither prompt nor unlock a keyring. **No provider credential is
    here**: each of those is a config secret in the stack that reads it,
    which is the channel below and the reason a `pulumi` run needs no
    prepared shell beyond the passphrase. A slot holds only what a
    command can write again — the passphrase is recovered from escrow,
    the bundle re-issued — so losing one costs a command and never a
    credential.

    The two Pulumi channels are separate rows because their exposure
    is opposite. **Config secret** lives in `Pulumi.<stack>.yaml` and
    is committed: its ciphertext is public the moment the repo is, so
    it carries only what must exist *before* a program can run —
    provider credentials. **State** lives in the state backend's
    Postgres and never enters git, which makes it the stronger of the
    two and the right home for what a program *generates* (Talos
    machine secrets, ZeroTier identities, restic repository
    passwords). Both channels are protected by the *estate*
    passphrase, escrowed to the kit's recovery key (§2.2) — so either
    channel opens from the kit and from nothing else.

    **One stack is encrypted apart: `github`.** Its config secret is
    the admin token that can switch off the protections guarding
    `main`, and the estate passphrase is in every CI Environment
    because every job runs a `pulumi` command — so a config secret
    under *that* passphrase is readable by anything CI can start,
    which for the ungated pull-request Environments means anybody who
    can push a branch here. The `github` stack therefore has a
    passphrase of its own (§3), escrowed like the estate's and
    delivered to no Environment at all. The channel is unchanged and
    so is its exposure: `Pulumi.github.yaml` is committed, and its
    ciphertext is public. What differs is that the key opening it is
    held by the workstation and the kit alone.
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
it, and that is also the membership rule: a credential that mints
nothing and opens nothing is a §3 row however it is made, delivered
into its consumer's slot even where a console is the only thing that
can create one. Keeping such a row here would give one credential two
homes and hand the kit a rotation it cannot perform.

**Account roots are not in this set.** Console logins and their MFA
recovery codes for the five provider accounts (OCI tenancy, GitHub,
Cloudflare, Backblaze, ZeroTier Central) are a *precondition* of the
system rather than a credential it manages: nothing scripted reads
them, and every seed below is minted through a console exactly once
and self-reproduces from then on. They live in the operator's personal
estate — its own database, its own succession — and the seed kit
borrows them at two moments only: first bring-up, and re-seeding after
a total seed loss.

**A root is the account itself, never a credential made inside it.**
Where an account's console can create a credential the system is able to
hold — a token, an API key, an admin login — that credential is a §3 row
delivered into the slot its consumer reads, and only the login that made
it stays here. The GitHub admin token the `github` stack declares the
forge with is that case, and is a §3 row delivered by `credentials
derived github-admin record`; so is the ZeroTier Central API token. What
is a root is the GitHub account itself, which no §3 row can replace and
which nothing in this repository opens.

Two of them are nonetheless *used* from the workstation, both by a
script, because the seeds they mint cannot be minted by anything
smaller: an OCI API key belonging to a user who may manage users,
groups and policies in the tenancy, and the B2 account master key,
which re-seeds B2 after a total loss. (Cloudflare
has no such root: its only job would have been minting the seed, and
the platform forbids that. A token minted through the API may not carry
token-management permissions, so nothing can mint a credential of the
seed's own class.)

**One acquisition chain serves both**, consulted per field rather
than per root, first hit wins:

1.  the **desktop secret store**, one credential at a time;
2.  the root's **token file**, a workstation slot (§1 rule 6) — the
    layer a machine with no desktop secret store falls back to;
3.  the root's **environment variable**, which is how CI or a one-off
    shell hands a value in without writing it anywhere;
4.  a **console prompt**, which names the credential and prints how it
    is created.

Because the chain runs per field, a root half-held asks for the half
that is missing and nothing else, and the file and variable names are
recorded in the register itself (`masters.py`) rather than being
conventions a reader has to reconstruct.

`credentials root <name> remember` is the only thing that ever writes
a root, and it writes the secret store, so a value that can stay
out of the filesystem does — falling back to the token file on a machine
that has no store at all, which is what makes `remember` meaningful on a
headless box.
`credentials root ls` says which roots this machine holds and which
layer each came from, printing no values, and `credentials root <name>
forget` removes both writable layers. Neither the personal estate
database nor its master password is ever opened by anything in this
repository.

Keeping them out is what makes §2.1's argument true rather than
aspirational: every row below has a designed
rotate-on-compromise path, so a compromised kit is answered by one
full rotation, while an account root has no such path and would leave
that rotation incomplete.

| Kit row | What it mints | Self-reproducing |
| --- | --- | --- |
| OCI seed API key (its own user, group and policy: manage users, groups, policies and compartments in the tenancy) | The per-stack OCI users and their API keys, and the compartment each is confined to | **Yes** — IAM creates users and keys, its own included |
| Cloudflare seed token (**API Tokens Write**, and **Zone Read** on all zones) | The zone-scoped provider token, the DNS-01 token, the gateway's ACME token | No — a minted token may not carry token permissions, so no token can mint this one |
| B2 seed key (`writeKeys`/`deleteKeys` + bucket admin) | The management key and every prefix-scoped writer key | **Yes** — `b2_create_key`. The account's *master* key is an account root and lives in the personal estate, borrowed only to re-seed |
| **Recovery key** (an age X25519 identity; its public recipient is committed here, its private half is kit-only) | Nothing — it *opens* the escrowed copy of every locally-generated secret (§2.2) | Generated, not minted (§2.2) |

The one "No" row is the whole manual surface of a rotation: the
rotation script stops, prints what to create in which console, and
resumes when the new value is handed to it. That pause needs an
account login, which is the one thing the kit deliberately does not
carry — so a rotation run by a successor starts in the personal estate
(§2.1), and the kit's README says so.

The Cloudflare row is console-only for a reason worth stating, because
the API a reader would reach for exists and is refused. A sub-token "is
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
    is the secret. Key material that is a *file* is an attachment: the
    OCI API key. The OCI row is the one
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
    provider accounts exist, that their credentials live in the personal
    estate, and that the personal estate's own succession is arranged
    separately. Without it a successor can open every seed and still not
    reach the Cloudflare seed's console-only rotation, replace the
    console-made credentials §3 carries — the ZeroTier Central token,
    the GitHub admin token, the two GitHub App keys — or re-seed after a
    total loss.
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

| Escrowed secret | Label | Origin |
| --- | --- | --- |
| Pulumi state passphrase | `pulumi/passphrase` | Generated |
| The `github` stack's own config passphrase (§3) | `github/passphrase` | Generated |
| Bearer token the issue-sync poller presents (§3) | `alertmanager/read` | Generated |
| State-backend CA private key | `state-backend/ca` | Generated |
| age identity for pg_dump encryption | `backup/age/<generation>` | Generated |
| Private key of the dispatch App (§3) | `github/dispatch-key` | Console |
| Private key of the trigger App (§3) | `github/trigger-key` | Console |

Two things are called a generation on that table, and they are not the
same: `backup/age/<generation>` names the *backup* generation
(state-backend.md §5), which is part of the label, and each such label
holds one identity for its lifetime — so its escrow has one generation
of its own, and rotating the backup key means a new label rather than a
new generation under the old one.

**Two rows are not generated here**, and the Origin column is what says
so. A GitHub App's private key is created on the App's own settings
page, disclosed once, and created by no API of that platform; what
this side does is take it as it stands and escrow it, which is the same
ciphertext, the same generations and the same recovery as everything
above. It is here rather than in the kit because it mints nothing the
credential tree derives from, and a kit row that a workflow consumes
would make every kit rotation owe that workflow a redelivery (§2). What
the registry buys such a row is that a lost slot is refilled from a
ciphertext instead of by another visit to that page; what it does not
buy is reproducibility — losing the ciphertext costs a new key there,
not the credential.

That table is the whole of it: **only durable roots are escrowed.** A
generated value earns a row by being one whose loss loses data or
forces a production rotation, and which nothing upstream can re-mint. A
console-made one earns it on that second test alone: no seed can mint
it, so the registry is the only home for a copy that the kit's own
rotation does not have to hand out again. Leaf
certificates under the state-backend CA fail the second test — their
keys are generated at issuance and never escrowed, because the CA
re-issues them (§3) — and so do the secrets a *program* generates,
restic repository passwords among them: Pulumi state already stores
those (rule 6), and escrowing them would buy nothing. The drill age key
fails it too, for the same reason from the other side: every dump it
opens is also encrypted to an escrowed generation, so losing it costs a
new key and a recipient swap rather than a byte of data.

Consequences, all deliberate:

-   **Generation and escrow are one act.** No command produces a random
    secret without committing its ciphertext in the same run, so a
    secret that reached a slot but not the registry is a state the
    scripts do not create and `credentials derived check` (§4) exists to
    catch. A value born outside that path — one a predecessor hands
    over, or one that predates the model — joins the registry by being
    imported as a generation, not by being replaced (§4.2).
-   **Nothing reads escrow at runtime.** Opening a ciphertext needs the
    recovery private key, which is in the kit and nowhere else, and it
    happens during bring-up, rotation, provisioning, or a recovery —
    the Alertmanager token is read by a running poller, the key that
    could open its escrow never is. That is what keeps §1's rule 4 true
    of the widest-scoped row in the register: it opens every escrowed
    label at once, and it is consumed by nothing.
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
    designed to be drilled rather than assumed (operations.md §4).
    Designed, not yet done: no drill has run, and the restore step of
    that chain has never run against a live box either.
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

The **Slot** column is written in a fixed vocabulary, so that it can be
checked rather than merely read: channels are separated by `·`, each
entry begins with its channel's term from `register_column` in the slot
map (`kluster/scripts/credentials/slots.py`), and an entry qualified
`pending` is a channel this register promises that nothing addresses
yet. A test holds the column against the map row by row, so a cell
cannot promise a delivery the code does not make. A `pending` qualifier
is held against the map row's reason **for that same channel**: the row
files each reason under the channel it is about, so a cell that defers
one channel while the row is waiting on a different one is drift rather
than a cell that reads well. Where a credential is several rows, a
channel one of them fills while another defers it is drift too — the
cell would say `pending` to an operator already being served.

| Credential | From | Scope | Slot | Consumer |
| --- | --- | --- | --- | --- |
| OCI API key (`physical`) | OCI seed key | Its own user, group and policy; administrator of the `physical` compartment and a stranger outside it | Pulumi config secret | `physical` |
| OCI API key (state backend) | OCI seed key | The same shape, over the appliance's own compartment | workstation slot (§4.4) | `state-backend provision` |
| Cloudflare token (zones) | CF seed token | DNS edit, this installation's zones only | Pulumi config secret | `dns`, `apps` |
| Cloudflare token (DNS-01) | CF seed token | `_acme-challenge` edit only | SealedSecret | cert-manager |
| Cloudflare token (gateway ACME) | CF seed token | DNS edit on the zones the gateway's own vhosts are served under | Pulumi config secret (`physical`) · device secret (caddy's token file) | the gateway's caddy, written onto the device by `physical` |
| B2 management key | B2 seed key | Bucket/key/lifecycle admin, **no file capabilities** | Pulumi config secret | `physical` |
| B2 writer keys | B2 seed key (via `physical`) | Prefix-scoped, `list+read+write`, **no `deleteFiles`** — deletes degrade to lifecycle-purged hides (audit H4): VolSync, CNPG barman, etcd snapshots | SealedSecret · ops-repo secret (pending) | restic/barman, ops-repo workflow |
| B2 dump key (micro) | B2 seed key | `writeFiles` alone, dump prefix | on-box (Ignition) | state-backend pg_dump timer |
| B2 freshness key (state dumps) | B2 seed key (`credentials derived b2-freshness-dumps mint`) | `listFiles` alone, confined to the dump prefix of the appliance's bucket: names, never a byte | ops-repo secret (`B2_FRESHNESS_DUMPS_KEY_ID`, `B2_FRESHNESS_DUMPS_KEY`) | `state-backend probe`, run by the ops repo's scheduled probes workflow (state-backend.md §6) |
| B2 freshness key (etcd) | B2 seed key | The same shape over the etcd snapshots' prefix of the backup bucket, which a B2 key cannot share with the one above: a key confines to one bucket | ops-repo secret (pending) | the etcd snapshot age probe |
| GitHub App key (dispatch) | Made on the App's own page (no key API) | Signs a JWT for that App alone, which mints an 8 h installation token carrying contents:write on `kluster-ops` | escrow as `github/dispatch-key` · `kluster` repository secret (`DISPATCH_APP_PRIVATE_KEY`) | the alert producer (`alert.yml`), called by every workflow that runs on `main` (ci.md §3) |
| GitHub App key (trigger) | Made on the App's own page (no key API) | The same, for an 8 h token carrying actions:write on `kluster` | escrow as `github/trigger-key` · ops-repo secret (`TRIGGER_APP_PRIVATE_KEY`) | Weekly drift trigger (the ops repo's `drift-trigger.yml`) |
| ZT CI member identities (`ci-physical`, `ci-dns`) | generated in-state (`zerotier_identity`) | One per joining stack, `ci`-tagged and flow-rule-confined (gateway.md §2.3) | CI env | CI per-run join |
| Pulumi state passphrase | generated, escrowed as `pulumi/passphrase` | Decrypts state secrets, and the config secrets of every stack but `github` | escrow · CI env (all stacks) · workstation slot | every `pulumi` run |
| `github` stack passphrase | generated, escrowed as `github/passphrase` | Decrypts the `github` stack's config secrets and nothing else | escrow · workstation slot | a `pulumi` run against `github`, and the `credentials` commands that reach that stack's config |
| State-backend CA | generated, escrowed as `state-backend/ca` | Issues every certificate below | escrow (private half) · on-box and every bundle (the certificate) | certificate issuance |
| State-backend certificates (server, `ci`, `operator`) | issued from the CA, keys generated at issuance and never escrowed | postgres:// mTLS | on-box (server) · CI env · workstation slot (the `operator` bundle) | Pulumi state access |
| age backup identity | generated, escrowed as `backup/age/<generation>` | Decrypts state-backend pg_dumps | escrow · on-box (public half, a Butane recipient) | micro cron, a restore run from the kit |
| Drill age identity | generated by `credentials derived drill-age-identity generate` and escrowed nowhere: the same generator as the backup identities (`age-keygen`), outside the generations | Its contract is the newest dump alone, with retention coverage left to the escrowed generations; what it *opens* is every dump written since it became a recipient and still in retention, whose payload secrets stay under the state passphrase | ops-repo Environment (`drill`, the private half, as `DRILL_AGE_IDENTITY`) · on-box (the public half, the third Butane recipient, read from `deploy/state-backend/drill-recipient.txt`) | Quarterly rebuild drill (state-backend.md §7.3) |
| restic repo passwords | generated in-state (`backed_pvc`) | Per-PVC repos | Pulumi state · SealedSecret (via `backed_pvc`) | VolSync |
| Talos machine secrets + talosconfig | generated by `physical` | Cluster PKI roots | Pulumi state · ops-repo secret (pending) | Talos ops, etcd snapshot workflow |
| kubeconfig | `physical` output | cluster-admin | Pulumi state | `k8s-base` and `apps`, through a StackReference |
| UDM SSH key, libvirt SSH identity | installed by automation on each side: the gateway's `AuthorizedKeys` keeps the UDM key on the device (physical/gateway.md §1.4), aconfmgr provisions the homelab host's dedicated service user together with its key (physical/homelab-host.md §4) | device-file push (host key pinned) / `virsh` as a `libvirt`-group user | Pulumi config secret; the UDM key's public half is a constant | `physical` |
| UniFi API key | Dedicated local admin | Network API | Pulumi config secret | `physical` |
| AdGuard API credentials | AdGuard admin (no scoped API — audit M6) | alice/bob rewrite API | Pulumi config secret | `dns` rewrites |
| ZeroTier Central API token | Made in the Central console (no token API) | The whole Central account: the installation's network, its members and its flow rules | Pulumi config secret (`zerotierApiToken`; the network id beside it is a constant in `conventions`, not a secret) | `physical` |
| GitHub admin token | Made in the GitHub UI (no token API) | This account's repositories — branch protection, rulesets, Environments and their gates: `repo`, the narrowest scope that covers them | Pulumi config secret (`githubAdminToken`) | `github`, and `credentials derived sync`, which pushes every GitHub secret as it |
| BGP session password | Drawn by the operator (no console makes it; `credentials derived bgp record` delivers it) | One BGP session, the gateway↔worker peering (cluster-infra.md §2): an MD5 password both ends are configured with | Pulumi config secret (`gatewayBgpPassword`) · device secret (the routing daemon's configuration) · SealedSecret (Cilium's `authSecretRef`; pending) | `physical`, which writes it onto the device; Cilium BGPv2 on the worker |
| Alertmanager read token | generated, escrowed as `alertmanager/read` | `GET /api/v2/alerts` only, by HTTPRoute method+path+header match | escrow · ops-repo secret (pending) · Pulumi config secret (the HTTPRoute's match, rendered with that route; pending) | Issue-sync poller |
| HA webhook URL/ID | Home Assistant | One notify endpoint | SealedSecret (pending) · ops-repo secret (pending) · `kluster` repository secret (`HAOS_DEPLOY_WEBHOOK_URL`, the interim deploy-failure channel, ci.md §3) | alertmanager, dispatch handler, the deploy chain's `notify-failure` job |
| Drill-environment credentials | OCI seed key and B2 seed key, one command (`credentials derived drill-credentials mint`) | The OCI key is its own user, group and policy: administrator of the `drill` compartment, which holds the drill's scratch box and nothing else, and a stranger outside it — no `--compartment` on this row, and no quota or budget guardrail on that compartment until `physical` declares one. The B2 key is `listFiles` and `readFiles` on the dump prefix alone, the writer's own prefix and nothing the writer may do | ops-repo Environment (`drill`: `DRILL_OCI_USER_OCID`, `DRILL_OCI_FINGERPRINT`, `DRILL_OCI_PRIVATE_KEY`, `DRILL_B2_KEY_ID`, `DRILL_B2_KEY`) | Quarterly rebuild drill (state-backend.md §7.3) |

Rows whose "From" is a seed rotate by re-running their subcommand.
Rows whose "From" is an escrow label rotate by generating the next
generation of that label (§4.2), which the row's own consumer then
adopts. Rows generated by a stack (Talos secrets, kubeconfig, overlay
identities) rotate with the resource that owns them. The client bundles
under the state-backend CA rotate by being issued again — `credentials
derived sync --only state-backend-certificates` for the `ci` one,
`state-backend bundle operator` for a workstation's — because their keys
are generated at issuance and kept nowhere. Nothing is retired by that:
the appliance authenticates the CA rather than a particular leaf, this
PKI has no revocation, and the certificate being replaced stays valid
until it expires. The drill age identity is the one row that rotates by
swap rather than by any of those, because its contract covers the newest
object rather than a retention window (state-backend.md §5):
`credentials derived drill-age-identity generate --rotate` overwrites
the Environment secret — with one slot, overwriting the old key *is*
deleting it — and the recipient on file; committing that file is drift
the plain converge names, so the adoption window is `state-backend
provision --force` followed by `restore` of the dump that run takes,
and the drill opens the first dump written after it. Between the
overwrite and that dump the drill cannot open the newest object, which
is the cost of a one-slot design and is bounded by one nightly.

**An ops-repo Environment secret's name carries its Environment as a
prefix** (`DRILL_AGE_IDENTITY`, and the five the drill-credentials row
holds). Inside a job an Environment secret shadows a repository
secret of the same name, and the ops repository holds both kinds —
the trigger App's key and the freshness key are repository secrets —
so the prefix is what keeps a workflow naming `drill` from silently
reading the drill's copy where it meant the repository's.

**A provider role is one shape.** The name a credential is minted under
and the whole of what it may do are a single value — `b2.Role`,
`cloudflare.Role` and `oci_iam.Identity` are that value in each
platform's own vocabulary: B2 capabilities and the bucket and prefix
they are confined to, Cloudflare permission groups, OCI policy
statements. A mint takes a role rather than a name plus whatever
permission constant is in scope, so a name cannot reach a mint without
its grant, and the **Scope** column above is the register's reading of
the same value. Which resources a role is granted over is not part of
it: the zones a token covers and the compartment a key administers are
this installation's to name at the mint, while the role is the same
wherever it is minted.

**A stack-generated row leaves Pulumi state only where something
outside Pulumi reads it.** The kubeconfig has no such reader: `k8s-base`
and `apps` take it from the `physical` stack through a StackReference, so
no workflow names a secret for it. The talosconfig has one — the hourly
etcd snapshot in the ops repository (ci.md §3) — and that reader is the
whole of its ops-repo secret. The ZeroTier CI identities are the third
shape: a job joins the overlay before it can reach anything the LAN
holds, and that join is a workflow step rather than something a program
does, which is why theirs is a CI Environment secret.

**The OCI rows are one mint and two slots.** Both are the same act — the
seed creates a user, the group that holds it and a policy confining that
group to one compartment, then mints the user's API key — and they differ
only in where the key is delivered, which follows from what consumes it.
`physical` is a program, so its key is a Pulumi config secret it reads
before it can run. `state-backend provision` is not: it is the command that
*builds* the backend every config secret is stored in, it runs from a
workstation at bring-up and at every rebuild, and it is never run by CI. Its
key is therefore a **workstation slot** (§4.4), for the reason the operator
bundle beside it is one: a file a non-interactive reader can be pointed at,
and one a command can write again, so losing it costs a command rather than
a credential.

Scope is a compartment rather than a list of verbs, which is what makes the
two rows independent. Each user administers its own compartment and is a
stranger everywhere else, so what a consumer may do widens by declaring a
resource in its own compartment rather than by editing a policy, and a
compromise of either key is confined to a boundary the console shows.

**The compartment is part of the mint, not a prerequisite of it.**
`conventions.OCI_TENANCY.compartments` names one per consumer, and the mint creates
the one the tenancy does not have yet — which is what the seed's `manage
compartments` statement is for: a boundary the platform's API can make must
not become a console errand (§1 rule 5). The mapping carries the name, which
is a decision, and the `OCID`, which is the site fact that follows from
creating it; a compartment created for the first time is announced as the
line to record there and commit, because the consuming stack reads the `OCID`
from that file and refuses by naming the mint until it is written. The
appliance's compartment predates the model and carries the installation's
own name rather than a per-consumer one, so the mint adopts it exactly as
it adopts a user or a group that is already there. `--compartment`
overrides the mapping for a drill tenancy, where none of those names mean
anything.

**Two rows are here for their delivery rather than their birth.** The
UDM SSH key and the libvirt identity are prerequisites rather than
credentials this side creates: rule 7 is satisfied for them by
automation rather than by a `credentials` subcommand — the gateway's
`AuthorizedKeys` component keeps the public half installed on the
device (physical/gateway.md §1.4), aconfmgr provisions the host's
service user — so the only act on this side is the paste into
`physical`'s config. Rotating either is generating the pair, putting
the public half where that automation reads it, and that paste.

**Some are made in the console that checks them.** The UniFi API
key and the AdGuard admin login belong to the appliances themselves: the
controller mints a key for a dedicated local admin and shows it once,
and AdGuard Home has no scoped API at all, so its admin account *is* the
API credential — the residual the security audit records as M6. Both
instances answer to the same login, because a rewrite is written to
alice and bob directly rather than synchronized (declarative/dns.md §3),
and that account lives in each instance's own `AdGuardHome.yaml`, state
the device keeps: the `physical` stack installs an initial state only
where an instance has never had one, and that initial state names no
account (physical/gateway.md §1.1). The ZeroTier Central token is the
same shape one layer out: Central publishes no token API, so an account
token made in its web console is what `physical` authenticates with,
as broad as the account it belongs to because Central offers nothing
narrower. **The GitHub admin token is the fourth**, and the same shape
again: GitHub publishes no API that creates a personal access token, so
one made on the account's own settings page is what the `github` stack
declares the forge with. It is a **classic** token scoped `repo`, which
is the narrowest that scope list offers, and the excess it carries
beyond branch protection, rulesets, Environments and their gates is the
row's own residual. A fine-grained token's `Administration` permission
would be narrower still, and the reason this row is not one is the ops
repository: a fine-grained token is scoped to repositories of a single
owner and this stack declares two, one of them private, so the classic
token is what covers the pair with one credential rather than two whose
rotations could drift apart. None of them is minted here, so
`credentials derived <row> record` (§4) is the delivery
alone: the console steps, the value, the stack config that reads it. The
consumer decides which stack — `physical` drives the UDM's Network API
and the overlay's Central account, `dns` writes the AdGuard rewrites,
`github` declares the forge — and nothing is recorded beside the token.
Which network the account administers here is an identity rather than a
setting, so it is a constant in `conventions`; the controller's own
address is not recorded either, because it is the overlay address the
roster assigns, stated once in `conventions` and derived everywhere it
is dialed; and the GitHub account the admin token administers is
`conventions.forge` for the same reason.

**Rotation is a console visit plus a re-run, and the program does no
half of the first.** Each is made again in the same console — a new
UniFi key on the admin's page, a new Central token, a new personal
access token on the tokens page — `credentials derived <row> record`
delivers it, the stack file is committed, and the superseded credential
is deleted in the same visit. What the command does is the delivery and
its proof: it encrypts the value into the stack's committed
configuration and decrypts it again to show the slot holds what was
handed over. Of the three steps a minted row's `mint` performs, it does
none — and the reasons differ, which matters because only one of them is
a wall. **Creating and retiring are API absences**: no endpoint of
these platforms makes a personal access token, a UniFi key, a Central
token or an AdGuard login, or deletes one, so a console visit is the
whole of both. **Verifying is a decision.** Any authenticated call is a
verification, and for the GitHub row a cheap one exists — a classic
token's scopes come back in the `X-OAuth-Scopes` header of any request —
so what stands in for it is chosen rather than forced: the first
`pulumi preview -s github` authenticates as the token, against the real
account, and shows what it would change, which is a stronger proof than
a scope string and is a step the operator takes anyway. If a `record`
that failed fast were ever worth more than that, this is the row where
adding the check is cheap, and this sentence is what says so. That the
platforms mint nothing is also why none of them is a seed: they
mint nothing, so there is nothing for the kit to hold or to reproduce.
What guarantees a lost one can be replaced is the account or appliance
behind it — the Central and GitHub accounts are among the account roots
§2 keeps out of the kit, and the two appliances are the installation's
own.

**The GitHub admin token is read back as well as read.** It is the one
row of this shape with a second consumer: `credentials derived sync`
authenticates to the forge as it before pushing any GitHub secret
(ci.md §3), and it takes it out of the `github` stack's configuration
rather than from a copy of its own. One credential, one home — which is
also why `sync` needs the state backend reachable and the passphrase in
hand, as every other command that reads a config secret does.

**Two more are made in a console and read by a workflow.** Each
single-purpose GitHub App has a private key generated on its own settings
page — disclosed once, and creatable by no API GitHub publishes — so the
key is recorded rather than minted, exactly like the three above.
It is not a seed for the same reason they are not: what a job makes from
it is an **installation token**, good for eight hours and used inside the
run that minted it, which is working material of a workflow rather than
anything this register stores. Where these two differ from the three
above is the slot. The consumer is a workflow rather than a stack, and
the key is a repository secret of the repository that workflow runs in,
pushed by `credentials derived sync` from the escrow copy (§2.2), which
stays the permanent store: an App key downloads once, and a lost slot
is a re-push rather than a console visit. `credentials derived sync
--only github-trigger-key` recovers the trigger key and pushes it as
`TRIGGER_APP_PRIVATE_KEY`, the name the ops repository's
`drift-trigger.yml` reads it under; `--only github-dispatch-key` pushes
the dispatch key as `DISPATCH_APP_PRIVATE_KEY`, a repository secret of
`kluster`, the name the alert producer `alert.yml` reads it under. A
repository secret rather than an Environment's because that job belongs
to no stack, and its exposure is the fence's: any same-repo branch can
read it, previews included, which buys a token that can post alerts and
write non-workflow files into the private ops repository, and nothing
else (cluster/architecture.md §4.3). Rotating one is another key on
that page, recorded here as the label's next generation, `sync --only`
for that row, and the superseded key deleted on the page in the same
visit. The client id the JWT is issued under travels
with the delivery rather than with the key: it identifies the App
instead of authenticating as it, and the App's page shows it for as long
as the App exists.

## 4. The scripts

The register's executable form: `credentials`, a console script in this
repo (`src/kluster/scripts/credentials/`). Every command reads
`credentials <subject> [<row>] <verb>`, and the subjects are the
register's own tiers: **`root`** for the account roots a workstation
borrows (§2), **`seed`** for §2's rows, **`kit`** for the offline store
and what is done to the whole of it (§2.1), and **`derived`** for §3's
rows. A row is named the same way everywhere — words joined by `-`, as
`oci-physical` — in the tree, in the slot map and in the tables here.

What differs between §3's rows is the verb, because what differs between
them is how the value comes into being: `mint` for a row a seed mints,
`generate` / `import` / `recover` for a row generated here and escrowed
(§2.2), and `record` for a row made in a console because no API of that
platform makes one — into the stack that authenticates with it, or into
the escrow where a row whose consumer does not exist yet rests. The escrow
*directory* keeps the `/` paths it files ciphertexts under
(`escrow/pulumi/passphrase/1.age`); only the command surface uses the row
name.

| Command | When |
| --- | --- |
| `credentials root <name> remember` | Once per machine and root, before a bring-up or a re-seed that needs it. Keeps one account root (§2) where its readers reach it. Skipping it costs a prompt, not a failure. |
| `credentials root ls` | Which roots this machine holds, and which layer of the chain each comes from. Prints no values. |
| `credentials root <name> forget` | Removes one root from the secret store and from its token file. |
| `credentials kit bootstrap` | Bring-up, from nothing or from a partial kit. Resumable: re-running skips what is already there. |
| `credentials kit bootstrap --only <member>` | One seed's row is gone from the kit. Creates that row alone; the rest of the kit is untouched. It is the walk above confined to one row, and the walk probes the kit and nothing else: a row the kit still holds is skipped, whatever has become of the credential behind it at the platform. A row that is present in the kit but dead at its platform is the next command's. `--only recovery` is also the repair path for a kit that predates the escrow: creating that row writes the recovery key (§2.2) into the kit and `escrow/RECIPIENTS` into the checkout. |
| `credentials seed <member> create` | The same single-row create, addressed by row rather than through `kit bootstrap`'s walk, and the form that takes `--entry` for a kit whose row sits somewhere else. It probes nothing: the row is written even when the kit already holds one — the entry's identifier and its secret are both replaced, and so is an attachment of the same name — which makes it the repair for a row that is present in the kit but dead at its platform: an OCI key or B2 key deleted at the platform, a Cloudflare token deleted in the dashboard. The recovery keypair is the exception: `seed recovery create` refuses a kit that already holds a recovery key, and a checkout that already holds `escrow/RECIPIENTS`, because every ciphertext opens with that one key and nothing else; replacing it deliberately is `credentials kit rotate` (§4.2), which re-wraps the escrow on its way. Every provider row holds its account against what `conventions` records before its first write: the OCI create, against the tenancy its account root names, ahead of the group, user, membership and policy it would otherwise leave standing there; the Cloudflare adopt, against the accounts the console-made token can see, while the operator is still on the dashboard page that fixes a token made in the wrong one; and the B2 create, against the account its master key authorizes as, before the seed every later B2 credential descends from exists. The recovery keypair is not a provider credential at all. |
| `credentials seed oci rotate` / `credentials seed b2 rotate` | One self-reproducing seed replaced **inside the kit that is open**: the seed mints its successor, the successor is verified, and the predecessor is retired. Each holds its account against `conventions` before any of it, because a rotation's other act is to delete every key of that name that is in the successor's way — run against an account this installation does not own, that is a sweep through somebody else's keys. `credentials kit rotate` (§4.2) is the whole-kit form, which writes a new database instead. The rows the platform cannot rotate have no such subcommand — they are console visits. |
| `credentials seed oci domain` | Once, on a kit written before the OCI row carried its identity domain (§4.3). Borrows the OCI account root; every rotation after it needs nothing but the kit. |
| `credentials derived oci-state-backend mint` | After the kit exists and **before** `state-backend provision`, which is the only thing that reads it. Mints the appliance's own user, group, policy and API key from the OCI seed into the workstation slot (§4.4), confined to the compartment `conventions` names for it. Before it creates anything, it refuses a seed that belongs to an account other than the one `conventions` records — this being the first place in a bring-up that check can fire. Re-running it rotates that key; a workstation that does not hold the kit cannot run it, and does not provision. |
| `state-backend provision` | After the kit and the appliance's key exist; every stack needs the backend before it can act. |
| `credentials derived pulumi-passphrase generate` | After the state backend exists. The state passphrase (§2.2) is generated, its ciphertext committed and its workstation slot (§4.4) written in one act, so `mise.toml` puts it into the environment of every later `pulumi` run and the backend URL comes from the bundle beside it — a `pulumi` command needs no prepared shell. The general form of this verb is below. |
| `credentials derived github-passphrase generate` | Before anything reads or writes the `github` stack's config, and once per installation. Generates that stack's own passphrase, commits its ciphertext and writes its workstation slot (§4.4) in one act — the same shape as the row above, differing in the one thing it exists for: it reaches no CI Environment, so nothing CI can start can read `Pulumi.github.yaml`. A second workstation runs `credentials derived github-passphrase recover` instead. Re-running `generate` files a *new* generation and does **not** re-encrypt the stack; rotating it is §4.2. |
| `credentials derived cloudflare-zones mint [--stack <name>]` | After the kit and the state backend exist. Mints the zone-scoped Cloudflare token (§3) from the seed and writes it into the `dns` stack's config, under the one key the stack reads; the stack file is then committed. The account the zones live in is not written beside it — that is `conventions.CLOUDFLARE_ACCOUNT`, and the mint holds the account it is about to mint in against it, before it creates anything. Re-running it rotates that token. It is the only row that takes a `--stack` (default `dns`): what each of the others mints is named after its row and its mint retires everything else of that name, so a delivery aimed at another stack would revoke the real one's live credential on the way to filling that stack's slot. |
| `credentials derived cloudflare-gateway-acme mint` | After the kit and the state backend exist. Mints the gateway's own ACME token (§3) from the same seed, scoped to the zones its vhosts are served under, and writes it into the `physical` stack's config secret; the stack file is then committed, and the stack writes the token onto the device. Which stack takes it is not a choice — the token is named after the row and minting retires every other token of that name. The account is held against `conventions.CLOUDFLARE_ACCOUNT` before the token is created, as it is for the zones row: the check belongs to the mint, so no row can be the one that forgets it. Re-running it rotates that token. |
| `credentials derived oci-physical mint` | After the state backend exists. The same mint for the `physical` stack, into that stack's config secrets; the stack file is then committed. It also creates that stack's compartment where the tenancy has none, and prints the `OCID` to record in `conventions` and commit. Before it creates anything, it refuses a seed that belongs to an account other than the one `conventions` records. |
| `credentials derived b2-management mint` | After the state backend exists. Mints the B2 management key (§3) from the B2 seed into the `physical` stack's config secret. Before it creates anything, it refuses a seed that authorizes as an account other than the one `conventions` records. Re-running it rotates that key and retires the one it replaces. |
| `credentials derived b2-freshness-dumps mint` | After the state backend exists — its bucket is what confines the key, and the key is proven by listing the dump prefix as itself. Mints the freshness probe's list-only B2 key (§3) and pushes both halves into the ops repository as repository secrets, as the GitHub admin token, each verified through the listing before the key it supersedes is retired. Re-running it rotates the key. |
| `credentials derived drill-credentials mint [--only <half>]` | After the state backend exists — its bucket is what confines the B2 key, and the key is proven by listing the dump prefix as itself — and beside the drill age identity, which the same Environment holds. Mints the rebuild drill's two provider keys (§3) and pushes their five carriers into the ops repository's `drill` Environment as the GitHub admin token, each verified through the listing before the key it supersedes is retired. The OCI half creates the `drill` compartment where the tenancy has none and prints the `OCID` to record in `conventions` and commit; it takes no `--compartment`, because the drill compartment is a recorded name in the recorded tenancy and the mint is held to it. Re-running it rotates both keys; `--only oci` or `--only b2` rotates one. |
| `credentials derived unifi record` | After the state backend exists, and after the controller has minted a key for its dedicated local admin — which the command prints the steps for. Takes the key without echoing it, into the `physical` stack's config; the stack file is then committed. The controller's address is not recorded beside it, being the overlay address `conventions` assigns. Re-running it is how a replaced key is delivered. |
| `credentials derived adguard record` | The same, for the admin login both AdGuard instances answer to, into the `dns` stack's config — the stack that writes the split-horizon rewrites. |
| `credentials derived zerotier record` | The same again, for the ZeroTier Central API token, into the `physical` stack's config — which network of that account is this installation's overlay is a constant in `conventions` rather than a value recorded beside the token. Central publishes no token API, so a token created in its web console and re-recorded here is the whole of a rotation; the superseded one is deleted in the same console. |
| `credentials derived github-admin record` | Once per installation, and again on each rotation, for the GitHub admin token — into the `github` stack's config, which is where both the stack and `credentials derived sync` read it. Nothing in this repository can create the value: GitHub publishes no API that makes a personal access token, so a token generated on the account's settings page and recorded here is the whole of a rotation, and the superseded one is deleted on the same page. It runs before `derived sync`, which authenticates as it. |
| `credentials derived github-dispatch-key record` / `credentials derived github-trigger-key record` | After the kit exists, and after the App's page has generated a private key — which the command prints the steps for. Takes the key on standard input and escrows it as the row's next generation, so a re-run with a key already on file changes nothing and a re-run with a fresh one is the rotation. `--from-kit` reads it out of the entry a kit that still carries the key as a seed row holds, instead of from standard input. |
| `credentials derived ls` | Any time, with or without a kit. Prints the slot map (below): every §3 credential, where its value comes from, and every slot it lands in, the ones still waiting on a consumer included. It reads a checked-in file, so it needs no token, no kit and no network. |
| `credentials derived sync [--only <row>] [--bundle-dir <path>]` | Once during bring-up, and again whenever one of those values moves or a slot is lost. Copies into their GitHub secrets the rows whose value lives somewhere else — read back out of a stack's state, recovered from the escrow, or typed in because the slot is its only storage — resolve, push, verify, per row. A row born into its slot is out of scope and is passed over; naming one is refused, pointing at the `mint` that owns it. `--only` addresses one row, and is what replaces a value that was typed in. |
| `credentials derived <row> recover [--generation <n>] [--stdout]` | Reading an escrowed secret back out. `derived pulumi-passphrase recover` is the common one: it fills the passphrase slot (§4.4) so `mise.toml` finds it and a local preview needs no offline database; `--stdout` prints instead of writing, for a pipe into another machine. `--generation` opens an older one — the certificate issued under a superseded CA, the dump written under a superseded age identity — where the default is the newest. |
| `state-backend bundle operator --address <ip> [--directory <path>]` | Once per workstation, or after a certificate reissue. Writes the client bundle into its slot; `state-backend provision` ends by doing the same thing. `--directory` writes it somewhere else instead — a second checkout, or a directory being staged for another machine — and the default is the slot. |
| `credentials derived <row> generate` | Rotating one escrowed credential (§4.2). Generates a new value, commits its ciphertext as the row's next generation and writes it into a workstation slot where the row has one — the two passphrases do, and a row without one reaches its consumer through that consumer's own procedure (§4.2). One act, no other row touched. |
| `credentials derived <row> import [--from-slot]` | Escrows a value that already exists as the row's next generation, changing nothing a consumer holds (§4.2). The value comes from standard input, or from the row's workstation slot with `--from-slot` — which is how a passphrase already sitting in `.credentials/` is escrowed without being copied through a shell. Refuses an empty or wrong-shaped value: a pipe whose producer failed dies here, not at the recovery that trusted the ciphertext. |
| `credentials kit rotate --into <new kit>` | Rotation (§4.2). Writes a new database and re-wraps the escrow to the successor recovery key in the same run; the retired one stays. A run that stopped part way is resumed by running it again with the same `--into`: the successor is opened rather than created, and each row it holds is finished rather than rotated twice. |
| `credentials kit rewrap` | The re-wrap on its own, for a recipients file edited by hand: it takes no recipients, re-encrypts every generation to whatever `escrow/RECIPIENTS` already names, and refuses a run that no identity in hand could open afterward. It opens with the one key the kit holds, so a rotation interrupted part way is not its case (§4.2: that is `kit rotate --into` the same file, again); an ordinary kit rotation never calls it. |
| `credentials derived check` | Any time, kit or no kit: every escrowed row the register names is present, generations run from 1 with no gap, every ciphertext is an ASCII-armored age file, `escrow/RECIPIENTS` holds age recipients, nothing is escrowed under a label the register does not name, and no stray file sits in the directory. It opens nothing, so a clone is enough to run it — which is what would let CI run it, though no workflow does today. |
| `credentials kit ls [<group>]` / `show <entry>` | Looking without changing. `ls` prints entry paths and reads no field, so it can disclose nothing; a group narrows it to one branch of the kit (`seeds`), where the default is the whole of it. `show` prints one entry's non-secret fields. |
| `credentials kit password remember` | Once per machine, so a run that lasts minutes is not guarded by a password typed into it. The password is proven against the kit before it is stored, keyed by the kit's resolved path — a kit reached by a new path needs one re-run. |
| `credentials kit password forget` | Drops that remembered password again, for a machine that should stop holding it. |

`credentials --help` carries the same ordering, because a command list
shaped like the register answers neither "where do I start" nor "which
of these destroys something". Every minting subcommand is **account
check → mint → push to every slot → verify → retire the predecessor**,
and therefore idempotent: rotation is a re-run, not a second procedure.
The first step is a precondition rather than a courtesy: a seed
belonging to an account this installation does not own is knowable
before anything is created — from the seed's own row for OCI, from the
zone listing for Cloudflare, from what `b2_authorize_account` answers
with for B2 — and a run that discovered it afterward would refuse while
leaving a live credential behind in that account, recorded in no
register and known to nobody who could revoke it. Every platform this
program mints from records its account in `conventions`, so no minting
subcommand is without that first step. An installation that has recorded no account
for a platform is refused rather than waved through, with the identifier
to record named in the refusal: a check skipped where the fact is missing
is skipped exactly where nothing has ever pinned the account down.

**The last step is last for the mirror image of that reason**, and what
it buys is one property: *at no point does the only copy of a working
credential exist solely in this process.* Between the mint and the push
the new credential exists here and nowhere else — Cloudflare and B2
disclose a value once, at creation, and an OCI private key is generated
on this machine and never returned by the service. A predecessor retired
there would mean that a push which then failed — an unreachable backend,
an absent passphrase slot, a read-back that does not match — ends with
the slot naming a revoked credential while the one that works is gone
with the process. Retired after the push instead, that failure costs a
re-run and nothing else: the predecessor is still live, and one extra
credential of its name stands at the provider. Nothing has to record
that stray, because every retirement here deletes *everything of that
name except the one in hand* rather than one recorded predecessor — so
the next run of the row that reaches its push clears whatever the failed
ones left, and re-running the row is both the repair and the rotation.
Every platform puts some ceiling on how many credentials of a name an
account may hold, and OCI's is the one low enough to be reached by a
handful of failed runs: a user holds `oci_iam.KEY_QUOTA` API keys, so
the run that would exceed it stops and lists what the user is holding
instead of minting past it. **Which of them to keep is a question that
refusal answers only where it can**, and the two answers are different
messages.

**For a derived row, and for a seed create, it cannot.** A derived
credential is in a slot this program never reads, delivered there as a
secret; a seed create has nothing in hand to compare against, because the
row it is about to write is the first thing that would hold a private
half. Either way nothing here picks the live key out of the strays. That
refusal says so, and names the two moves that are safe without knowing —
delete all but one and re-run, or delete all of them and re-run, since
the re-run mints a fresh key and writes it down either way.

For a **seed rotation** it can, and must. The key at stake is the one
the kit holds the private half of and the one the command is signing
with, so that refusal names it as the key to keep and lists the rest as
the ones to delete. Offering the delete-everything move here would be
inviting the operator to spend the seed's own key, after which the row
comes back only from the account root — through **`credentials seed oci
create`**, not `kit bootstrap --only oci`: the kit still holds the row
and only its key at OCI is gone, and the walk skips what the kit already
has, where the single-row create probes nothing and overwrites both
halves.

That refusal is also the one a sweep that could not make room produces,
which usually means the row predates the identity-domain attribute
retirement goes through — the state §4.3 describes. So it points at
`credentials seed oci domain` unconditionally rather than only where the
console refuses too: an operator whose console deletes the strays by
hand has cleared the symptom and will strand another key on the next
rotation.

The slot map (below) does not drive those pushes; it records where they
land, naming each config key by importing it from the code that writes
it, so the two cannot say different things. `generate` keeps that
shape — **generate → escrow → push → verify** — and drops the idempotence
deliberately: a re-run produces a new generation, which is exactly what
rotating that row means.

Two global options sit in front of every subject, because both defaults
are per-checkout rather than universal: `--kdbx` names the kit
(otherwise `$KLUSTER_KDBX`, otherwise the workstation slot §4.4 names),
and `--escrow` names the registry directory (otherwise this checkout's
`escrow/`). A kit on removable media and a registry in a second clone
are the two cases they exist for.

A third option, `--bundle-dir`, is on every command that delivers into a
stack's configuration rather than in front of the subject — each `mint`
that writes a config secret, each `record`, and `derived sync`. A stack's
configuration lives in the state backend, so pushing into it means
reaching the backend, which is the client bundle plus the passphrase the
kit recovers; the flag says which bundle, and the default is the
workstation slot (§4.4) `state-backend bundle operator` writes.
`derived oci-state-backend mint` is the one mint without it, because its
value goes into a workstation slot and there is no backend yet to reach.

§3's minted rows are `credentials derived <row> mint`, and its escrowed
rows are `credentials derived <row> generate`. A row is implemented when
its consumer exists: minting a credential
that has no slot to be delivered into would park a secret, which rule 2
forbids. Five are delivered today — the zones token, the gateway's ACME
token, the two OCI keys and the B2 management key; the DNS-01 token
joins them with cert-manager. The GitHub-secret half of a row is
delivered separately, by `credentials derived sync` rather than by the
row's own command, for the rows whose value can be obtained without
minting one (below).

§3's **device rows** are neither minted nor escrowed, so they are
`credentials derived <row> record`: the command prints the console
steps that create the credential, takes the value without echoing it,
and pushes it into the config of the stack that reads it, proven by
reading it back like every other config secret. A value may be handed in
instead of typed, which is what makes a scripted run possible — a secret
as a *path* and never as an argument, because an argument would put the
credential in the process table of a shared machine, with `-` reading
standard input. The console steps live beside the row (`devices.py`) for
the reason §2's live beside theirs: a runbook would be a second place
for them to be wrong.

The **App-key rows** carry the same verb one slot over (`escrow.py`):
console steps printed, the value taken on standard input, and the escrow
generation written in place of a stack push, because no stack
authenticates with either key. What the command adds there is a probe —
it opens the registry and compares before it files anything, so a value
already escrowed produces no second generation of itself. That is what
makes moving a key out of a kit re-runnable, and it is the same
discipline as the rest of §4.1: whether the work is done is answered by
looking at the product, never by a note saying a command ran.

The zones token's scope is not a list in the script: it is the
installation's zones as `conventions` names them, resolved to zone ids
through the seed at mint time, so adding a zone there and re-running the
command is the whole procedure for widening it. The push writes one key,
`cloudflareApiToken`, as a config secret. It is unqualified, which
`pulumi config set` and `pulumi.Config()` both resolve against the
project's own name — so the committed file reads
`kluster-py:cloudflareApiToken`, and the project name lives in
`Pulumi.yaml` rather than a second time in the script. The provider
package's own `cloudflare:` namespace holds nothing, for the reason
`oci:` and `b2:` hold nothing: the stack builds its provider from this
value rather than being handed one by ambient configuration, so the key
belongs to the program that reads it.

Which account those zones live in travels with neither the credential
nor the configuration. It names the account rather than opening it, so
it is code (`conventions.CLOUDFLARE_ACCOUNT`) and the stack declares
every zone against it. The mint resolves that same identifier from the
seed while resolving the zone ids, which writes nothing, and holds it
against the recorded one before it creates the token — naming both
accounts, because which of the two is stale is the operator's question.
That is what catches a kit re-seeded from another Cloudflare account,
and catching it there costs nothing: every Cloudflare mint is held to
it, so the gateway's ACME token cannot be the row that forgets.

An OCI key is pushed the same way and fills more keys, because an API key
is several things (§2.1) and a provider recovers none of them: it writes
`ociUserOcid`, `ociFingerprint` and `ociPrivateKey`, bare and therefore in
the project's own namespace like the zones token above. Those three are the
whole of the push, and all three are config secrets — the key, the
fingerprint, and the identifier naming the user the key signs as, which is
the class of fact the kit itself keeps as a protected attribute (§2.1). The
fingerprint is written although §2.1 declines to store one: the provider
takes it as an input rather than deriving it, and the command computes it
from the key it is pushing in the same breath, so the two cannot disagree.

Which account the key acts in, and where inside it, travels with neither
the credential nor the configuration. The tenancy OCID names the account
rather than authenticating to it, the region is permanent per account, and
the compartment is a boundary this program decides, so all three are code
(`conventions.OCI_TENANCY`) and the stack reads them there — at the one
line that builds the cloud provider, beside the three secrets above
([rfc-002](rfc/rfc-002-src-layout-and-the-gateway.md) §8.1, §10.3). A
fact with one home is not copied into a second, so the mint proves it
instead: before it creates anything — the compartment included — it
holds the account the seed belongs to against the recorded one and
refuses on a mismatch, naming both. A run given `--compartment` is
pointed at a drill tenancy and is not held to it, for the reason the
compartment lookup is not. It is the same shape the Cloudflare row has.
The provider's own `oci:` namespace holds nothing: with default providers
disabled there is no ambient configuration left for it to carry, and the
same is true of `b2:`, whose two keys are pushed as
`b2ApplicationKeyId` and `b2ApplicationKey`.

**The slot map is checked in** (`slots.py`). One row per §3 credential,
naming the source its value comes from — recovered from escrow, minted by
the row's own command, read out of a stack, or typed in — and every slot
it lands in, spelled as the closed set of channels §1 rule 6 lists:
GitHub secret (repository, Environment, name), Pulumi config secret (per
stack and key), Pulumi state, escrow ciphertext, SealedSecret, on-box,
workstation slot, device secret. The CI Environment secret, the
ops-repo secret and the `kluster` repository secret are one channel there,
differing in which repository they name and whether they name an
Environment. The Pulumi config channel is the **secret** one alone:
rule 6 names no plain committed key, so a value that file may carry in
the clear — an identifier naming an account rather than authenticating
to it — is a constant in `conventions` and no row of this map, which is
where the Cloudflare account identifier and the tenancy OCID both went.

§3 stays the human-readable view and the map is the machine-readable one,
and a test reads this document and holds the two equal — so a credential
in one and not the other fails a check rather than going unnoticed. A slot
the register promises and nothing has given a name yet — an Environment
secret no workflow reads, a SealedSecret with no manifest — is recorded on
the row as what it is waiting on, rather than as an invented name a future
workflow would have to guess right. Each reason is filed under the channel
it is about, in the same vocabulary the Slot column is written in, so the
qualifier there and the reason here name one slot rather than two.

**The GitHub secrets are filled by a `credentials` command run from the
workstation.** Besides the Pulumi config secret, that is the only channel
with a sink today. Deliberately not the `github` stack: that stack
declares the *structure* — which repositories exist, which Environments,
which of them a reviewer gates — and is applied by hand a few times a
year, while these values rotate on their own cadence and some are
generated in state after it last ran, so a stack cannot push what did not
exist when it was applied. Deliberately not CI either: a workflow holding
the credential that writes its own Environment's secrets can rewrite the
partition confining it, which is the one property that partition exists to
have (ci.md §3). The push shells out to `gh secret set`, because the API
takes a secret as a sealed box and `gh` already implements that exchange —
the alternative being handwritten cryptographic primitives for one call
site.

**Verification stops where the API does.** A pushed secret is never
disclosed again, so what a run checks is that the name is in the listing
and its timestamp moved. That distinguishes a delivered secret from a
refused one, which is the failure worth guarding against; nothing on this
channel can distinguish a correct value from a corrupted one.

One piece is designed and not built (`kluster-ops#1`), and is described
here because the rest of the register is written against it:

-   **Slot-drift probe**: an ops-repo scheduled workflow comparing the
    slot map against reality in both directions — `gh` secret listings
    and `pulumi config` keys. A live slot with no map entry, or a map
    entry with no live slot, would raise an `actionable` alert. This
    (plus the expiry/destroy-date tripwires, operations.md §4) is what
    replaces the calendar register-review.

### 4.1 Bring-up

Bring-up is a sequence of commands rather than one command: each stage
leaves behind the artifact the next one reads, and each is separately
re-runnable. `credentials --help` prints the same order.

**One database is opened: the kit.** The account roots the minters
borrow are not in a database this repository reads — they come from the
chain in §2: the **desktop secret store**, a token file, an environment
variable, or a prompt where a machine has none of them. The kit's own
master password is asked of the same store first
(`credentials kit password remember` puts it there),
because a run that then goes on for minutes should not be guarded by a
password typed into a process nobody is watching. Nothing is ever
written to the store implicitly: a `remember` command is the only thing
that puts a value there.

1.  `credentials root <name> remember` — once per machine and root,
    for the account roots the mints borrow (§2). Skipping it costs a
    prompt rather than a failure, which is also how a headless run works.
2.  `credentials kit bootstrap` — fills the kit with every §2 row, the
    recovery key included, creating the kit if it is absent. A row
    whose platform can mint it is minted; the rest stop and print their
    console steps. The kit is all it writes secrets to; the recovery
    row additionally writes `escrow/RECIPIENTS` into the checkout — the
    public half, and a file to commit.
3.  `credentials derived oci-state-backend mint` — the appliance's own
    OCI key (§3), minted from the seed into the workstation slot the next
    stage reads. It comes first among §3's rows because it is the only one
    whose consumer runs before the state backend exists. The compartment
    it is confined to is the one `conventions` names for the appliance,
    adopted where it exists and created where it does not.
4.  `state-backend provision` — the Pulumi state backend, which every
    stack needs before it can act, and the first thing to escrow (§2.2):
    it generates the CA and the age identity, commits their ciphertexts,
    and the appliance's Ignition carries what is public about them — the
    server certificate issued under that CA and the age identity's public
    half — plus a B2 dump key minted from the B2 seed. The run ends by
    writing the `operator` client bundle into its workstation slot (§4.4).
5.  `credentials derived pulumi-passphrase generate` — the one escrowed
    row no stage above mints, because it has no single installer: the
    state backend owns the CA and the backup identities and generates
    them in the run that installs them, while the state passphrase
    belongs to every stack and to none of them. The command writes the
    workstation slot (§4.4) as well as the ciphertext, and that slot is
    what a `pulumi` run reads from here on: `mise.toml` puts the
    passphrase and the bundle's `PULUMI_BACKEND_URL` into the
    environment of every later run, so no stage below prepares a shell.
    A second workstation that holds the kit fills the same slot once with
    `credentials derived pulumi-passphrase recover`, and one that does not
    hold the kit gets it in the copied `.credentials/` directory (§4.4).
    A kit that predates the escrow carries a live passphrase already and
    uses `import` here instead (§4.2), which escrows that value rather
    than replacing it.
6.  `credentials derived cloudflare-zones mint`,
    `credentials derived oci-physical mint` and
    `credentials derived b2-management mint` — the §3 rows whose slot is a
    stack's committed configuration, which is then committed. One row per
    command, and re-running one rotates that row. The OCI row creates the
    `physical` stack's compartment on its first run and prints the `OCID`,
    which is recorded in `conventions` and committed with the rest.
7.  `credentials derived github-passphrase generate` — the `github`
    stack's own passphrase, before anything reads or writes that stack's
    config. It is stage 5's command on a second row, and it is separate
    from stage 5 for the one reason the row exists: this value goes to no
    Environment, so the stack whose config carries the forge's admin token
    is unreadable by anything CI can start (§1 rule 6). A machine that
    skips it does not silently fall back to the estate passphrase —
    every command that would touch that stack refuses by name.
8.  `credentials derived unifi record`,
    `credentials derived adguard record`,
    `credentials derived zerotier record`,
    `credentials derived bgp record` and
    `credentials derived github-admin record` — the §3 rows whose
    credential is made in the console that checks it — or, for the BGP
    session password, drawn by the operator, no console making one —
    rather than minted here. Each prints the steps that create it, takes
    the value, and writes it into the config of the stack that reads it,
    which is then committed like the rows above. The GitHub one is last of these
    because stage 10 authenticates as it, and it needs stage 7 to have
    run.
9.  `credentials derived github-dispatch-key record` and
    `credentials derived github-trigger-key record` — the two GitHub App
    private keys, each generated on its own App's settings page and
    escrowed here. No stack authenticates with either, so the ciphertext
    is a file to commit; stage 10 pushes each into the repository secret
    its workflow reads (§3).
10. `credentials derived sync` — the GitHub secrets CI reads, for the §3
    rows whose value lives somewhere else (§4). Last, because a row read
    out of a stack needs that stack to have run; a row it cannot fill yet
    says which slot is waiting on what, and the same command run again
    fills it. It authenticates as the GitHub admin token stage 8
    recorded, read back out of the `github` stack's configuration, so a
    run before that stage refuses by naming the command that fills it —
    and before stage 7, by naming the passphrase that opens it. It
    pushes the estate passphrase into every Environment and the `github`
    one into none, which is the partition ci.md §3 rests on.

A stage that fails is re-run; nothing is parked. Once the last one is
done, the kit goes back in its envelope.

**What is not built yet** (`kluster-ops#1`): the §3 rows below. Three slot
kinds have a sink — the Pulumi config secret, the workstation slot and the
GitHub secret — and the rest have none.

-   The **SSH identities** (the UDM key and the libvirt identity) have no
    command on this side. Neither is created in a console, so there are
    no steps to print: the installation's other automation installs them
    (§3), and what is left here is a paste into `physical`'s
    configuration. The **in-cluster secrets** (the DNS-01 token and the
    writer keys, sealable only once `k8s-base` has the sealed-secrets
    controller up) have neither half.
-   Part of the **CI Environment half** (ci.md §3). The sink exists (§4)
    and fills what a workstation can obtain: the state passphrase and the
    `ci` client bundle, both into every Environment, the dispatch App's
    key as a repository secret, and the deploy-failure webhook, which is
    typed in. What is left waits on
    something other than the sink — the ZeroTier CI identities, on the
    `physical` stack that generates them.
-   The **ops-repo channel** has three rows that land through the sink
    (§4): the drill age identity's private half, which its generator
    pushes, and the drill's OCI and B2 keys, which their mint pushes as
    five carriers, both in the `drill` Environment; and the trigger App's
    key, which stage 10 pushes as a repository secret. Every other row
    above naming an ops-repo secret lands nowhere, because the workflow
    that would read it is not built — nothing there names a secret for
    it, and nothing there reads the drill Environment yet either
    (state-backend.md §7.3).
-   **`alertmanager/read`** is generated and escrowed, and what it lacks
    is a consumer: neither the issue-sync poller nor the HTTPRoute that
    matches its header exists, so the ops-repo secret and the config
    secret that route is rendered from have nowhere to land, and the
    escrow copy is the only slot the row has today. That is not the
    parking rule 2 forbids — §2.2's register expects this label, so
    `derived check` reports its absence as a problem — and the token on
    file is the value those two consumers will be built around rather
    than one they replace.
-   The **slot-drift probe** (§4). The map it would read is checked in;
    the scheduled workflow that compares it against reality is not.

Restic passwords will not join that list: they arrive with the
`backed_pvc` helper (declarative/workloads.md §3), which is itself
unwritten but generates its own password into state and seals it, so a
new volume will need no `credentials` run (rule 6). Until the commands
above exist, a bring-up delivers the seed kit, the state backend, the
provider credentials the `dns` and `physical` stacks run on, the three
console-made credentials those stacks authenticate with, the two App
keys into the escrow, and the GitHub secrets whose values a workstation
can obtain; the rest of §3 is design rather than procedure.

**Resumable by probing, not by bookkeeping.** `kit bootstrap` asks whether
each row is already in the kit and skips it if so, so an interrupted run
is resumed by re-running the same command, and `--only <member>` is that
walk confined to one row — the repair for a row the kit has lost. What
it probes is the kit: a row the kit still holds is skipped whatever has
become of the credential behind it at its platform, and that state — a
provider seed's key deleted in a console, say — is repaired by
`credentials seed <member> create`, which probes nothing and overwrites
a provider row (§4's table; the recovery key has no platform to die at,
and is the one row that command refuses to overwrite). `state-backend
provision` compares the running appliance against the repository the
same way. A checkpoint file would record "this ran" instead — which
stops being true the moment a row is deleted out of the kit — and the
run after that would skip the repair.

**The console steps live in the register, not in a runbook.** Each §2
row that no API can create carries the instructions for creating it
(`entries.py`), so `kit bootstrap` prints them at the moment it stops
rather than sending the operator to look for a document.

### 4.2 `credentials kit rotate`

Two rotations live here, and the model's point is that they are
independent: rotating **the kit** (this command) touches no production
value, and rotating **one credential** (`credentials derived <row>
generate`, below) touches no other credential.

`--only <member>` re-runs one row; the default rotates the whole kit.
It **writes a new database file** (`--into`), and the retired one
is left byte-for-byte as it was: unseal the old, have each seed mint its
successor, generate a fresh recovery key, and write all of it into
the new database. The Cloudflare seed token is an explicit pause — the
script prints the console steps and waits. `--into` is created when it
is absent and opened when it exists, and an interrupted run is resumed
by running the same command again (the resume rule below).

**The recovery row's rotation is the re-encryption.** Rotating that row
writes the successor identity into the new database and then, in the
same run, opens every generation in the registry — with either identity,
so an interrupted run resumes — and writes each back encrypted to the
successor's recipient, `escrow/RECIPIENTS` last so that the file names
what the directory actually holds. No plaintext changes, so no consumer
is touched, no stack is re-encrypted and no appliance is re-provisioned
— that commit and the new database are the whole of a kit rotation.
`credentials kit rewrap` is the standalone form of the same
re-encryption and takes no recipients: it re-encrypts to whatever
`escrow/RECIPIENTS` already names, and it refuses outright a run that
would leave the registry with nothing in hand able to open it. Its job
is a recipients file edited by hand — a custodian's recipient added
beside the one in hand — while every ciphertext is still under the key
the kit holds. It opens with that one key, so it does not finish a
rotation that stopped part way through: from the retired kit the
ciphertexts already under the successor do not open, and from the
successor `escrow/RECIPIENTS` still names the retired key, which is
written last. That run is `kit rotate --into` the same file, again.

**What a run proves is that the new kit's seeds work, one row at a
time.** A seed that mints its own successor (OCI, B2) authenticates *as*
that successor before the predecessor's key is retired, so a run
interrupted anywhere leaves a working seed in one kit or the other; the
console-made Cloudflare token is checked for both permissions the seed
needs (§2), and for seeing the account `conventions` records among the
zones it can list, while the operator is still on the page that fixes
any of it.
No row in the walk is merely pasted in, which is what makes the run's
success mean the successor kit works.

**Every account refusal is raised before the walk starts, and a new
successor file is made after them.** The pre-flight
(`lifecycle.prove_account`) is each self-reproducing seed's account
check — what its own rotation holds against `conventions` before its
first write (the `seed oci rotate` / `seed b2 rotate` row of the table
in §4) — made for every row before any row rotates, and nothing else.
What a check takes is the platform's business: the OCI tenancy is
stored on the row and read from it, while the B2 account is knowable
only by authorizing as the seed, so that check also meets a B2 key that
no longer authenticates, and the network. Neither of those is what the
pre-flight is for, and what falls outside its definition lands at its
row as it always did: a dead OCI seed key is met by the OCI row's own
listing, after the recovery row has re-wrapped the escrow; its repair
is `credentials --kdbx <successor> seed oci create`, which mints a
fresh seed into the successor from the account root, and the same
`kit rotate --into` then resumes past the row. A refusal the pre-flight
raises costs nothing: no predecessor is retired, no row is written, no
`--into` file is made, no console visit has been asked for, and no
second run is owed. A refusal landing after an earlier row had retired
its predecessor would cost all of those, and ordering the walk instead
would not do, because each row's check sits directly above its own
retirement, so whichever row went first would still have retired before
the next row's check ran. Each check reads the row's **live**
credential (`lifecycle.live`): the successor's row where the successor
already holds a complete one, the retired kit's otherwise — a resumed
run's B2 row in the retired kit is a key the account no longer accepts
once the first run retired it, and a check that read it there would
refuse a sound resume.
**A seed family that mints its own successor adds its account
check to the pre-flight** as well as to its own rotation, and until it
does, a kit rotation refuses it by name. A row with no account check
is left where it is: a console-made token does not exist until the
walk reaches its row, so one the dashboard made wrong is refused there,
after the rows before it have rotated — and, because the operator is
on the page that fixes it, asked for again rather than raised — an
empty paste included. Ctrl-C or end of input at that prompt stops the
run, and the refusal that stops it says which rows the successor
holds, that their predecessors in the retired kit no longer work, that
the console row and every row after it are not rotated, and that the
same command with the same `--into` resumes at that row.

**An interrupted rotation is resumed by running the same command
again.** Nothing records which row ran: the successor is probed the way
`bootstrap` probes the kit it fills, and where a row is there — its
own reader would succeed on it (`lifecycle.holds`); a row missing any
part is treated as absent and written over — the arm finishes that
row's retirement rather than minting again. The recovery arm re-wraps
to the key the successor holds, opening every ciphertext with that key
or the retired one, so a registry half under each converges; the OCI
and B2 arms authorize as the successor's key and retire every other key
of the seed's name, the predecessor among them; the Cloudflare arm
verifies the stored token again — the same checks a paste gets — and
writes the same row back without a paste; a manual row is skipped. A
completed rotation run again is therefore a no-op at every platform and
reports every row; the re-wrap re-encrypts every ciphertext on every
pass, so a resume shows a diff on files whose plaintext did not change.
The retired kit is read and never written, and a successor row whose
credential the platform refuses is refused at its row, not healed: the
repair is `credentials --kdbx <successor> seed oci create` or
`credentials --kdbx <successor> seed cloudflare create`, into the
successor, after which the same `kit rotate --into` resumes past it.

**The successor file names its predecessor** — the rule that makes an
existing `--into` safe to open. A successor `rotate` creates records,
in the KDBX file's own `DatabaseDescription`, `kluster: successor of
<uuid>`, where `<uuid>` is the predecessor's root-group UUID
(`KdbxStore.uuid`): a value a copy of a file shares and a file
`KdbxStore.create` made does not, since `create` gives each new
database a UUID of its own. The marker is written once, at creation
and before any row, and an existing `--into` is refused unless its
marker names the kit in hand: that refuses the kit itself and any copy
of it (one database identity), a kit `bootstrap` wrote (no marker), and
a successor of some other kit — an older retired kit of this estate
among them, whose rows name the same principals, whose keys are dead
at every platform, and whose recovery row would otherwise be reused
by the re-wrap as the new recovery key. Escrow content cannot tell
that last case from a successor that died before its first ciphertext
(both open nothing yet), and a path comparison cannot see a copy; the
marker can.
It is lineage and not a checkpoint: it says what the file *is*, which
stays true whatever rows are later deleted from it, and it is checked
against the predecessor rather than believed. A file that exists and
carries no marker is refused by name, and the refusal says how to tell
the two things it can be apart: one that `kit ls` shows empty is a
successor that died before its marker was written and is deleted by
hand; one that shows rows is not this kit's successor.

Nothing beyond the kit is touched. The §3 credentials minted from
the retired seeds keep working, and each is replaced by re-running its
own command against the new kit: rotation neither re-mints them nor
inspects a slot, and the map §4 carries is read by the pushes rather
than by this command. The escrowed credentials keep
working for a stronger reason — their values are unchanged, only their
wrapping is.

**The retired kit is destroyable once nothing is owed to it**: every
generation in the registry opens under the successor's identity,
`credentials derived check` passes, and every seed the retired database
holds has a live successor. That is the whole criterion — a property to
verify, not a date to wait out — and the rotation run is what
establishes the first part of it.

**Rotating one credential is a generation, not a kit.** `credentials
derived <row> generate` produces a new random value, writes it to the
slot §3 names where the row has one and commits its ciphertext as the
row's next generation; adopting it is that one consumer's business — a
re-provision for the state-backend CA, a re-seal or a secret update for
the Alertmanager token, the recipient swap that state-backend.md §5
describes for an age generation. The recovery key is not involved,
and no other row moves.

**A passphrase is the row whose consumer is a file in this repository**,
and adopting a new generation of one is a re-encryption rather than a
restart. `credentials derived github-passphrase generate` files the next
generation and writes the slot; what it does *not* do is rewrite
`Pulumi.github.yaml`, which is still encrypted under the predecessor and
whose `encryptionsalt` still verifies that one. The adoption is
`pulumi stack change-secrets-provider passphrase -s github`, run with the
predecessor in `PULUMI_CONFIG_PASSPHRASE` and the successor typed at its
prompt: it decrypts every secret the stack holds and writes them back
under a fresh salt, in place, and the file is then committed. Until it
runs, the slot and the stack file disagree and every command against
that stack answers `error: incorrect passphrase` — loud, and repaired by
finishing the adoption or by putting the predecessor back.

**The stack's *state* carries a salt of its own, and it is rewritten by
the next `up` rather than by that command.** A first run under a new
passphrase therefore meets state whose secrets-provider salt names the
old one, and succeeds — but only because that state holds no secret
*values* to decrypt; the `up` then writes the state's salt afresh. Where
a secret does exist there the mismatch is loud in its own way —
`failed to decrypt: incorrect passphrase`, the state's own salt being a
verifier checked when a secret is read — so the case this glosses over is
a refusal and never a corruption. The estate passphrase adopts the same
way, plus a `derived sync` to re-push it to every Environment.

**Never repair that disagreement by deleting `encryptionsalt`.** With no
salt, `pulumi config set --secret` writes a *new* one from whatever
passphrase is in the environment and re-keys the stack silently — the
one operation on this path that does not announce itself, and the way a
stack encrypted apart quietly rejoins the estate passphrase. Done with
ciphertext still in the file, it is worse than silent: the values left
behind were encrypted to the superseded key and the new salt verifies
the new one, so they decrypt under **neither** passphrase and the file
looks intact. It is safe
in exactly one case, which is why the crossing that introduced this row
used it: a stack file that holds no ciphertext at all has nothing to
lose. `Pulumi.github.yaml` is that file today — it carries no
`encryptionsalt` at all, and a comment at its head says why — so the
first `credentials derived github-admin record` derives the salt from
the passphrase in hand rather than checking against one that names the
estate's.

**A value that already exists is imported rather than replaced.**
`credentials derived <row> import` escrows what a slot or a predecessor
already holds as that row's next generation. That is how a kit written
before the escrow joins the model: the live state passphrase, CA key and
age identities become first generations, nothing production-facing
rotates, and the superseded kit is destroyable as soon as `derived check`
and a recovery of each imported row prove the registry holds them.

**A compromised recovery key is a full production rotation.** The
registry is public, so an attacker who holds that key holds every
escrowed value; re-wrapping is no answer, because it changes none of
them. The order is a new kit first — so that what comes next is wrapped
to a key the attacker does not have — and then a new generation of every
row, adopted consumer by consumer. No design escapes this: one offline
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
the root, and why the seed needs no permission beyond the four
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
| `kit.kdbx` | The seed kit (§2.1), on the workstation that holds one. Not a slot — the offline store, whose canonical copies are the two envelopes. `$KLUSTER_KDBX` overrides the path, for a kit on removable media. | `credentials kit bootstrap` |
| `pulumi.passphrase` | The state passphrase (§2.2), kept here because `mise.toml` reads it from a file on every `pulumi` run: a template can neither prompt nor open a kit. | `credentials derived pulumi-passphrase generate`, `credentials derived pulumi-passphrase recover` |
| `github.passphrase` | The `github` stack's own passphrase (§2.2), which opens that stack's config and nothing else. Here for the same reason and read the same way, under its own variable: `PULUMI_CONFIG_PASSPHRASE` is process-global, so which passphrase is right depends on the `-s` a command carries. | `credentials derived github-passphrase generate`, `credentials derived github-passphrase recover` |
| `roots/<root>.<field>` | An account root's token file — the second layer of §2's chain, written only on a machine with no desktop secret store. No tool reads one on its own; a `credentials` run does, when a mint asks for the root. | `credentials root <name> remember` |
| `state-backend/` | The `operator` client bundle: CA, certificate, key, and the connection string for the appliance they authenticate against — which names the appliance and none of the files. The key is `0600`, which libpq insists on. | `state-backend provision`, `state-backend bundle operator` |
| `oci/state-backend/` | The appliance provisioner's own OCI key (§3): an SDK configuration file plus the `0600` PEM it names. An SDK configuration rather than a shape of this repository's own, because the SDK is the whole of the reader. The compartment it acts in is not here — that is a convention its reader shares (§3). | `credentials derived oci-state-backend mint` |

**The directory is `0700`, and that is the boundary that matters**: it
is what keeps every entry inside it private, whatever mode the file
itself carries. Two files are stricter on their own account — the client
key, because libpq refuses a key anything but its owner can read, and the
OCI key beside its configuration — and the rest are written at the process
`umask` under that directory. Only the kit is irreplaceable, and it is
irreplaceable in the envelopes rather than here; every other entry is
recovered, re-issued or re-pasted by the command in the right-hand column,
so a lost `.credentials/` costs a few commands and no credential.

`mise.toml` reads this directory — the passphrase, the backend URL and
the three `PGSSL*` variables naming the bundle beside it — falling back
to whatever the environment already holds. It reads no provider
credential at all, because none is here: each is a config secret in the
stack that reads it, which the program opens with the passphrase this
directory carries. **CI
walks the same path rather than a parallel one.** The four Environment
secrets that carry the `ci` bundle (`PULUMI_BACKEND_URL`,
`PULUMI_BACKEND_CA`, `PULUMI_BACKEND_CERT`, `PULUMI_BACKEND_KEY`) are
file contents, not variables a job reads: a composite action writes them
into the checkout's `state-backend/` slot before any `pulumi` runs, and
the same template resolves them there exactly as it does here. Only the
passphrase reaches a job as environment. Because each is
read by template rather than by a program, nothing on that path can
prompt: that is the whole reason these are files and not secret-store
entries (§1 rule 6).

**Moving to another workstation** is copying the directory (`rsync -a`),
minus `kit.kdbx` unless that machine is meant to hold the kit — one
copy in place of the mixture of an `rsync` under `~/.config` and a piped
passphrase that it replaces. A second workstation needs nothing more to
apply any stack: every provider credential travels in the committed
stack files, which the passphrase in this directory opens. **The client
bundle travels as it is, wherever the second checkout sits**: its
connection string names the appliance and no file at all
(`postgres://<role>@<ip>:5432/…?sslmode=verify-full`), and the three
certificates are found through `PGSSLROOTCERT`, `PGSSLCERT` and
`PGSSLKEY`, which `mise.toml` resolves from this directory's own
location on every run. The environment is the channel a path can be
carried on because it is the one libpq and the driver behind Pulumi's
Postgres backend both read; a connection string expands nothing, which
is why the paths are not in it (physical/state-backend.md §3).

The OCI configuration is the one entry that does not travel: the SDK
expands nothing either, so its `key_file` names the PEM beside it by
absolute path into this checkout. A checkout at a different path
re-runs `credentials derived oci-state-backend mint` on a machine that
holds the kit.

Some of these slots had other homes before, and every old location is
still read — the bundle and the appliance's OCI configuration by
`credentials`, with a warning naming the move; the passphrase file by
`mise.toml`, silently, because a template has no way to warn. A
workstation that predates the move therefore keeps working untouched, and
converges by running the commands above once. The fallbacks are marked in
the code and in `.gitignore` for deletion (`kluster-ops#34`, and
`kluster-ops#41` for the OCI one, whose predecessor is a hand-made
configuration under `~/.config` rather than a minted credential at all).
A machine that predates the GitHub token's move to the `github` stack's
configuration is the one case where an old location is **not** read: the
token file it holds is inert, and deleting it is the last step of that
crossing.
