# Physical Design: the State-Backend Appliance

The OCI **VM.Standard.E2.1.Micro** (Always Free, x86, 1 GB) running the
Pulumi `postgres://` state backend for every stack. It sits
**beneath** every stack that keeps its state there — a bootstrap
dependency that must exist before any of them can act — so it is
declared by a stack of its own, `state-backend`, whose state is
committed to this repository rather than kept in the backend it creates
([framework/pulumi.md](../framework/pulumi.md) §3.3), and whose
machine is `src/kluster/lib/state_backend/`. Design goal: a
**zero-maintenance appliance** — the box carries no state that
`pg_dump` and a replacement can't rebuild, and every operational path
is either automated or a written playbook (§7).

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
> rejection live there. **The appliance serving state is the
> `state-backend` stack's**, since the replacement of 2026-10-09 that
> cut it over (rfc-006 §14, slice 6; the drill's record is
> `kluster-ops#462`). The stack declares it whole (rfc-006 §4), and the
> box serves the stack's stable server certificate and SSH host key
> (§1), the ones `state-backend ssh` pins. The stack still carries the
> adoption, the ids `state-backend adopt` wrote into its configuration
> as `adopted` for its program to import, until slice 7 removes it.
> `src/kluster/lib/state_backend/machine/`
> holds the Butane file, the operator keys, the dump script and the
> files the `credentials derived` rows commit beside them; the
> `operator-stack state-backend` driver plans and applies the stack;
> and the `state-backend` console script renders a scratch box, checks
> the pins, writes the client bundle into its workstation slot (§3),
> logs in for diagnosis, takes and restores dumps (§7), and probes the
> box from outside (§6). The **drill key** of §5 has its generator
> (`credentials derived drill-age-identity generate`), and its
> committed public half is among the recipients the box encrypts to
> (`committed.age_recipients`). The two scheduled probes of §6 — the server certificate's
> expiry, and the age of the newest dump — are `state-backend probe`,
> built to be run from the ops repository on a schedule; whether that
> schedule is in place is the ops repository's own record (its README's
> census of workflows), and until that schedule starts a job, the probe
> runs when an operator runs it. Every key rotation this document
> describes is carried out with commands that exist and, for the age
> identity, a pin edit (§7.4): a reissue into the stack's configuration
> and the replacement that carries it for the server and host keys, a
> generation bump in configuration for the dump key (§1), and the
> commands the §7.1 and §7.4 playbooks name for the CA and the age
> identity; the one step no command takes is deleting a compromised
> generation's objects from the bucket early. The box runs the Postgres
> image `POSTGRES_IMAGE` pins by digest (§2) once the first
> `operator-stack state-backend up --force` after that pin has run
> (Aetf/kluster-ops#504): until then the stack's `plan` shows that
> replacement -- a dump, a new box, a restore -- and the box runs the
> `postgres:17` its first boot pulled. As of 2026-09-25 the
> scheduled drill of §7.3 is design-only, so no drill runs. §7.3.1 is
> the restore rehearsal an operator runs in its place. It ran on
> 2026-09-18 against a scratch box, on a dump taken from a workstation,
> and `state-backend restore` ran against the appliance itself in its
> first replace-and-restore on 2026-09-25; §7.3.1 says which steps each
> proved, and what neither did.

## 1. OS & configuration management

**Fedora CoreOS, stable stream.** Verified facts (2026-08-24, official
FCOS docs): OCI is a supported platform (`coreos-installer download -p
oraclecloud`, x86_64), the qcow2 imports as a custom image
(PARAVIRTUALIZED launch mode), and Ignition is delivered as instance
`user_data` — FCOS reads it in place of cloud-init.

-   **Single source of truth: the `state-backend` stack.** Its
    component, `kluster.components.state_backend`, declares every entity
    the appliance is (rfc-006 §4.1): the network, the custom image,
    the instance with the Ignition its Butane file renders to, the
    reserved address, the readiness of the box on that address, the
    dump bucket and its retention, and the dump key. The Butane file is
    reviewed like any code, and so are the pins it is rendered with:
    renovate opens pin-bump PRs against
    `src/kluster/lib/state_backend/settings.py` (§2) — the Fedora CoreOS
    release and its digest among them — and humans merge them.
-   **A run of the stack is the apply path.** No configuration agent,
    no SSH mutation: any change = PR → `operator-stack state-backend
    plan`, a refreshed preview, which names every difference between
    the commit and what OCI and B2 hold → `operator-stack state-backend
    up`. A difference that does not touch the box — a security rule
    added by hand, a retention changed — is repaired in place by the
    next `up`, and the box keeps serving. A difference that replaces
    the box — a changed Ignition, any input the provider replaces the
    instance on — waits for `--force` (below). SSH exists (operator key
    in Ignition) for **diagnosis only** — `state-backend ssh` dials the
    recorded address and logs in. Note the key set is
    `src/kluster/lib/state_backend/machine/operator-keys.txt`: a
    workstation whose key is not in it cannot reach the box at all,
    which is a replacement to fix, not an `ssh-copy-id`. The no-drift
    rule is what makes "the repo describes the box" true, and the
    refreshed `plan` is what reads it: a hand edit is in the refreshed
    state, and a difference on the next run.
-   **The keys the box carries are stable.** The server key and
    certificate, the SSH host key, the dump key and the age recipients
    are each minted once and held where the stack reads them (rfc-006
    §5): the first three in the stack's configuration and state, under
    the operator passphrase, the recipients committed beside the Butane
    template. So two renders from the same commit are equal byte for
    byte, and the diff Pulumi computes on the instance's `metadata` is
    the box's bill of materials. The instance also carries a digest per
    component of what it was built from in `extendedMetadata`, in the
    clear, because `metadata` is secret, and a diff can show it only as
    changed: a planned replacement names there which component moved —
    the Butane file, the operator keys, a pin, a key, the dump key's id.
    The Butane digest is over the template's text as committed,
    comments included, so a comment-only edit moves that digest in
    place and replaces nothing.
-   **First contact with the box is not trust-on-first-use.** The
    address is reserved, and the box is cattle, so every replacement
    hands the same address a machine — and the one machine whose
    compromise reaches every stack's state is the last place to answer
    that with "type yes". So the SSH identity is minted rather than
    discovered: `credentials derived state-backend-host-key generate`
    draws an ed25519 key into the stack's configuration and writes its
    public half to `src/kluster/lib/state_backend/machine/host-key.txt`,
    a file to commit; the Ignition delivers the private half as
    `/etc/ssh/ssh_host_ed25519_key`, and the stack refuses to plan while
    the configured key's public half is not the committed one. Fedora
    CoreOS uses a delivered key as it stands — `sshd-keygen@.service`
    runs only for a key type whose file is missing or empty — so nothing
    on the box overwrites it. Rotating it is generating again and the
    replacement that carries it.
-   **`state-backend ssh` holds the box to the committed pin.** The
    public half goes into a `known_hosts` file of the tool's own beside
    the client bundle, keyed by `settings.ADDRESS`, which the client is
    pointed at exclusively and under strict checking. A wrong or unknown
    key is refused rather than written down. **No client configuration
    participates in that connection** — the exec passes `-F /dev/null`,
    so neither the operator's `ssh_config` nor the machine's is read.
    That is a cut rather than a list of directives to distrust, and it
    has to be: a `ControlMaster` block, which is ordinary on a
    workstation, would otherwise let one bare login outside the tool
    leave a multiplexing socket that a later pinned exec attaches to
    with no host-key check performed at all, and a `KnownHostsCommand`
    would supply trusted keys beside the pinned file. It needs no OCI
    credential and makes no OCI call: the pin is a committed file, and
    whoever can change it is whoever can merge into `main`. Before it
    connects the run says both readings of a refusal: a box the stack
    has not replaced since the key was generated, or something
    interposed on the path. The operator's own `~/.ssh/known_hosts` is
    neither read nor written, so a bare `ssh core@<address>` outside the
    tool is unpinned by definition and the answer to it is
    `state-backend ssh`.
-   **What the box stands on is declared beside it, and repaired in
    place.** The appliance's own VCN, internet gateway, route table and
    subnet; one security list the program owns, which the subnet carries
    alone, holding exactly TCP 22 and 5432 from anywhere, the ICMP rules
    a VCN's default list carries — "fragmentation needed" from anywhere,
    which path MTU discovery needs, and every "destination unreachable"
    from inside the VCN — and all egress, every rule stateful; the box's
    one interface, in the subnet, with no public address and no network
    security group; the reserved address pointed at that interface's
    primary private address; and the dump bucket with the retention of
    §5. The security list's rules are one input of one resource, so a
    rule added by hand is part of that resource's refreshed state and a
    difference on the next run; `up` puts the list back without touching
    the box. The VCN, the subnet, the reserved address and both buckets
    are protected: a run that would replace any of them is an error, in
    a preview too.
-   **Two route-table attachments are deliberately not declared, and
    so not compared.** Besides a subnet, OCI attaches a route table to
    a private address, the interface's included, for source-based
    routing, and to an internet gateway, for ingress routing. The stack
    sets neither: it declares no private address of its own, and it
    leaves the gateway's `route_table_id` unset, which the provider
    treats as optional and computed, so whatever is there is read back
    and never differs. The subnet's table is the only one the program
    owns, and the box's one path is that table's default route out
    through the gateway; nothing here routes by source address or
    inspects ingress. So a table put on the interface or on the gateway
    by hand is drift no plan shows. A test in
    `tests/test_state_backend_stack.py` holds the subnet as the only
    resource the stack hands a route table, so declaring either later is
    a visible change.
-   **A replacement of the box waits for `--force`, and is dumped
    before it happens.** A plain `up` whose preview plans a create, a
    replacement or a delete of the instance writes nothing, names the
    digests that moved and `--force`, and exits 1; `--force` runs it,
    and `--replace` replaces the box when nothing moved. **The engine
    enforces that gate, not the driver alone**: a `before_create` and a
    `before_delete` hook on the instance refuse unless the run carries
    the replacement permission, which the driver sets on the one process
    `up --force` starts and removes from every other's environment, so a
    bare `pulumi up` passed through, or a caller's shell that exported
    the variable, fails at that step with the box untouched. The delete
    hook then dumps the box about to go, through the reserved address,
    which still points at it — the instance is replaced old box first —
    and keeps the plaintext for the restore: a dump that fails fails the
    delete, and the old box keeps serving. The new box answers on the
    reserved address once the readiness resource is created, and its
    `after_create` hook restores that plaintext and verifies it with
    `pulumi stack ls`. **Neither flag stands in front of a prompt** for
    the replacement — `--yes` skips the confirmation `up` asks before it
    applies anything — so the flags mean the same thing in a playbook
    as by hand.
-   **A run that leaves the backend empty does not report success.** A
    box whose run took no dump of its own — a first launch, a launch
    after a lost box, the cutover, a run after one that died between its
    terminate and its restore — finds a backend that holds no stack and
    nothing to restore: the restore hook names `state-backend restore
    <file>`, or the first `pulumi stack init` of a site with no state
    yet, and fails, and the driver exits 3. **The record of a restore
    owed is the backend itself**, not a workstation file: after every
    `plan` and every `up` the driver asks the estate's backend which
    stacks it serves, over the `operator` client bundle, and answers 3
    while it answers and serves none, and 4 while it does not answer —
    naming the address it dialed — so a second workstation reads what
    the first left behind.
-   **A create beside a held address is refused, `--force` or not.**
    A run whose refreshed state holds no instance, while the refreshed
    reserved address is assigned to something, would launch a second
    box beside one another workstation launched and has not landed the
    checkpoint of, or one the stack never declared, and take the address
    from it. The driver refuses that run, naming what the address
    points at. On a first launch, or after the box is lost, the address
    points at nothing: an instance's termination deletes the private
    address the reservation pointed at.
-   **The server certificate's expiry is a stack output.** The program
    exports when the certificate in its configuration expires, and the
    driver holds it against the **renewal margin**
    (`settings.RENEWAL_MARGIN`) on every preview: inside it, the run
    names the reissue, `credentials derived state-backend-server issue`,
    and the `up --force` that carries it, and changes nothing. The
    margin is small against the certificate's validity
    (`pki.LEAF_VALIDITY`), and wider than the expiry probe's alert
    margin (`config.EXPIRY_ALERT_MARGIN`, §6), which makes that alert
    the backstop for an appliance whose stack nobody has run rather than
    the trigger for the rotation; a test holds the two margins in that
    order.
-   **The image matters only at the next launch.** The custom image is
    imported from the pinned Fedora CoreOS release's `oraclecloud`
    artifact, checked against its digest and uploaded to the image
    bucket on the way; a release bump imports a new image and changes
    nothing else, because the instance ignores a change of its source
    image — an in-place one would replace the running box's boot volume
    without deleting the instance, so without the dump hook — and
    Zincati keeps the running OS current.
-   **OS updates: Zincati `periodic` strategy** — reboots confined to
    a weekly maintenance window (exact window chosen at
    implementation), not finalized the moment a rollout arrives:
    the blip is the same either way, but a *scheduled* blip is never
    mistaken for an incident and never coincides with someone
    mid-operation. A reboot is a brief 5432 outage, accepted:
    Postgres shutdown is systemd-ordered, nothing corrupts, and the
    clients retry.
-   **Everything the template executes names a binary the box has.**
    Fedora CoreOS is immutable and packageless: the image is the whole
    of what is installed, and the template's own header says the rest —
    anything missing from the file does not exist on the machine. So
    each `Exec*=` head in `butane.yaml.j2`, and the shebang of every
    file the template writes executable, names either a binary the
    stable stream's image ships — its composed package set is
    `manifest-lock.x86_64.json` on the `stable` branch of
    `coreos/fedora-coreos-config`, and no Python of any kind is in it —
    or a path another unit in the same template installs, which today
    is `/opt/bin/age` from `age-install.service`. The dump script is
    shell for that reason (§5), written against `bash`, `coreutils`,
    `curl`, `jq`, `gawk`, `sed` and `podman`, all of which the
    lock carries. A test in `tests/test_state_dump.py` reads the
    template as text, extracts those targets and holds them to a
    committed allowlist that cites the lock. It exists because the rest
    of the suite cannot see this failure: a script runs on a
    workstation under whatever interpreter its shebang names, and on
    the box the same shebang fails at exec — `203/EXEC`, "No such file
    or directory" against a file that is there — on every timer run.
-   **Every hand operation is a command.** The box is declared by the
    stack, and what the stack does not declare is a command of the
    `state-backend` console script: render a scratch box, ssh, pins,
    bundle, dump, restore, probe, and the cutover's `adopt`. Key
    rotation rides the `credentials derived` rows that fill the stack's
    configuration and the replacement that carries what they wrote; the
    CA and age identity rotations of §7.1 and §7.4 add the generator for
    the new generation, after the pin edit §7.4 starts with for the age
    identity. The playbooks (§7) *invoke* those commands; a procedure
    that exists only as prose in a playbook is a bug.
-   **Secrets ride Ignition, accepted**: the server TLS key (§3), the
    SSH host key and the B2 upload credential (§5) are in `user_data`.
    On this box that's fine where it wasn't for cluster nodes (audit
    H1): no untrusted workload runs here, so instance metadata is
    readable only by the instance itself and OCI principals with
    compartment read — who are root-equivalent for this box anyway.
    The stack holds the same values as ciphertext under the operator
    passphrase, in its committed configuration and checkpoint (rfc-006
    §3.3). Rotating any of them is a replacement.

## 2. Postgres

-   A plain systemd unit, `pgstate.service`, whose `ExecStart` is
    `podman run --replace …` — not a quadlet, so the box holds no
    `.container` file — with the image pinned by digest: the major
    line's tag and the digest it named when the pin last moved
    (`postgres:NN@sha256:…` — `POSTGRES_IMAGE` in
    `kluster.lib.state_backend.settings`, rendered into the Butane file).
    The box runs that image and nothing on it moves to another: the
    container carries no auto-update label, and no auto-update timer
    runs. A new digest — the tag rebuilt for a minor release or a new
    base — reaches the box as a renovate pull request, taken monthly, and
    the replacement that carries it (§7.2).
-   **Major upgrades take the rebuild path** (§7.2): pin bump → a
    replacement, which dumps the old box, initdb's a fresh data
    directory under the new major and restores into it. At tens of MB of state, owning `pg_upgrade` machinery
    buys nothing. (The *in-cluster* CNPG databases are the opposite
    case — their major-upgrade policy is workloads.md §4.)
-   Config: TLS on, `pg_hba` requiring certificate auth over TCP
    (§3); data directory on the boot volume (the DB is four orders of
    magnitude smaller than the disk).
-   **Roles.** The image's bootstrap superuser, `postgres`, is
    reachable only on the container's local socket, where `pg_hba`
    trusts it and asks for no certificate: nothing outside the
    container reaches that socket, getting there is `podman exec` as
    root on the box, and its users are the image's own initialization
    and the dump timer (§5). Over TCP `pg_hba` admits the two client
    roles, `ci` and `operator`, into `pulumi_state` and nothing else,
    so a certificate the CA signs for any other name — the superuser's
    included — is refused. Both client roles are `NOSUPERUSER`
    members of `state_owner`, a role that cannot log in, and every
    session either opens acts as it (a per-role `role` setting in
    `pulumi_state`). What that role holds is exactly what Pulumi's
    Postgres backend needs: `CONNECT` on `pulumi_state` and `USAGE`
    and `CREATE` on its `public` schema, which the box's own
    initialization grants, with every grant `PUBLIC` holds on the
    database revoked; and ownership of the `pulumi_state` table the
    backend keeps its objects in, which is the role's because a
    session acting as it created the table. Ownership, and not rows
    alone, because the backend
    (`pkg/backend/diy/postgres` at the pinned CLI) runs `CREATE TABLE
    IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` every time it
    opens, and Postgres checks `CREATE` on the schema and ownership of
    the table before it looks for either object: a role that can only
    read and write rows is refused on its first command. Acting as the
    owner is what makes the table the backend creates at first use,
    and whatever a restore loads (§7), belong to the owner, whichever
    client got there first. What neither client role holds is a
    superuser's reach: no `COPY … TO PROGRAM`, no server files, no
    other database. What each can do is what the backend does — read, write
    and drop any stack's state, `github`'s included; credentials.md §3
    records that as the excess the certificates carry. The roles are
    created by `/docker-entrypoint-initdb.d`, the image's own
    initialization, from a script the Butane file delivers, so they
    exist from a fresh data directory's first start and change only
    by a replacement.

## 3. PKI: a tiny offline CA

(Decisions from ci.md §1, 2026-08-24; this is the owning section now.)

-   **One single-purpose private CA** (~10-year validity), generated
    on the workstation; the CA key never reaches the micro, CI, or
    Pulumi state — the only processes that hold it are the commands
    that issue a certificate. It is **random at creation and escrowed**
    as `state-backend/ca` (credentials.md §2.2): a ciphertext in the
    repository that only the offline recovery key opens, which is the
    copy a rebuild reads. `credentials derived state-backend-ca
    generate` draws it once, at a site's bring-up (credentials.md
    §4.1), and every issuance after that recovers it and mints nothing
    over it, because generating over a live CA would invalidate every
    certificate under it. **Every certificate under it is a leaf issued
    on demand**: `credentials derived state-backend-server issue` and
    every `render` issue a server leaf, and every `bundle` and every
    `credentials derived sync` that reaches the `ci` bundle issues a
    client leaf. None of them is recorded anywhere, and each is valid until it
    expires, so the set of valid leaves grows with use and is not
    something anyone can list. What a leaf is good for is bounded by
    the box instead: a client leaf authenticates as the one role its
    CN names, and the box admits only the two client roles (§2).
    Two kinds of leaf:
