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
> rejection live there. **The appliance is built and serving state**:
> `deploy/state-backend/` holds the Butane file, the operator keys and
> the dump script, and the `state-backend` console script renders,
> provisions and converges the box, checks its pins, writes the client
> bundle into its workstation slot (§3), logs in for diagnosis, and
> takes and restores dumps (§7). Still design-only: the key-rotation
> script of §1; the **drill key** of §5, so today every dump opens with
> an escrowed generation and nothing else; and the scheduled drills of
> §7.3.

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
    `state-backend provision`, which names every way the running box no
    longer matches the commit and, once the replacement is asked for
    (below), dumps that box, terminates it and relaunches (minutes of
    5432 downtime — CI retries, local ops re-run). SSH exists (operator key in
    Ignition) for **diagnosis only** — `state-backend ssh` looks the
    address up and logs in, so reading a log does not start with
    finding an IP. Note the key set is
    `deploy/state-backend/operator-keys.txt`: a workstation whose key
    is not in it cannot reach the box at all, which is a re-provision
    to fix, not an `ssh-copy-id`. The address is reserved, and the box
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
    converge run recomputes them and calls for the box's replacement
    when any differs, naming the ones that did. Certificates are compared by what they
    assert rather than by bytes, because they are re-issued on every
    render and would otherwise read as permanent drift. The two
    comparisons differ, and the difference is which key is stable: the
    **CA** is compared by subject *and public key*, its private half
    coming from escrow and outliving every render, while the **server
    certificate** is compared by subject and SANs alone, its key being
    random at each issuance (§3). The server's private key is therefore
    outside the bill of materials on purpose — rotating it is
    `provision --replace`, which is what that flag is for. A box with
    no such record (built before this existed) counts as drifted:
    silence is not evidence that it matches.
-   **One component is compared against the clock rather than against
    the repository: how much life the server certificate has left.**
    Every digested component is re-derived from the current commit, and
    the current commit issues a certificate that is always young, so
    equality can never see an expiry approaching. What the box records
    beside its digests is therefore the date its own certificate dies,
    and a converge leaves the box alone while more than the **renewal
    margin** remains and calls for its replacement once less does. The margin's value
    lives in one place, `config.RENEWAL_MARGIN`, and its reason is a
    ratio rather than a date: it is small against the certificate's
    validity (`pki.LEAF_VALIDITY`), so a certificate spends a small
    fraction of its life inside the margin and the box is replaced for
    expiry at most once per certificate. It is also wider than the
    expiry alert §3 describes, which would make that alert the backstop
    for a box nobody has converged — or whose reports nobody acted on —
    rather than the trigger for the rotation — but that probe does not exist yet, so today the margin
    is the only thing watching the certificate at all, and the ratio is
    the reason that stands on its own. A **threshold**, not a date, is what
    keeps a time-dependent component from making the converge flap:
    outside the margin a second run is the same no-op as the first, and
    inside it the replacement carries a certificate with its full
    validity ahead of it, so the run after the rebuild is a no-op
    again. A box recording no expiry is drifted for the reason a box
    with no digest map is.
-   **A replacement is asked for before it happens, and dumped before
    it happens.** Drift is a reason to replace the box, not permission
    to: a plain `state-backend provision` reports what differs and
    stops, and `--force` asks for exactly that replacement.
    (`--replace` is the same request for a box with no drift to find.)
    **Neither flag stands in front of a prompt** — the replacement
    decision reads nothing from a terminal, so it means the same thing
    in a playbook and under a scheduler as it does by hand. (The run as
    a whole is not unattended: opening the kit asks for its password
    when the desktop secret store does not hold one, which is the one
    place a `provision` waits for a human.) Once asked for, the run
    dumps the box it is about to destroy and verifies the dump before
    terminating anything, which closes the window back to the last
    nightly one. That makes the replacement depend on the dump,
    deliberately: a dump that fails stops the run with the box still
    standing. `--no-dump` is how an operator says the box cannot be
    dumped at all — unreachable, or a Postgres that will not start,
    which is the case §6 sends here as its diagnosis path — and accepts
    losing everything since the nightly object.