-   **Server cert, 2–3 years, SAN = the micro's reserved public IP.**
    Issued into the `state-backend` stack's configuration, the key
    as a secret and the certificate and the CA's certificate in the
    clear, so the same key survives every replacement until it is
    reissued (rfc-006 §5). Clients connect by literal IP with `sslmode=verify-full` (libpq
    matches IP SANs), keeping the state-backend hot path free of any
    DNS dependency — the backend stays reachable when Cloudflare or
    the `dns` stack is itself the thing being repaired.
-   **Client certs, for two roles — `ci` and `operator`**. The `ci`
    key is a CI Environment secret; the `operator` key is a
    **workstation slot** (credentials.md §1 rule 6) —
    `.credentials/state-backend/` in the
    checkout, written by `state-backend bundle operator`, alongside the
    connection string for the backend it authenticates against. The
    `state-backend` stack's hooks connect with it, and the driver reads
    the backend through it after every run (§1). libpq refuses a client key anything but its
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
-   **Leaf keys are random at issuance and escrowed nowhere.** They
    are re-issuable from the CA at any time, so a stored copy would be an exposure that buys nothing back. The server key is the one
    leaf key held anywhere, in the stack's configuration, so the box's
    replacements keep it (§1). Writing a
    client bundle mints a certificate rather than reproducing one, and
    the box authenticates the CA rather than a particular leaf, so a
    workstation re-running `state-backend bundle operator` needs no
    notice given to anything. Issuing twice yields two different keys,
    which is why a caller that needs a certificate and its key takes
    both halves from one issuance.
-   **No CRL/OCSP.** The box's configuration changes only by a
    replacement (§1), so a revocation list could reach it only the way
    a new CA does — and a new CA revokes every leaf under the old one
    at once, with no list of leaves to keep. The compromise response is
    therefore "regenerate the CA, reissue the server certificate,
    replace the box, reissue the client bundles" — playbook §7.1. Until then a leaked client
    leaf is its role (§2): the state, not the box.
-   **An approaching expiry is named by every run of the stack;
    rotating is still an operator's decision.** Once the configured
    certificate's expiry is inside the renewal margin, every
    `operator-stack state-backend plan` and `up` names it with the
    reissue (§1) — so nobody has to be watching a date. What no run
    does is act: the reissue is a command, and carrying it to the box
    is the replacement `--force` asks for, so the certificate changes
    when an operator says so and not before. The backstop is the expiry probe of §6:
    `state-backend probe` reads the server certificate off an `openssl
    s_client` handshake (no credentials needed) and fails once less
    than `config.EXPIRY_ALERT_MARGIN` remains, and the ops repo's
    scheduled workflow (ci.md §3) is what runs it, failing into the
    unified alert channel (architecture.md §4.3). The renewal margin
    opens earlier than the alert margin, so the probe fires only for an
    appliance whose stack nobody has run — or whose reports nobody acted
    on — in the interval between the two. Response: playbook §7.1.

## 4. Network exposure

**The appliance owns its own network.** A VCN, public subnet, route
table, internet gateway, security list and reserved public IP, all
declared by the `state-backend` stack and repaired in place by its runs
(§1), and none of them the cluster's: the cluster VCN is a
`physical`-stack resource, whose state is in the backend this box
serves, so putting the box inside it would invert the dependency this
whole design exists to avoid. The isolation is a bonus, not the point. The **reserved**
public IP is load-bearing rather than tidy — the server certificate's SAN
is that literal address, so an ephemeral IP would invalidate the
certificate on every replacement. The address is recorded as
`settings.ADDRESS`, beside the other pins: a site fact that follows
from the first reservation, the way the compartment is recorded in
`conventions`, and the one home a reader that holds no bundle — the
probe of §6, and `state-backend ssh` — takes it from. It is public
already, on 5432 and 22 and in the certificate. The stack holds the
reservation to it: the component refuses a reservation that carries any
other address, naming both, before anything depends on it
(`StateBackend._held_address`), so a moved box is a decision the
repository records rather than drift a run follows. On a site whose
address is reserved for the first time the run ends at that refusal
naming the address OCI chose; recording it and running again is the
second half of the first launch.

Public 5432 with TLS + **mandatory client certificates** — the
client cert is the wall, and **the only wall** (decided 2026-08-24):
the subnet's one security list permits 5432 (and SSH, key-auth only)
from anywhere, beside ICMP "destination unreachable" messages:
"fragmentation needed" from anywhere, which path MTU discovery needs,
and every code from inside the VCN. The box's one interface is in no
network security group, so that list is all that admits anything to
the box (§1 says how a run holds it). The
earlier GitHub-Actions-ranges allowlist died on arithmetic —
`api.github.com/meta` lists thousands of CIDRs against an NSG rule
quota in the hundreds, so the "coarse pre-filter" cannot be
expressed, aggregating it until it fits is theater, and a
home-/32-only rule would simply break CI. What an arbitrary IP
reaches is Postgres's TLS handshake rejecting certificate-less
clients; brute force buys nothing against cert auth, a certificate
gets no further than the role it names (§2), and the
Postgres-CVE surface is bounded by the pinned image's monthly moves to
the tag's newest build (§2) plus the replace-not-mutate posture — the
same appliance logic as everything else on this box. (A scheduled workflow auto-editing
security rules was already rejected on standing-rent grounds; now
there is nothing for it to edit.)