-   **A run that replaced the box does not report success.** What it
    leaves is an appliance answering on 5432 over an empty database,
    which is half of the operation and reads as all of it. So the run
    ends by naming the dump it took and the `state-backend restore`
    that puts it back, and exits non-zero until that has happened — a
    status distinct from both the converge that changed nothing and the
    run that failed.
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
    the machine's definition, and the `state-backend` console script
    carries the executable form of every operation this document names
    — provision/re-provision, dump, restore, key rotation. The
    playbooks (§7) *invoke* these; a procedure that exists
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
    on the workstation; the CA key never reaches the micro, CI, or
    Pulumi state — the only processes that hold it are the
    `state-backend` runs that issue a certificate. It is **random at
    creation and escrowed** as `state-backend/ca` (credentials.md §2.2): a
    ciphertext in the repository that only the offline recovery key
    opens, which is the copy a rebuild reads. The bring-up run that
    first provisions the appliance is what generates and commits it
    (credentials.md §4.1); every run after that reads it and mints
    nothing, because generating over a live CA would invalidate every
    certificate under it. Exactly three certificates under it:
-   **Server cert, 2–3 years, SAN = the micro's reserved public IP.**
    Clients connect by literal IP with `sslmode=verify-full` (libpq
    matches IP SANs), keeping the state-backend hot path free of any
    DNS dependency — the backend stays reachable when Cloudflare or
    the `dns` stack is itself the thing being repaired.
-   **Two client certs — `ci` and `operator`**. The `ci` key is a CI
    Environment secret; the `operator` key is a **workstation slot**
    (credentials.md §1 rule 6) — `.credentials/state-backend/` in the
    checkout, written by `state-backend provision` and by `state-backend
    bundle operator`, alongside the connection string for the backend it
    authenticates against. libpq refuses a client key anything but its
    owner can read, so the key is `0600` and the directory `0700`.
-   **The connection string names no file; the environment does.**
    `postgres://<role>@<ip>:5432/pulumi_state?sslmode=verify-full` is
    true on every machine, and the three files travel beside it as the
    standard libpq variables `PGSSLROOTCERT`, `PGSSLCERT` and
    `PGSSLKEY` — the one channel both libpq and the driver behind
    Pulumi's Postgres backend read, and the reason no placeholder is
    ever expanded inside the string itself. Paths in the string would
    make the recorded copy true of one directory on one machine, so
    moving a checkout would invalidate it silently. `mise.toml` sets
    all four variables from the same slot, and a CI job materializes
    the `ci` bundle into `.credentials/state-backend/` of its checkout
    so that it resolves them the same way rather than through a
    workflow environment of its own. A slot written before the split
    still works as it is: a parameter inside a connection string
    overrides the variable of the same meaning, so such a URL names the
    bundle it was written beside, and `state-backend bundle operator`
    rewrites it into the portable form.
-   **The three leaf keys are random at issuance and escrowed
    nowhere.** They are re-issuable from the CA at any time, so a
    stored copy would be an exposure that buys nothing back: writing a
    client bundle mints a certificate rather than reproducing one, and
    the box authenticates the CA rather than a particular leaf, so a
    workstation re-running `state-backend bundle operator` needs no
    notice given to anything. Issuing twice yields two different keys,
    which is why a caller that needs a certificate and its key takes
    both halves from one issuance.
-   **No CRL/OCSP.** At three certificates, revocation infrastructure
    is standing rent for nothing: the compromise response is
    "regenerate the CA, reissue all three, re-provision" — playbook
    §7.1.
-   **An approaching expiry surfaces as drift; rotating is still an
    operator's decision.** Once the box's recorded expiry is inside the
    renewal margin, every `state-backend provision` names it in the
    drift it reports — so on an installation whose stacks are deployed
    at all, nobody has to be watching a date. What the plain run does
    *not* do is act: replacing the box is `--force`, like every other
    replacement (§1), so the certificate is re-issued when an operator
    says so and not before. **Still design-only**: the ops repo's
    scheduled workflow (ci.md
    §3) is to assert ≥30 days remaining on the server cert via an
    `openssl s_client` probe (no credentials needed), failing into the
    unified alert channel (architecture.md §4.3). The margin opens
    earlier than that threshold, so once the probe is built it fires
    only for a box nobody has converged — or whose reports nobody acted
    on — in the interval between the two. Until it is built, a box
    nobody converges is a box whose expiry nothing reports. Response: playbook §7.1.

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