**The security group is retired.** The appliance's script admitted
traffic through a network security group and the VCN's default list;
the stack declares a list of its own instead, because a rule added to a
group by hand is a resource of its own that no state holds, while one
added to a list is part of the list's refreshed state (rfc-006 §4.1).
The group the script made is gone: the replacement launched its box
outside it, and the group was deleted by hand (`kluster-ops#462`,
step 8).

## 5. Backup

-   A plain systemd timer, `state-dump.timer` driving
    `state-dump.service`, runs `pg_dump -Fc`, **reads the archive and
    fails the run when it holds no stack checkpoint**, **age-encrypts**
    the dump — it holds every stack's ciphertext *and* salt — and
    uploads to B2 under the state-backend prefix with a
    **prefix-scoped key holding `writeFiles` alone** — the system's
    one genuinely write-only key: unlike restic, the uploader keeps
    no index to read (storage.md §4). The key is a resource of the
    `state-backend` stack, minted with the stack's own B2 management
    key, its secret kept in the stack's state and in the box's Ignition
    alone, B2 returning it once; its name carries a generation from the
    stack's configuration, so a bump of that generation rotates it,
    which replaces the box that must carry the successor, and the old
    key is deleted at the end of that run. Pruning is not the box's job:
    RPO ≤ 24 h is fine — state is re-derivable from reality
    (`pulumi refresh`/import) at worst.
-   **Every object under the dump prefix holds at least one stack
    checkpoint, and the uploader refuses anything else.** A stack
    checkpoint is a key in the state table under
    `<database>/.pulumi/stacks/` that ends in `.json`, or in `.json.gz`
    or `.json.zst` when the backend compresses.
    That is the reading `pulumi stack ls` makes, and `stack ls` is what
    a restore ends on (§7), so the box uploads only an archive whose
    restore would pass the restore's own verification. Every archive
    with no checkpoint is refused; today's instances are a box nothing
    has opened, which has no state table; a box something has opened —
    `pulumi stack ls` is enough — which has the table and the backend's
    meta row; a site before its first `pulumi stack init`; and a
    backend whose stacks were all removed, which keeps only `.bak`
    rows. A refused run fails and uploads nothing, so the newest object
    stays the last dump that held state, and the failure leaves the
    login notice below. The first bring-up is no special case: until
    the first `pulumi stack init` the nightly refuses, and the probe
    (§6) reports an empty prefix, which is true — no state exists to
    back up. The script counts the checkpoints in the archive's state
    rows, read back with `pg_restore --data-only`, after listing the
    archive with `pg_restore --list`, which is the check that
    `pg_restore` can read it at all; it holds a copy of the operator's
    grammar (`state.checkpoints`), and the suite holds the two to one
    table of inputs and to what the pinned `pulumi` writes. Reading the
    archive rather than the database, because the object is what this
    rule is about, is why the dump reaches a file before it reaches
    `age` rather than being piped into it: a stream cannot be read
    twice. The listing is **not** a truncation check: a custom-format
    archive carries its table of contents at the head, so a file cut
    down to a few kilobytes still lists what the whole one would have.
    The rows read is one, for this database: its one data block is the
    state table's, so a cut anywhere past the table of contents fails
    `pg_restore --data-only`, and the run with it.
    The plaintext lives in `/var/tmp`
    beside its own ciphertext for the two steps that read it — not in
    `/tmp`, which on this box is backed by memory rather than by the
    50 GB disk — and is unlinked as soon as the ciphertext exists. The
    script is `src/kluster/lib/state_backend/machine/state-dump.sh`: shell,
    because the box has no interpreter to run anything else (§1).
-   **The same dump on demand: `state-backend dump`.** It differs from
    the timer's in its channel, its destination and its spool — the
    operator's client certificate rather than the box's local socket, a
    named local file rather than a B2 object, a temporary directory on
    the workstation rather than `/var/tmp` on the appliance — and it
    refuses to overwrite a file already there, which the box has no
    equivalent of. What it does not
    differ in is the archive: the same `pg_dump -Fc`, the same age
    recipients, and the same check — an archive holding no stack
    checkpoint is refused, and no file is written. That is what makes a hand-taken
    dump interchangeable with a nightly one: as recoverable, and a
    restore cannot tell which produced its input. A replacement takes a
    dump of the same form through the stack's delete hook (§1).
-   **Retention, explicit: STANDARD class — daily, kept 30 days —
    enforced by a B2 lifecycle rule on the prefix** (storage.md §4),
    not by the uploader. The `state-backend` stack declares the bucket
    with the rule, protected, and the rule converges in place in both
    directions: a rule changed by hand is a difference the next `plan`
    names and the next `up` puts back, with the box left serving. That
    keeps
    the box's key free of delete/prune capability (the H4
    discipline), and it is what gives retired encryption keys a
    definite end of life (below).
-   **Stale means one and a half periods.** The rule is
    `conventions.backup.max_age`, one function for every scheduled
    backup: a run that is merely late is inside the age, a run that was
    missed is half a period overdue by the time it runs out. The
    timer's period is `settings.DUMP_PERIOD` beside its calendar
    expression, and `settings.DUMP_MAX_AGE` is the rule's answer for
    it, which is the threshold the dump-age probe of §6 reads. The
    daily retention classes carry the same answer in the alert rules'
    syntax, and a test holds them equal.
-   The box reports one thing about itself, and to one audience: a
    run of `state-dump.service` that fails leaves a notice under
    `/etc/motd.d/`, which Fedora CoreOS prints at an interactive ssh
    login — a plain `state-backend ssh`; one given a command prints
    none — so it reaches whoever next opens a shell there for any
    reason, and it pages nobody. The next run that succeeds removes
    it. What nothing on the box observes is the bucket: its key writes
    and cannot list (above), so whether a recent object is *there* is
    a question only the outside can ask, and a run that never began —
    a timer that stopped firing, a box that is down — has no failure
    to report.
    Freshness is therefore asserted **from outside**, by the dump-age
    probe of §6 (object-age on the prefix, run on the ops repo's
    schedule, ci.md §3), and the box's design is what makes that
    probe load-bearing: a nightly that stopped in August looks exactly
    like one that ran, until a restore reaches for it. Reading the
    newest object's stamp is the assertion, and the probe is what
    performs it; §7.3.1 has an operator perform it by hand.
-   **The age identity rotates by generations; no key is assumed
    immortal.** A generation is a label with a **stored ciphertext**:
    the identity for `backup/age/<generation>` is random at creation
    and its age ciphertext is committed under `escrow/`
    (credentials.md §2.2), where the offline recovery key alone opens
    it. `credentials derived backup-age-<N> generate` draws generation N
    once and writes its public half into the committed recipients file,
    `src/kluster/lib/state_backend/machine/backup-recipients.txt`, which
    the stack renders the box's recipients from; `credentials derived
    check` holds that file to the escrow. Rotating means generating the
    next one and the replacement that carries it, and a retired
    generation's ciphertext stays in the repository until the last dump
    under it expires. Rotation is a designed path, not an
    emergency improvisation:
    -   **Every dump is encrypted to the two newest generations, and
        to the ops-repo-held drill key as a third recipient** — age is
        natively multi-recipient, and the public keys sit in the Butane
        file. The recipients the stack renders are the generations the
        committed recipients file names followed by the drill recipient
        where `src/kluster/lib/state_backend/machine/drill-recipient.txt`
        is on file (`committed.age_recipients`); the drill file is written by
        `credentials derived drill-age-identity generate`, which pushes
        the private half into the ops repository's `drill` Environment
        first, and it is absent until that generator has run. Every
        object still opens with an escrowed identity, and a drill with
        no Environment to read needs the kit (§7.3.1). The
        generational pair makes per-object key attribution
        unnecessary (deploys are intentionally manual, so git dates
        prove nothing about which key an object carries): any object
        in retention decrypts with the current *or* previous key,
        and 30 days after a rotation the current key alone covers
        the entire retention window. Before the first rotation there
        is no previous key and the window is generation 1 alone —
        naming a generation 0 would escrow a key for a generation
        that never existed. The **drill key**
        (operations.md §4, credentials.md §3) is what lets the rebuild
        drill run unattended; it adds no new *class* of exposure —
        the kluster CI's client cert already reads the live
        database, and the ops repo holding the key is fenced at
        the same private tier (architecture.md §4.3) — while the
        offline generations keep the survive-loss-of-GitHub role. It needs **no
        generational pair of its own**: its contract is decrypting
        the *latest* object only (retention coverage is the offline
        keys' job), though what it opens is every object written
        since it became a recipient and still in retention. So its
        rotation is `credentials derived drill-age-identity generate
        --rotate`, which overwrites the one Environment secret — with
        one slot, that *is* deleting the old key — and the recipient
        on file, then commit → `operator-stack state-backend up
        --force`, which dumps the box, replaces it and restores into the
        new one → a fresh dump the drill opens: no N−1 bookkeeping,
        and between the overwrite and that dump the drill cannot open
        the newest object, a gap bounded by one nightly.
    -   **Rotate at least yearly** (and on compromise or custody
        change): `credentials derived backup-age-<N+1> generate`, then
        bump the appliance's generation pin, which swaps the Butane
        recipients `[N, N−1] → [N+1, N]`, then the replacement that
        carries them — playbook §7.4. The pin and the escrow's
        expectations come from the same constant, so `credentials
        derived check` fails until the new generation exists, and until
        the recipients file names it. The path stays warm because the apply
        is the same replacement as everything else.
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

Every probe lives **outside** the box — the one signal it raises itself
is §5's failed-run notice, and that reaches nobody who does not open a
shell on it:

-   Every CI job and local `pulumi` operation is an implicit
    5432 + TLS + auth probe — backend-down is discovered by the first
    thing that needs it, which is the only thing that cares.
-   **`state-backend probe`** is the two scheduled checks, and the ops
    repo's scheduled workflow (ci.md §3) is what runs it, from a
    checkout of this repository at a pinned commit, alerting into the
    unified alert channel (architecture.md §4.3). One command runs
    both probes (`--only certificate|dumps` runs one), prints each
    verdict with what it measured as its probe answers, and exits with
    one bit per failed probe — 4 for the certificate, 8 for the dumps,
    their sum when both failed — so the workflow's log names which. 1
    is a run that could not probe at all (an empty secret), which is a
    different fact from a probe that did and failed; 2 is what the
    parser and `uv run` answer when the command never ran, which is
    why no probe's bit is 2. The workflow carries no threshold, no
    prefix and no address: every number is this repository's.
    -   **Certificate.** `openssl s_client -connect <ADDRESS>:5432
        -starttls postgres -showcerts` — the same handshake the
        stack's readiness wait makes, credential-free because the
        box sends its certificate before anything authenticates — and
        the **server leaf** is read off the transcript: the first
        certificate of the chain. It fails when the leaf is not valid
        now, when less than `config.EXPIRY_ALERT_MARGIN` remains, or
        when its SAN does not carry `settings.ADDRESS`, each into
        playbook §7.1; a box that does not complete the handshake is
        unreachable, into §7.3 (there is no NSG allowlist to refresh —
        §4). The leaf and only it: the CA is not what a handshake
        proves, and the two client certificates are held by their
        consumers, whose first `pulumi` command is their expiry's
        report — out of a network probe's reach by design.
    -   **Dumps.** Authorize as the list-only key (credentials.md §3,
        `B2 freshness key (state dumps)`, read from the two secrets
        `B2_FRESHNESS_DUMPS_KEY_ID` and `B2_FRESHNESS_DUMPS_KEY`), list
        `pulumi-state/`, and take the newest object's stamp: older
        than `settings.DUMP_MAX_AGE` (§5) is stale, into §7.3, whose
        restore path doubles as the diagnosis start. A box replaced
        and never restored reads as stale once that age has passed,
        because its nightly refuses an archive holding no stack (§5);
        each refused night brings the last object holding state a day
        closer to the 30-day lifecycle rule, so the stale verdict is
        the alarm and the 30 days are the deadline. **An empty prefix
        is its own failure** — no dump holding state has landed within
        the retention window: the box refuses to upload one holding no
        stack (§5), as for a box replaced and not restored or a site
        before its first `pulumi stack init`, or its timer never
        fired — and so is an object under the prefix
        that is not named like a dump, since nothing but the
        appliance's uploader can write there. Names and their stamps
        are the whole of what the key can see: never a byte of a dump.
        A key B2 refuses, or one confined to no bucket, is this
        probe's failure too, into the mint (credentials.md §4), named
        by its variables and never by its id, which the workflow holds
        as a secret. So is B2 not answering the listing at all, which
        points back here rather than at a playbook: the outage is B2's
        and not the box's, nothing is known about the dumps that run,
        and the next scheduled run is the retry.
    -   The renewal margin of §1 narrows the certificate's gap — any
        run of the stack names the coming expiry without being asked,
        though the reissue itself is a command and its delivery waits
        for `--force` — and the probe is the backstop behind it; for the
        dump the probe is the only watcher.
-   **Deliberately unmonitored, with rationale**: Zincati/update
    failures and disk fill. The DB is ~4 orders of magnitude under
    the disk, and OS staleness is to be bounded by the quarterly
    rebuild drill (§7.3), whose scratch box boots the pinned image and
    is updated by Zincati like the appliance — until that drill is
    scheduled the bound is Zincati alone. If either ever bites first, that is the signal to
    add the probe — not before.

So between scheduled runs of the probe, what observes this box is the
first bullet alone: something needed the backend, and either reached
it or did not. The probe is what reports on an appliance nobody is
using.

## 7. Playbooks

Per the alert discipline (architecture.md §4.3), each alert above maps
to a playbook. **This section is the design-level census** — title,
trigger, and outline only, enough to show what must exist; the
executable form of each is the `state-backend` commands of §1, and
§7.5 is how those commands are operated.

**The two moves the playbooks below are built from are commands, not
prose.** `state-backend dump` writes a `pg_dump -Fc` of the live state,
age-encrypted to the recipients of §5, into a named local file;
`state-backend restore <file>` feeds one back into a box that serves no stack.
Either form of file is accepted — an encrypted dump or a bare archive
— and the identity that opens an encrypted one comes from the escrow
via the kit, or from `--identity-file` for a workflow that was handed a
key and has no kit at all (§7.3). Each path's `age` binary has a home
of its own, and no pin beyond the pair a test holds equal: the
appliance's timer runs `/opt/bin/age`, which `age-install.service`
fetches once at first boot from `settings.AGE_URL` and verifies against
`settings.AGE_SHA256`; a workstation's `dump` and `restore` run the
`age` that `mise.toml` pins, which
`tests/test_age.py::test_local_age_matches_the_appliance_pin` holds
equal to `settings.AGE_VERSION`; and the drill workflow's runner, like
the probe's (§6), installs mise through `jdx/mise-action` inside its
checkout of this repository at a pinned commit, so its `age` is that
same `mise.toml` pin as of that commit. Their `pg_dump` and `pg_restore`
are `mise.toml`'s `postgres` pin, whose major
`tests/test_postgres_client.py::test_the_local_client_is_on_the_appliance_major`
holds equal to the one `settings.POSTGRES_IMAGE` names. Both commands connect over the
`operator` client bundle (§3), which is the same connection string
`pulumi` uses, and both hand `PGSSLROOTCERT`/`PGSSLCERT`/`PGSSLKEY` to the tool they
run — so a `pg_dump` that is really a wrapper around a container has to
forward those variables and mount the bundle at the paths they name.

A run of the stack that replaces the box takes both moves for itself
(§1): the delete hook on the instance takes the dump, of the box about
to go, into the working directory, and the create hook on the readiness
resource restores it into the new box once that box answers. The same code runs
in both, `kluster.lib.state_backend.state`, so a hook's dump is the
file `state-backend dump` would have written.

**A replacement that ends without its restore is finished by
`state-backend restore`.** Every playbook below that replaces the box
can end with the backend empty: the restore failed, the run was killed
between the terminate and the restore, or the run took no dump of its
own. Each of those exits 3, and the recovery is the same for each:

-   `state-backend restore <file>` of the dump the replacement took —
    the encrypted file its delete hook wrote, named in its output — or,
    where none was taken, or where it is on another workstation, of the newest
    nightly object, which loses what changed since. Then
    `operator-stack state-backend plan` answers 0, the backend serving
    its stacks again.
-   A run killed after it launched the new box but before it pointed the
    address at it leaves that box unknown to the stack, and the next run
    launches another beside it: that one is an orphan, terminated by
    hand. The way out of every case is to finish on the workstation that
    started the run and land its checkpoint.
-   A replacement of the dump key — its generation bumped — deletes the
    old key at the end of the run that launched the box carrying its
    successor; a run that stopped before that end leaves the old key
    live, able to write into the dump prefix and nothing else, until
    the next `up` completes.

Each **verifies rather than reports**, because the moment either is run
is the moment nobody can afford to find out later:

-   A dump is read before it is called one: listed with
    `pg_restore --list`, and refused when its state rows hold no stack
    checkpoint — the rule of §5, which catches the archive a replaced
    box produces until its restore, regardless of whether anything has
    opened it. A restore reads its archive the same way before the archive
    touches the database, so a `--force` restore of such an archive
    cannot empty a backend that serves stacks. The appliance's own
    timer holds its uploads to the same rule (§5). The plaintext
    exists in a temporary directory for the two steps that read it
    and is never written beside the encrypted file.
-   A restore asks `pulumi stack ls` twice. Beforehand, so that a
    backend already serving stacks is refused rather than overwritten
    — `--force` is how a deliberate overwrite says so. A box
    launched minutes ago answers with no stack, since the question
    opens the backend, which creates its table on the way; one that
    does not answer at all is read as serving nothing rather than as a
    reason to stop. Afterward as the verification proper: **a
    restore is finished when Pulumi can log in to what came back and
    list what it holds**, which is a stronger claim than "the rows
    arrived". The load itself runs in a single transaction, so a
    failure leaves a box to re-run against instead of a half-populated
    backend that `pulumi` would read as authoritative.
-   Ownership is the box's, not the archive's: the load runs with
    `--no-owner`, so everything it creates belongs to the role the
    restoring client acts as (§2). An archive names whichever role
    owned each object where it was dumped, and one from a box whose
    client roles were superusers names `ci` or `operator` itself,
    which the restoring role cannot become — replaying that ownership
    would abort the transaction.

-   **§7.1 Certificate rotation / CA reissue.** Trigger: an expiry
    inside the renewal margin, which every run of the stack names (§1);
    the expiry alert of §3, which by then means no run of the stack has
    happened since the margin opened; or key compromise. Outline:
    `credentials derived state-backend-server issue`, which reissues the
    server key and certificate under the same CA into the stack's
    configuration → commit → `operator-stack state-backend up --force`,
    which dumps the running box, replaces it with one carrying the new
    certificate and restores into it. The replacement is not optional:
    the server certificate rides in Ignition, so delivering it replaces
    the instance and its data directory with it — the same shape as
    §7.2, for the same reason. On compromise of the CA itself:
    `credentials derived state-backend-ca generate` for a new generation,
    the same reissue and replacement against it, redistribute the `ci`
    and `operator` bundles → `verify-full` check.
-   **§7.2 Postgres major upgrade.** Trigger: renovate major pin PR.
    Outline: merge → `operator-stack state-backend up --force` (the run
    dumps the old box before deleting it, the new major initdb's a fresh
    data directory, and the run restores into it) → `operator-stack
    state-backend plan` answering 0. The dump is the run's own step
    rather than one the operator has to remember, because the
    replacement destroys the box it came from; it is verified as it is
    taken, and a dump that fails leaves the old box serving.
-   **§7.3 Rebuild / DR drill (quarterly, automated in the ops repo).** §7.2
    minus the pin bump: launch a scratch micro from a rendered Ignition,
    restore the latest age-encrypted B2 object via the **drill key**
    (`state-backend restore <object> --identity-file <key>`, the form
    that needs no kit), verify, destroy — unattended, alert on failure
    (operations.md §4). One pass exercises B2 download, decryption,
    a launch from the Butane file, restore, and cert delivery. The *offline*
    age identity is proven separately by the yearly rotation (§7.4),
    which inherently decrypts with it. **As of 2026-09-25 the workflow is
    not built**:
    the drill key is generated into the ops repository's `drill`
    Environment (§5) and the drill's OCI and B2 keys are minted beside
    it (credentials.md §3), but the workflow that would read them is not
    written (`kluster-ops#57`), so as of 2026-09-25 nothing runs this on
    a schedule and no pass of it has happened. Nor would a written one
    start before that repository's Actions billing is restored
    (`kluster-ops#393`): until then no job there starts, `probes.yml`
    (§6) included. Until a pass does run, the same ground is covered by
    hand — §7.3.1, which
    opens the object with the kit, the `--identity-file` form being the
    workflow's.