-   A systemd timer (quadlet) runs `pg_dump -Fc`, **lists the archive
    with `pg_restore --list` and fails the run when it names no
    table**, **age-encrypts** the dump — it holds every stack's
    ciphertext *and* salt — and
    uploads to B2 under the state-backend prefix with a
    **prefix-scoped key holding `writeFiles` alone** — the system's
    one genuinely write-only key: unlike restic, the uploader keeps
    no index to read (storage.md §4). Pruning is not the box's job:
    RPO ≤ 24 h is fine — state is re-derivable from reality
    (`pulumi refresh`/import) at worst. What the listing catches is a
    dump of a database that has no tables — which is exactly what a box
    produces after a replacement nobody followed with a restore (§1) —
    and it is why the dump reaches a file before it reaches `age`
    rather than being piped into it: a stream has no table of contents
    to read. It is **not** a truncation check: a custom-format archive
    carries its table of contents at the head, so a file cut down to a
    few kilobytes still lists what the whole one would have. The
    plaintext lives in `/var/tmp`
    beside its own ciphertext for the two steps that read it — not in
    `/tmp`, which on this box is backed by memory rather than by the
    50 GB disk — and is unlinked as soon as the ciphertext exists.
-   **The same dump on demand: `state-backend dump`.** It differs from
    the timer's in its channel, its destination and its spool — the
    operator's client certificate rather than the box's local socket, a
    named local file rather than a B2 object, a temporary directory on
    the workstation rather than `/var/tmp` on the appliance — and it
    refuses to overwrite a file already there, which the box has no
    equivalent of. What it does not
    differ in is the archive: the same `pg_dump -Fc`, the same age
    recipients, and the same listing refused on the same terms. That is
    what makes a hand-taken dump interchangeable with a nightly one: as
    recoverable, and a restore cannot tell which produced its input.
    The playbooks below take theirs from the converge.
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
    immortal.** A generation is a label with a **stored ciphertext**:
    the identity for `backup/age/<generation>` is random at creation
    and its age ciphertext is committed under `escrow/`
    (credentials.md §2.2), where the offline recovery key alone opens
    it. Like the CA, generation N is minted by the bring-up run that
    first installs it, and read unchanged by every run after.
    Rotating means generating the next one and re-provisioning, and a
    retired generation's ciphertext stays in the repository until the
    last dump under it expires. Rotation is a designed path, not an
    emergency improvisation:
    -   **Every dump is encrypted to the two newest generations plus
        the ops-repo-held drill key** — age is natively multi-recipient,
        and all three public keys sit in the Butane file. The
        generational pair makes per-object key attribution
        unnecessary (deploys are intentionally manual, so git dates
        prove nothing about which key an object carries): any object
        in retention decrypts with the current *or* previous key,
        and 30 days after a rotation the current key alone covers
        the entire retention window. Before the first rotation there
        is no previous key and the window is generation 1 alone —
        naming a generation 0 would escrow a key for a generation
        that never existed. The **drill key**
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
        change): `credentials derived backup-age-<N+1> generate`, then
        bump the appliance's generation pin, which swaps the Butane
        recipients `[N, N−1] → [N+1, N]`, then re-provision —
        playbook §7.4. The pin and the escrow's expectations come from
        the same constant, so `credentials derived check` fails until
        the new generation exists. The path stays warm because the
        apply is the same re-provision as everything else.
    -   **Old keys get a definite end of life**: generation N−1
        becomes destroyable **30 days after the rotation to N+1** —
        every object it can uniquely decrypt has aged out, and
        everything newer also carries key N. Destroying a generation is
        deleting its ciphertext under `escrow/`, which is the only copy,
        and doing so is what actually ends that generation's exposure.
        **No field holds that date**: a ciphertext carries no expiry and
        the register has nowhere to write one, so the yearly offline day
        is what honors it (operations.md §4). At most two *generational*
        private keys are ever live — the ops-repo-held drill key sits
        outside the generations. (The register is the
        [credential register](../credentials.md), which inventories
        every credential in the system; this label is one of its
        escrowed rows.)
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
    generations). The saving would be one row in the register. Both are
    escrowed labels of their own (credentials.md §2.2), which is what
    lets them rotate on their own clocks.

## 6. Monitoring

Everything observable lives **outside** the box:

-   Every CI job and local `pulumi` operation is an implicit
    5432 + TLS + auth probe — backend-down is discovered by the first
    thing that needs it, which is the only thing that cares.
-   The ops repo's scheduled workflow (ci.md §3) is to assert pg_dump
    freshness
    (object-age on B2) and server-cert expiry (≥30 days), alerting
    into the unified alert channel (architecture.md §4.3). **Both
    probes are design-only.** For the certificate the renewal margin
    of §1 narrows the gap — any converge reports the coming expiry
    without being asked, though the re-issue itself waits for
    `--force` — and for the dump nothing does. Each alert maps to a
    playbook: stale
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