-   **§7.4 age identity rotation.** Trigger: yearly cadence, key
    compromise, custody change. Outline: bump the generation pin
    (`settings.AGE_GENERATION`) to N+1, which brings the row
    `backup-age-<N+1>` into the register and swaps the Butane recipients
    `[N, N−1] → [N+1, N]` →
    `credentials derived backup-age-<N+1> generate` → note the rotation
    date and N−1's earliest-destroy date where the next offline day
    will read them (nothing stores either — §5) → commit the recipients
    file it wrote → `operator-stack state-backend up --force`, which
    dumps the box, replaces it and restores into the new one → verify
    both decrypt paths →
    destroy N−1 on its date by deleting its escrow ciphertext
    (compromise: the recipients are always the pin and the generation
    below it, so dropping N now is the pin at N+2 with
    `backup-age-<N+1>` and `backup-age-<N+2>` both generated, then a
    fresh dump; the objects written under N are deleted from the bucket
    by hand, which no command here does).

### 7.3.1 Restore rehearsal

**Status: run on 2026-09-18; its restore ran against the appliance on
2026-09-25.** The rehearsal of 2026-09-18 (`kluster-ops#281`) ran
steps 2 to 9 against a scratch box, on a dump an operator took with
`state-backend dump` from a workstation, because the bucket held no
object to take (`kluster-ops#385`). The dump opened with the escrowed
generation in the kit, the restore landed rows, a client with the
scratch bundle selected a stack, and the times were recorded. Of step
1, the minted key listed the prefix and found it empty; as of
2026-09-25 no object has been downloaded from it. The appliance's
first replace-and-restore, on 2026-09-25 (`kluster-ops#385`), ran
`state-backend restore` against production, on the dump the
appliance's earlier script took of the box it replaced: the
dump opened with the kit, went in as one transaction, and the
restored backend served `dns`, `github` and `physical`. Neither opened an object the appliance
uploaded itself, so as of 2026-09-25 that is unproven: step 1 as
written, and the decrypt of a nightly with the kit
(`kluster-ops#281`).

The rest of §7 is outline because a command carries the detail; this
one is written out because it proves `state-backend restore` against a
live box ahead of a replacement, and a replacement whose own restore
fails, or that took no dump of its own, leaves a backend holding nothing
until `state-backend restore` fills it (§1). So the rehearsal comes
*before* a replacement rather than during it, and it is the operator
form of the §7.3 drill: same object, same commands, a kit where the
drill would have had its own key.

**It runs against a scratch box, never against the appliance.** The
appliance is a singleton — the `state-backend` stack declares it and
owns the reserved address — so nothing here runs the stack, and the
scratch box is launched by hand from a rendered Ignition. What that costs is a second instance of the shape in the same
availability domain, the one that offers it (§1).

**What it establishes**, and the order the steps are in so that each
failure is cheap:

1.  **Mint a key that can read the prefix, and take the newest
    object.** Nothing already in service reads a dump: the appliance's
    own key holds `writeFiles` alone and can neither list nor read (§5),
    the B2 writer keys the cluster's backups run on are confined to
    their own prefixes, and neither the seed (credentials.md §2, where
    its grant is key and bucket administration) nor the management key
    (§3) carries file capabilities at all. The account master key
    could, and it is borrowed to re-seed and to nothing else (§2 as
    well). What the seed carries is `writeKeys`, which is how every
    prefix-scoped key on this bucket is minted — so the rehearsal mints
    a temporary one with `listFiles` and `readFiles`, confined to the
    dump bucket and the `pulumi-state/` prefix. That is the drill's own
    reader in every respect but where it lands: `credentials derived
    drill-credentials mint` mints that key from this same seed
    (credentials.md §3) into the ops repository's `drill` Environment,
    which no workstation can read back, so the rehearsal makes its own
    copy of the same grant by hand. Nothing in this repository fetches an object,
    so listing the prefix and downloading the newest `.dump.age` are
    `b2_list_file_names` and `b2_download_file_by_name` under it. Record
    the object's name, size and timestamp — the same stamp the dump-age
    probe of §6 reads, checked here by a person. Keep the file outside
    the checkout: it is every stack's state.
2.  **Render the scratch box.** `state-backend render --address
    127.0.0.1 > <scratch>/scratch.ign` — the command prints the
    Ignition rather than placing it. The address is the server
    certificate's SAN, and the drill reaches the box through an SSH
    tunnel so that the connection is `sslmode=verify-full` against a SAN
    that matches — the alternative, an ephemeral public IP no
    certificate names, would prove the restore under a weaker mode than
    the one every real client uses. A rendered Ignition carries
    placeholder B2 credentials, so the scratch box's own dump timer
    fails and uploads nothing: a drill cannot write to the bucket.
3.  **Launch it** with `oci compute instance launch --user-data-file`
    against the rendered file, in the appliance's compartment, subnet
    and availability domain, on the imported FCOS image, with an
    ephemeral public IP. **Give it a display name that is not the
    appliance's**: the box the stack declares carries that name, and a
    second one under it is a box an operator reading the console cannot
    tell from the appliance.
4.  **Open the tunnel**: `ssh -L 5432:127.0.0.1:5432 core@<scratch
    address>` with the operator key. The workstation's public key has to
    be in `src/kluster/lib/state_backend/machine/operator-keys.txt`, which is the file
    the render put on the box (§1). **This login is trust-on-first-use,
    on purpose.** The pin of §1 is the appliance's committed host key,
    and this box carries a key its render drew, so nothing pins it; what rides inside the tunnel is
    libpq under `verify-full` against the escrowed CA, which an
    interposer does not reach. The tunnel is transport here, not
    trust. Where 5432 is taken locally, forward
    another port and rewrite it in the bundle's `backend-url` below.
5.  **Write a scratch client bundle**: `state-backend bundle operator
    --address 127.0.0.1 --directory <scratch>/bundle`. **`--directory`
    is not optional here** — omitted, the bundle lands in the
    workstation slot and its `backend-url` becomes the address
    `mise.toml` hands every later `pulumi` run.
6.  **Restore**: `state-backend restore <object> --bundle
    <scratch>/bundle`, with no `--identity-file`, so the escrowed
    generation in the kit is what opens the object — the first thing the
    rehearsal establishes. The command refuses a backend that already
    serves stacks, so a bundle pointed at the appliance by accident
    stops here; that is a guard rather than the check, and reading the
    URL in the bundle first is the check. Its own steps are the next
    assertions: decrypt, list, `pg_restore --single-transaction`, and
    `pulumi stack ls --all` against what came back. Ownership is the
    scratch box's rather than the archive's (§7): everything lands
    owned by the role its client roles act as, whichever role the
    archive names.
7.  **Count the rows.** Point libpq — `psql`, which `mise.toml`'s
    `postgres` pin installs beside `pg_restore` — at the same bundle, with
    `PGSSLROOTCERT`, `PGSSLCERT` and `PGSSLKEY` naming `ca.crt`,
    `client.crt` and `client.key` in that directory (§3), and ask:

        psql "$(cat <scratch>/bundle/backend-url)" -At -c "
          select table_name,
                 (xpath('/row/c/text()', query_to_xml(
                     format('select count(*) as c from public.%I', table_name),
                     false, true, '')))[1]::text::bigint
            from information_schema.tables
           where table_schema = 'public' and table_type = 'BASE TABLE'
           order by table_name"

    **The assertion is the counts, and it has to be, because
    `pg_restore --list` does not detect truncation**: a 145 MB
    custom-format archive cut to 5 000 bytes still lists every table and
    exits 0, since the table of contents sits at the archive's head.
    What the listing does catch is a file that is not an archive, and
    what the stack count beside it catches is an archive whose restore
    brings nothing back (§5).
    A drill that only listed the object would have tested nothing about
    restoration. The counts are also what to write down: the next
    rehearsal reads them to see whether the state has quietly shrunk.
8.  **Select a stack**: `pulumi stack select <one of the names the
    restore printed>`, from the checkout, with `PULUMI_BACKEND_URL` set
    to the scratch bundle's URL. The restore proved the backend answers
    a listing; this proves an operator's own client opens a stack in it.
9.  **Record the elapsed time** — the download, the restore, and the
    wall clock across the whole sequence. That is what lets the bring-up
    runbook price the recovery path instead of assuming it.
10. **Destroy the scratch box, the key and the copies.** Terminate the
    instance, close the tunnel, delete step 1's temporary key with the
    seed's `deleteKeys` (`b2_delete_key`), and delete the downloaded
    object and the scratch bundle. `state-backend restore` decrypts into
    a temporary directory of its own, so the plaintext archive is
    already gone. The key is the piece that outlives a forgotten step
    worst: it reads every dump the bucket holds.

**What it does not establish**: the `--identity-file` path with the
drill key itself, which only the workflow that reads the Environment
can hand it; the unattended shape of §7.3, which is a
workflow rather than a sequence; and anything about objects older than
the one it opened — a generation still covers those by design (§5),
which is a claim the yearly rotation exercises (§7.4).

### 7.5 Operating the appliance

How to run the appliance day to day, where the rest of this document is
its design. The machine is the files in
`src/kluster/lib/state_backend/machine/`:

| Path | What |
| --- | --- |
| `butane.yaml.j2` (template) | The machine, whole: the Postgres unit — a plain systemd unit running `podman run` at the image pinned by digest, not a quadlet — PKI, `pg_hba` and the Postgres roles, the age recipients, the unit that installs the pinned `age`, the dump timer, the reboot window. |
| `state-dump.sh` | What that timer runs — `pg_dump` → `pg_restore`, refusing an archive that holds no stack → age → B2. Shell, because the box has no interpreter: it uses what the Fedora CoreOS image ships plus the `age` the template installs, and a test holds the template to that (§1). |
| `operator-keys.txt` | SSH keys for diagnosis (`state-backend ssh`). The box is never configured by hand, and a key absent here means no access until the next replacement. |
| `host-key.txt` | The box's SSH host key, public half. Written by `credentials derived state-backend-host-key generate` beside the private half it puts in the stack's configuration, and committed; `state-backend ssh` pins the box to it, and the stack refuses to plan while it is absent or names another key. |
| `backup-recipients.txt` | The backup generations' public halves, one line per generation the box encrypts to. Written by `credentials derived backup-age-<N> generate` and committed; `credentials derived check` holds it to the escrow, and the stack refuses to plan without it. |
| `drill-recipient.txt` | The public half of the drill age identity, one recipient. Written by `credentials derived drill-age-identity generate` — which pushes the private half into the ops repository's `drill` Environment first — and committed; absent until that generator has run, and the appliance then encrypts to the escrowed generations alone. |

The code that renders them, dumps and restores the state and reads the
committed files is `kluster.lib.state_backend`, beside them; the stack
that declares the appliance is `kluster.components.state_backend`,
built by the `state-backend` stack program; and the code that renders a
scratch box, logs in, writes bundles and probes is
`src/kluster/scripts/state_backend/`, exposed as the `state-backend`
console script.

#### Running the stack

Runs on the workstation that holds `.credentials/` — the operator
passphrase, which opens the stack's configuration and state, and the
`operator` client bundle the hooks connect with — from its primary
checkout, whose working copy holds the forge's `main`
([framework/pulumi.md](../framework/pulumi.md) §3.3):

```sh
operator-stack state-backend plan          # a refreshed preview; writes nothing
operator-stack state-backend up            # applies what is planned, once asked
operator-stack state-backend up --force    # and a create, replacement or delete of the box
```

`plan` names every difference between the commit and what OCI and B2
hold, the refresh reading what a hand changed. `up` applies a repair in
place with the box left serving; one that would create, replace or delete
the box writes nothing, names what moved and `--force`, and exits 1.
`up --force` dumps the box before it goes, replaces it and restores
into the new one; `up --replace` does the same when nothing moved. A
run that wrote leaves the checkpoint under `checkpoints/` changed in the
working copy, to land like any change.