**The two moves the playbooks below are built from are commands, not
prose.** `state-backend dump` writes a `pg_dump -Fc` of the live state,
age-encrypted to the recipients of §5, into a named local file;
`state-backend restore <file>` feeds one back into a provisioned box.
Either form of file is accepted — an encrypted dump or a bare archive
— and the identity that opens an encrypted one comes from the escrow
via the kit, or from `--identity-file` for a workflow that was handed a
key and has no kit at all (§7.3). Both connect over the `operator`
client bundle (§3), which is the same connection string `pulumi` uses,
and both hand `PGSSLROOTCERT`/`PGSSLCERT`/`PGSSLKEY` to the tool they
run — so a `pg_dump` that is really a wrapper around a container has to
forward those variables and mount the bundle at the paths they name.

A converge that is about to replace the box takes the first of those two
moves for itself (§1), so a playbook below names the file that run wrote
rather than a dump the operator had to remember to take.

Each **verifies rather than reports**, because the moment either is run
is the moment nobody can afford to find out later:

-   A dump is listed with `pg_restore --list` before it is called one,
    and a listing naming no table fails the run — the shape that
    catches an archive of a database with nothing in it, which is
    what a replaced box holds until its restore. The appliance's own
    timer runs the same listing before it uploads (§5). The plaintext
    exists in a temporary directory for the two steps that read it
    and is never written beside the encrypted file.
-   A restore asks `pulumi stack ls` twice. Beforehand, so that a
    backend already serving stacks is refused rather than overwritten
    — `--force` is how a deliberate overwrite says so, and a box too
    freshly provisioned to answer at all is read as empty rather than
    as a reason to stop. Afterward as the verification proper: **a
    restore is finished when Pulumi can log in to what came back and
    list what it holds**, which is a stronger claim than "the rows
    arrived". The load itself runs in a single transaction, so a
    failure leaves a box to re-run against instead of a half-populated
    backend that `pulumi` would read as authoritative.
-   Ownership is preserved — no `--no-owner` — because the roles a
    dump names are certificate subjects that exist on every box (§3),
    and flattening them would hand CI's tables to the operator.

-   **§7.1 Certificate rotation / CA reissue.** Trigger: an expiry
    inside the renewal margin, which the converge finds by itself (§1);
    the expiry alert of §3 once it exists, which by then means no
    converge has run since the margin opened; or key compromise.
    Outline: `state-backend provision --force`, which dumps the running
    box, replaces it with one holding a certificate re-issued under the
    same CA, names the file it wrote and exits non-zero →
    `state-backend restore <that file>`. The bracket is not
    optional: the server certificate rides in Ignition, so re-issuing it
    replaces the instance and its data directory with it — the same
    shape as §7.2, for the same reason. Rotating the server *key* while
    the certificate still has life left is `provision --replace`, there
    being no drift for a converge to find. On compromise of the CA
    itself: `credentials derived state-backend-ca generate` for a new
    generation, the same replace-and-restore against it, redistribute
    the `ci` and `operator` bundles → `verify-full` check.
-   **§7.2 Postgres major upgrade.** Trigger: renovate major pin PR.
    Outline: merge → `state-backend provision --force` (the run dumps the
    old box before terminating it, and the new major initdb's a fresh
    data directory) → `state-backend restore <the file the run named>`
    → clean `pulumi preview`. The dump is the run's own first act
    rather than a step the operator has to remember, because the
    re-provision destroys the box it came from; it is verified as it is
    taken rather than trusted for the minutes between the two, and a
    dump that fails aborts the replacement.
-   **§7.3 Rebuild / DR drill (quarterly, automated in the ops repo).** §7.2
    minus the pin bump: provision a scratch micro from Butane,
    restore the latest age-encrypted B2 object via the **drill key**
    (`state-backend restore <object> --identity-file <key>`, the form
    that needs no kit), verify, destroy — unattended, alert on failure
    (operations.md §4). One pass exercises B2 download, decryption,
    provision-from-Butane, restore, and cert delivery. The *offline*
    age identity is proven separately by the yearly rotation (§7.4),
    which inherently decrypts with it.
-   **§7.4 age identity rotation.** Trigger: yearly cadence, key
    compromise, custody change. Outline: `credentials derived
    backup-age-<N+1> generate` → note the rotation date and N−1's
    earliest-destroy date where the next offline day will read them
    (nothing stores either — §5) → bump the generation pin, which swaps
    the Butane recipients `[N, N−1] → [N+1, N]` →
    `state-backend provision --force`, which replaces the box and leaves
    it empty like every other replacement here →
    `state-backend restore <the file that run named>` → verify
    both decrypt paths → destroy N−1 on its date by deleting its escrow
    ciphertext (compromise: drop the key from recipients now, fresh
    dump, early-delete old objects).