The statuses are an interface, so a script can branch on them
(`operator-stack --help` says the same):

| Exit | What happened | What to do |
| --- | --- | --- |
| `0` | Nothing planned, or what was planned applied, and the backend serves its stacks. | Land the checkpoint, where the run wrote one. |
| other | `pulumi up` itself failed, and the backend serves its stacks: the status is `pulumi`'s own. | Read its output; land the checkpoint it wrote, which records every step that finished. |
| `1` | Something is planned and nothing was applied: a plain `plan`, an `up` not confirmed, or one holding a replacement of the box for `--force`. | Read the plan; run `up`, or `up --force` for the box. |
| `2` | A refusal or a failed check: the working copy behind the forge's `main`, a conflicted checkpoint, no `operator` bundle in its slot, a create beside a held address or a replacement of an adopted resource (§1), or a checkpoint carrying a secret in the clear. | Read the refusal; it names the repair. |
| `3` | The estate's backend answers and serves no stack: a box owed a restore, or a site before its first `pulumi stack init`. | `state-backend restore <file>` of the dump the replacement took, or the newest nightly one; a new site needs only its first `stack init`. |
| `4` | The estate's backend does not answer at the address the run names. | `state-backend ssh` reaches the box for diagnosis (§6). |

`3` reads the backend itself rather than a record on one workstation,
so every workstation reads it the same way, and over a backend that
serves no stack it is the answer whatever else the run did, a failed
`pulumi up` included: only `2` keeps its own answer, a checkpoint that
must not be pushed. `4` likewise stands in for `0` and `1`, while a
failed `up` keeps `pulumi`'s status. The backend is asked over a
connection that must open within a few seconds
(`state.CONNECT_TIMEOUT`), so an address whose traffic is dropped is `4`
in seconds rather than when TCP gives up.

#### The cutover

The appliance serving when the stack is first applied was built by a
script, so the stack's first run adopts what that script built and
replaces the box (rfc-006 §14, slice 6). `state-backend adopt` reads the
ids of the VCN, its gateway and subnet, the reserved address, the image
bucket and the dump bucket, under the names the stack declares, and
writes them into the stack's configuration in the clear, all of them or
none: the program refuses a partial set, since a name left out is a
second resource beside the first. The program imports each through the
component, with its secret markings. Three things are not imported. The
instance, whose `metadata` would be recorded in the clear. The image:
OCI returns no image's source, so an imported image differs from its
declaration and is replaced, and the engine refuses to replace a
resource whose declaration carries an import id — the driver refuses
such a run before it starts, naming the resource. And the dump key the
script minted. The stack makes its own of each, and the script's key,
image and its object, the old security group and the box are deleted or
terminated by hand.

Other commands:

```sh
state-backend render --address <ip>   # a scratch box's Ignition, without touching the cloud
state-backend bundle ci --address <ip>  # the CI client certificate and its URL
state-backend pins                    # verify the pinned digests (CI runs this)
state-backend dump                    # a dump of the live state, encrypted like the nightly one
state-backend restore <dump>          # feed a dump into a box that serves no stack
state-backend probe                   # the scheduled checks, run by the ops repository
state-backend ssh                     # a diagnostic login; the box is never configured by hand
state-backend adopt                   # the cutover's ids, into the stack's configuration
```

#### Connecting

The backend speaks TLS with **mandatory client certificates**, and clients pin
the server by literal IP (`sslmode=verify-full`) so the hot path never depends
on DNS — which is itself something this backend deploys.

Nothing has to be exported by hand. `mise.toml` reads the checkout's
`.credentials/` and sets all five variables a `pulumi` run needs, so a command
run through `mise` is already connected:

```sh
mise x -- pulumi stack ls
```

`PULUMI_BACKEND_URL` is the string in the bundle, and it names the appliance
and none of the files:

```sh
postgres://operator@<ip>:5432/pulumi_state?sslmode=verify-full
```

The three files travel beside it as `PGSSLROOTCERT`, `PGSSLCERT` and
`PGSSLKEY`, resolved from the same slot the URL came from. That is the one
channel both libpq and the driver behind Pulumi's Postgres backend read, and
it is why no path is expanded inside a connection string: paths in the string
would make the recorded copy true of one directory on one machine. A bundle is
usable wherever its files are, so moving a checkout invalidates nothing;
`state-backend bundle operator --address <ip> --directory <where>` writes one
somewhere else when that is wanted.

`PULUMI_CONFIG_PASSPHRASE` comes from `.credentials/pulumi.passphrase`, which
`credentials derived pulumi-passphrase recover` writes from the escrow using
the kit's recovery key. A checkout that has the bundle but not the passphrase
needs that one command and nothing else.

The certificate's Common Name *is* the Postgres role: `operator` locally,
`ci` in the pipeline. Neither is a superuser: both act as the role that owns
the state, which holds what Pulumi's Postgres backend needs and nothing more,
and the box admits no other role over TCP. The superuser answers only on the
container's local socket, which is how the box's own initialization and its
dump timer reach it (§2).

#### Changing it

**A run of the stack is the apply path.** Nothing on the box is mutated in
place: a change is a PR against `butane.yaml.j2`, the pins in
`src/kluster/lib/state_backend/settings.py` or the stack's configuration,
then `operator-stack state-backend up --force` for a change the box is
rendered from — minutes of downtime on 5432, which CI retries through,
and which local runs re-run — and a plain `up` for anything else. SSH exists for
diagnosis only.

Rotating the server certificate is `credentials derived
state-backend-server issue` and then that same `up --force`: the reissue
writes the new key and certificate into the stack's configuration, and
the replacement carries them to a new box, dumping the old one and
restoring into the new one on the way.

Because the OS and Postgres both follow their streams automatically, and
because the machine carries nothing that `pg_dump` plus a replacement cannot
rebuild, "the repo describes the box" stays true without a configuration agent
to enforce it.

#### Losing it

The daily dump is read on the box before it leaves it — an archive holding no
stack checkpoint is refused and nothing is uploaded, since its restore would
bring nothing back: a box replaced and not yet restored, or a site before its
first `pulumi stack init`, so the newest object is always the last dump that
held state (§5) — and age-encrypted to the
recipients the stack renders into
the Butane file, one kind of recipient per reader. The escrowed
`backup/age/<generation>` identities serve the operator: random at creation,
their only stored copies the ciphertexts under `escrow/`, which the kit's
recovery key opens, so `state-backend restore` on the workstation opens a dump
through the kit. The drill recipient on file beside the template serves the
quarterly rebuild drill: its private half lives in the ops repository's `drill`
Environment and nowhere on disk, and the drill's `state-backend restore
<object> --identity-file <key>` needs no kit. Which generations are recipients,
how each kind rotates and why the drill key needs no generational pair is
§5; the register rows for both keys and the
drill's own OCI and B2 credentials are credentials.md §3. The dump lands in
B2 under a prefix whose lifecycle rule enforces retention. Recovery from a lost
box is an `up --force`, which launches a new one and exits 3 on an empty
backend, followed by `state-backend restore` of the newest object — the
path the drill is designed to exercise (§7.3),
and the operator form of it, run by hand against a scratch box with the kit, is
§7.3.1. As of 2026-09-25 the drill workflow is not written
(`kluster-ops#57`). The operator form has run: the §7.3.1 rehearsal on
2026-09-18 opened a workstation dump with the kit (a dump encrypted to the same
escrowed generation as the appliance's), restored it into a scratch box with its
rows, and selected a stack against it (`kluster-ops#281`); and the first
production replace-and-restore followed on 2026-09-25 (`kluster-ops#385`). Two
halves of the path are unproven as of 2026-09-25, and so assumed broken rather
than known to work: opening, with the kit, an object the appliance uploaded
itself, and the drill key's `--identity-file` restore.

**A lost stack state** — only with every clone of the repository, since
every checkpoint that ever landed is in `main`'s history — costs one
replacement (rfc-006 §3.6). What the cutover adopts is adopted again
the same way, `state-backend adopt` reading the ids by name; the image is
imported afresh, the old one deleted by hand; the instance is dumped, terminated by hand and
launched again by `up --force`, with the dump key beside it, since an
imported key has no secret. A **stale** checkpoint is refused by the driver before a
run starts (framework/pulumi.md §3.3), and a **corrupt** one is reverted
by a commit, after which a refreshed `plan` shows what the revert does
not know about.
