# RFC 008: The Workload Migration

*   **Status:** Proposed, 2026-11-05. The operator ruled on an outline of
    this design on 2026-11-05. hath moves directly after Wave A2 has
    soaked (§11.1). The unit of migration in §3, the shapes in §4, the
    wave order and the slices in §10 stand as written, together with
    every decision the outline listed as settled.
*   **Created:** 2026-11-05
*   **Authority:** AGENTS.md,
    [framework/dispatch.md](../framework/dispatch.md),
    [framework/rfc.md](../framework/rfc.md) and the style rules
    (`docs/style/`) are what this document obeys. A rule proposed here
    that they do not state is marked **new rule** where it is stated.
*   **Companion:** [rfc-003](rfc-003-dns-and-github-stacks.md), for the
    route census, the record blocks and the seam between the stack that
    declares a route and the stack that writes its rewrite (rfc-003 §6);
    [rfc-007](rfc-007-k8s-base-and-apps.md), for the application base,
    `route`, the sealed values and where a sealed value lives (rfc-007
    §6.2, §9), and for the three decisions it left to the migration's
    design (rfc-007 §15.3). Their precedents are cited at each use rather
    than re-argued.
*   **In scope:**
    1.  the unit of migration: how an application's component is built
        before its wave, deployed beside the legacy name, verified, and
        given the name, and how each step is rolled back;
    2.  the order in which the deploy chain applies `dns` and `apps`;
    3.  the shared shapes the applications are built from: public ports,
        LAN ports, names for raw ports, the authentication gate, backed
        claims, NAS datasets, node volumes, databases, single sign-on,
        static sites, and the secrets the migration carries;
    4.  per wave, each application's component, its data move, its
        cutover with its DNS, its verification and its rollback;
    5.  the homelab worker's memory across the waves: when it grows, by
        how much, what is verified first, and how a rollback after a
        growth is run;
    6.  the three decisions rfc-007 §15.3 left to this design;
    7.  what the decommission inherits from the waves;
    8.  the slices.
*   **Out of scope:**
    *   **The decommission itself**: the legacy cluster's teardown, the
        VPS's cancellation, the trackers' retirement, the sealing key's
        rotation and the worker's final sizing. migration.md §4 lists
        them, and the milestone that retires the legacy estate opens with
        a design of its own. §5.8 says what it inherits.
    *   **The GPU's allocation mechanism in the cluster.** It is
        `k8s-base`'s ([declarative/cluster-infra.md](../declarative/cluster-infra.md)
        §1 item 7). An application here requests the A380 through
        whatever that stack offers when Wave C opens (§5.6).
    *   **The A380's passthrough to the worker.** It is the host's
        ([physical/homelab-host.md](../physical/homelab-host.md) §3).
        §5.6 schedules it; this document does not redesign it.
    *   **The operations repository's alert poller.** The read route it
        consumes lands with it (§5.5), and the poller is that
        repository's workflow.
    *   **What stays on the host or the gateway**: AdGuard, HAOS,
        dmarc-check, the NAS role and the legacy state backend
        ([cluster/migration.md](../cluster/migration.md) §2, "not
        migrating").
    *   **The applications retired outright** (migration.md §2), which
        never land.
    *   **The upgrade of immich's `vchord` extension.** It runs once,
        on the new cluster, after every workload has moved.
    *   **Edits inside kluster-code**, the legacy repository. Each wave
        needs two of them (§3.5), and they are that repository's pull
        requests; the slices here name them.

--------------------------------------------------------------------------------

## 1. Context and problem statement

The `apps` stack declares two providers and no application. Every
workload the installation serves runs on the legacy cluster, behind the
legacy VPS's ingress, and the homelab host runs both clusters at once
for as long as the migration lasts. That coexistence is the cost this
milestone exists to end: the homelab host has no spare memory while the
legacy cluster holds its share (§6.1), the VPS is a monthly bill, and
every day both run is a day on which a change has two places to land
([sources-of-truth.md](../sources-of-truth.md), rule 1).

Most of the migration was decided before this document.
migration.md orders the waves and names each storage kind's data move.
[declarative/workloads.md](../declarative/workloads.md) fixes the
application contract: each component owns its namespace, workload,
storage and backups, its exposure and its DNS records. rfc-007 builds
the application base and `route`. What is left is of four kinds:

*   **A step the canon states that no merge can take.** migration.md §0
    rule 1 verifies an application before its DNS is cut, while
    [declarative/dns.md](../declarative/dns.md) §6 makes the deploy and
    the cut one pull request, and a merge to `main` is an apply of
    `apps`. The same merge also hands each name from `dns`, which deletes
    its legacy block, to `apps`, which creates its record, and the deploy
    chain runs those two stacks in parallel, while Cloudflare refuses
    the second record while the first stands (§3.4).
*   **Shapes the contract names and nothing builds**: `public_port`,
    `backed_pvc`, the database pattern, the static site, the
    authentication gate. Beside them are the shapes the legacy estate
    needs that the contract does not yet name: LAN-side raw ports, names
    for raw ports, NAS datasets, the single sign-on clients.
*   **Facts the legacy estate holds that the canon does not.** Its
    database backups are in Google Cloud Storage, not in the backup
    bucket. Its authentication service's users and second factors are
    backed up nowhere. One application's data sits on the shared
    JuiceFS volume, which migration.md §3 treats as the syncthing
    replica's alone. The selectorless backend workloads.md §4 designs
    for Home Assistant is one a cloud node cannot reach. The mail
    relay the authentication service sends through is scheduled a wave
    after it. Each is settled where its wave is (§5).
*   **The memory interleave, against measured numbers.** migration.md §0
    rule 4 grows the worker as legacy releases memory, towards
    [cluster/nodes.md](../cluster/nodes.md) §4.2's 20 GiB, and a
    rollback after a growth has nowhere to start the legacy application.
    The host's readings of 2026-11-05 put the ceiling below that end
    state (§6.1).

The milestone is also the long pole: the platform below it is built,
and stands up live directly after the bring-up. So this design is
written to be **built before the cluster is live**. Every component and
shape is constructed and tested under Pulumi's mocks, merged, and
waiting; each wave then costs only the small live steps of §3.

--------------------------------------------------------------------------------

## 2. What is inherited, and what is decided here

| Precedent | Applied here |
| --- | --- |
| Stop-copy-start per application, verify before the DNS cut, rollback by scaling the legacy application back up (migration.md §0 rule 1) | The unit of migration: build, land, verify, cut (§3) |
| One `route(row)` per HTTP exposure, the row's `Exposure` deciding gateways, record and rewrite; the rewrite is `dns`'s, from the same row (dns.md §3, §5; rfc-003 §6) | The legacy-held state, the apex row, the raw-port and public-and-IoT exposures (§3.3, §4.1) |
| Every record of a type the proxy can front is proxied unless it states why not, at its declaration (dns.md §2) | Route rows carry the same reason before the first direct name migrates (§4.1) |
| `legacy.py` is one block per application, deleted whole at its migration (dns.md §6) | The block goes at the cut, not at the deploy (§3.2) |
| A merge to `main` is an apply of `apps`, and `dns` and `apps` run in parallel on it (framework/ci.md §3; sources-of-truth.md §3) | `up-apps` waits for `up-dns` (§3.4) |
| A component receives the census it acts on; a census read by more than one program is a convention ([style/pulumi.md](../style/pulumi.md), "Data") | Each application's component receives its rows; the LAN ports, the NAS datasets, the single sign-on clients and the backed namespaces are censuses in `conventions` (§4) |
| A sealed value is a plain value in the declaring stack's configuration; `conventions.sealed` lists what `credentials` seals (rfc-007 §6.2) | The legacy ciphertexts and the backup credentials (§4.6) |
| The legacy sealing key is restored at the first ported manifest, its channel left open (rfc-007 §6.3, §15.3) | A one-off carry at Wave A1's head, retired by the decommission's rotation (§4.6) |
| How hath leaves on its own address is the migration's choice, the reserved address on its node's primary private address recommended (rfc-007 §15.3) | Taken (§5.4) |
| Which name the alert read route answers on, and whether `k8s-base` publishes a record (rfc-007 §15.3) | `mon`, published by `k8s-base` (§5.5) |
| The worker grows as legacy releases memory, toward 20 GiB (migration.md §0 rule 4, nodes.md §4.2) | Grown to fit, in drained windows, with a reading before each (§6) |
| A flip keeps the live service's feature parity unless its row records otherwise (sources-of-truth.md rule 4) | Each application's cut, and the two arrangements recorded in §5 |
| CNPG on local-path with barman to the backup bucket, a restore drill inherited from the legacy discipline (workloads.md §4) | One instance per database, recovery from the legacy store, the drill ported (§4.3) |

--------------------------------------------------------------------------------

## 3. The unit of migration: build, land, cut

### 3.1 A component merges before its application moves

**An application's component merges unwired.** Its module, its
parameters and its tests land in a pull request of their own, and the
`apps` program does not call it. A merge to `main` is an apply of `apps`
(framework/ci.md §3), and a component no program calls declares
nothing, so the merge changes nothing live. **New rule**, landing in
migration.md §0 and sources-of-truth.md §3, whose sentence "a component
for an application that has not migrated does not merge" it replaces
with "is not wired".

Two properties follow, and they are what make the milestone buildable
ahead of the cluster:

*   **Every component is built and reviewed under mocks** before its
    wave, many at once, each owning only its own package and test
    module (§10). Nothing about a build depends on the cluster being up.
*   **A component receives its census rows as parameters**, as
    style/pulumi.md asks of every component ("A component receives the
    census it acts on"). Its build slice tests it against rows of the
    test's own, and its row in `conventions.routes` arrives with the
    wiring, in the wave that lands it. The build therefore touches no
    census, and no two builds share a file.

### 3.2 Land, verify, cut

An application then moves in two merges and one verification between
them:

1.  **Land.** The legacy application is stopped (§3.5) and its data
    moved (§5, by storage kind). The land pull request wires the
    component into the `apps` program and adds its route rows in the
    legacy-held state (§3.3). The application runs on the new cluster;
    its route attaches to the Gateways; no public record and no rewrite
    names it, so the name still answers where the legacy estate answers
    it.
2.  **Verify.** The name is requested at the cluster's front door
    directly — `curl --resolve <name>:443:<address>`, at the balancer's
    address for a public row and at the LAN VIP for a LAN-side one. The
    Gateway matches the route by hostname, and the zone's wildcard
    certificate, which the cluster holds for every zone a row names
    (rfc-007 §5.2), answers the handshake, so the request reaches the
    new application exactly as a client's will after the cut. Each wave
    in §5 says what its applications are verified by.
3.  **Cut.** The cut pull request deletes every legacy declaration of
    the name and takes the row out of the legacy-held state, so `apps`
    publishes the record and `dns` the rewrite. The legacy declarations
    are, by kind: the application's block in `legacy.py`; the web
    origin's address, for an apex (§4.5); a `$dnsrewrite` rule on the
    resolvers' declared list for the same name (sources-of-truth.md, row
    R2); and a row of the gateway proxy's `conventions.gateway.LEGACY_VHOSTS`,
    for a name under the retiring LAN zone, which `physical` serves
    until the cut and whose apply waits on that stack's reviewer.

**Why two merges.** Verification has to run against what is deployed,
and nothing is deployed until a merge applies it; the cut has to wait
for the verification. One merge cannot both. **New rule**, landing in
migration.md §0 rule 1 and dns.md §6, whose "three files in one pull
request" becomes the cut's pull request, with the component's wiring
moved to the land's.

**A window's applications land together and cut together.** Stopping
several applications in one window costs one drained apply instead of
several, and the cut's pull request carries every name that verified. An
application that does not verify is left out of the cut, and its land
is reverted on its own (§3.6).

A LAN-only application whose new name differs from its legacy one —
every name under the retiring LAN zone becomes a name under a public
zone (dns.md §4) — needs no legacy-held state: its new name has nobody
else's declaration to collide with, so its row lands published, both
names answer until the cut, and the cut deletes the vhost row alone.

### 3.3 The legacy-held route

**A route row may be held by the legacy estate**: `Route` gains a field
that says so. A held row's route attaches to its Gateways, and the row
publishes no record and no rewrite; `served_zones` still counts its
zones, because verification needs the zone's certificate. **New rule**,
landing in dns.md §5.

**A row is held exactly while `dns` points its name at the VPS.** The
invariant is a relation between `conventions.routes.ROUTES` and the
`dns` stack's records — a row is held if and only if a record that
addresses the VPS, in `legacy.py` or in the web origin's pair, holds the
row's host in one of the row's zones — and it is held in
`tests/test_conventions.py` with the census invariants, against a census
that breaks it in each direction. A land that forgets the field, or a
cut that deletes the block and keeps the field, is red there.

**Derivation was weighed and loses.** Deriving the state from
`legacy.py` would make the illegal state unwritable, but `apps` would
then read the `dns` stack's record blocks, which makes them a census of
two readers that belongs in `conventions` (style/pulumi.md, "Data"),
and moves the record types with them — for a fact that exists only
until the migration ends. The field is the cheaper statement, and the
relation test makes its one illegal state red.

**The field leaves with the last block.** It is a migration-period
statement: when `legacy.py` is empty, the field and its relation test
are deleted together, and nothing else changes. Its boundary is the
field, the branch in `route` and in the rewrite derivation that reads
it, and that test.

### 3.4 The record changes hands in one run

A cut deletes the legacy record in `dns` and creates the application's
record in `apps`, at the same name. Two facts force their order:

*   the pinned Cloudflare provider's `DnsRecord` has no option to adopt
    or overwrite a record that exists (`pulumi-cloudflare` 6.22.1, whose
    record arguments carry no such field), so each stack creates and
    deletes its own;
*   Cloudflare refuses a CNAME beside another record of the same name,
    so a create that runs before the delete fails.

**So `up-apps` runs after `up-dns`, whatever `up-dns`'s result.** In
`deploy.yml`, `up-apps` needs `up-dns` beside `up-k8s-base`, and its
condition requires only `up-k8s-base`'s success. A cut's run then
deletes before it creates, and the name is absent for the time between
them. A run in which `up-dns` failed still applies `apps`: a failing
`dns` — the stack that reaches the LAN — never blocks the daily driver,
which keeps the separation dns.md §3 put there. Only a cut whose
`up-dns` failed then fails in `up-apps` too, on Cloudflare's refusal,
and the next run converges it. **New rule**, landing in
framework/ci.md §3.

**Adoption was weighed and loses.** A record kept by `dns` with
`retain_on_delete` and imported by `apps` would change hands with no
gap, but an import is refused unless the declared inputs match the
record as it stands, so retargeting it takes a second apply of `apps`,
and the import needs a record ID the program would have to look up. The gap it saves is a minute, inside a
window in which the application is already stopped.

### 3.5 Stopping and removing the legacy side

**A legacy application is stopped by a kluster-code change, not by
`kubectl scale`.** kluster-code's deploy applies on every merge to its
`main`, so a scale made by hand is undone by the next merge there,
whatever it is about. The stop sets the application's workloads to no
replicas and keeps every claim it holds. It merges at the head of the
window, before the data move.

**It is removed by a second kluster-code change, after its cut has
soaked**: the component goes, and its claims' directories are mapped
or deleted in the retained-volume census storage.md §3.3 asks for. That
removal is also what releases the application's memory for the next
growth (§6.3).

Both are kluster-code pull requests, which the operator merges; the
slices of §10 carry them.

### 3.6 Rollback

| Where it fails | The rollback |
| --- | --- |
| Before the land merges | Revert the stop in kluster-code; nothing here changed |
| After the land, before the cut | Revert the land: `apps` deletes the workload, and no record or rewrite was ever published. Then revert the stop |
| After the cut | Two merges, in order. First the row goes back to legacy-held, so `apps` deletes its record and `dns` its rewrite. Then the legacy declarations come back, so `dns` recreates the block. Then the stop is reverted |
| After a growth of the worker | The window reversed (§6.4), before the legacy application can start |

A single revert of a cut is not a rollback: its run would recreate the
legacy block while the application's record still stands, and
`up-dns` would fail on Cloudflare's refusal.

**Data written to the new application after its cut is not carried
back.** Rule 1's rollback restarts the legacy application on its own
data, which the stop left intact, and that is the rollback's cost for
each application with state; each wave names its own.

--------------------------------------------------------------------------------

## 4. The shared shapes

Each shape is designed once and built in its own slice before any
application uses it. Placement on the application base or in
`kluster.lib.k8s` follows rfc-007 §9.1: what one stack calls lives with
the base, what two call lives in the library.

### 4.1 Exposure

*   **`public_port`** is written as workloads.md §1 states it, by its
    first users: stdiscosrv, the cloud syncthing replica and hath. It
    emits one LoadBalancer Service for a row of the public port census,
    under `Cluster`, without node ports, carrying the pool label and
    the addresses its front asks for.
*   **The route census states the reason a name is published direct.**
    `Route` takes `unproxied: str`, empty for a proxied name, and
    `proxied` is derived from it, as the record blocks already do
    (dns.md §2). A row published direct, and every record it aliases,
    hold the same chain relation the blocks do: a direct name resolves
    through no proxied record. The co-host check holds that no row names
    the website co-host zone's labels as one of the primary's
    applications. Each is tested against a census that breaks it, since
    the census is empty until the first land.
*   **Two exposures join `Exposure`**, each the value its docstring says
    an application would add rather than a flag:
    *   `SPLIT_IOT` — a public name, and the LAN rewrite answered by the
        media VIP rather than the LAN one. Jellyfin is public today and
        reached by the IoT VLAN's televisions; both stay (sources-of-truth.md
        rule 4).
    *   `PORT` — a public name for a raw port: the record, with its
        `unproxied` reason, and no route, no rewrite and no certificate.
        It is how `syncapi` and `btsync` are published, which name ports
        the balancer forwards rather than routes. `served_zones` skips it.
*   **An apex row publishes no record from `route`.** A zone apex cannot
    be a CNAME, so a website's apex is an address, and it is the `dns`
    stack's web origin block (dns.md §2) that holds it, read from the
    balancer's addresses as the anchors are. The row for an apex
    attaches its route and leaves the record to that block. **New
    rule**, landing in dns.md §2.
*   **A LAN port** is a LoadBalancer Service on the `lan` pool for a raw
    TCP or UDP port a LAN client reaches: an IMAP proxy, a printer's
    API, a peer port. It shares the default LAN VIP with `lan-gw`,
    under a sharing key, so that the ports, not new addresses,
    distinguish the Services, and the resolver rewrite that already
    answers a LAN-side name with that VIP serves the port too. The
    ports are a census beside the public one, `LAN_PORT_CENSUS` in
    `conventions.cluster`, because `apps` declares each Service and the
    census invariant that no two rows, and no Gateway listener, take one
    port on one VIP is the same table's; a row that names a host of its
    own makes `dns` write that host's rewrite. **New rule**, landing in
    cluster/architecture.md §3.6 and workloads.md §1.
*   **`auth=True`** puts the ExternalAuth filter on the route, pointed
    at Authelia's `ext_authz` endpoint (cluster-infra.md §2), so a
    request the authentication service does not admit never reaches the
    application. The authentication canary that cluster-infra.md §2
    requires, and that rfc-007 §7.2 left waiting for the authentication
    service, lands with Authelia's component (§5.2).

### 4.2 Storage

*   **`backed_pvc(name, size, retention)`** is written as workloads.md
    §3 states it: a `local-path` claim, and a VolSync
    `ReplicationSource` with the restic mover, scheduled by the
    retention class, to the repository at
    `conventions.volsync_repo_path`. Its repository Secret is the
    namespace's backup value (§4.6) under the claim's own name, with the
    repository's path beside the sealed keys in the SealedSecret's
    template.
*   **A NAS volume** is an NFS `PersistentVolume` and its claim, for a
    dataset of a new census, `conventions.nas.DATASETS`: the dataset's
    export path and whether the consumer may write. The server is the
    host's address on the worker's own leg, and the volume is
    scheduled only to the worker, since no other node can reach the
    export (physical/homelab-host.md §5). A row's coupling to the host
    is stated on the row: the export is aconfmgr's, and a dataset the
    export list does not name is a mount that fails at the land (§5.1).
*   **A node-volume claim** is a `local` `PersistentVolume` over a
    volume of `conventions.NODE_VOLUMES`, at a directory inside the
    mount and never the mount point, with node affinity on the volume's
    label, claimed by an ordinary claim (workloads.md §4, block-volume
    application). The directory is created by a seeding step the
    component declares, so a volume that failed to mount leaves the pod
    pending rather than writing into the node's root.
*   **Every claim whose data the program does not back up says so with
    its reason** (`backup=None`, workloads.md §2): caches, the
    preserved node volumes, and the NAS datasets, whose safety net is
    the NAS's own regime.

### 4.3 Databases

A database is a CNPG `Cluster` on `local-path`, archiving through the
barman plugin to the backup bucket at `conventions.barman_repo_path`,
with its scheduled backup and retention taken from a retention class
(workloads.md §3). Three decisions are taken here.

**One instance, `instances: 1`, with no standby.** The second instance
a CNPG cluster can run would be scheduled to the same machine as the
first: every database here is pinned to one site, and the homelab site
has one worker. It would share the primary's node, disk and failure
domain, and add no availability against any failure the installation
plans for, while costing its whole memory request a second time —
legacy immich's database runs two instances on its one node, each
requesting 2 GiB. Durability is barman to the backup bucket, and the
restore drill below is what proves it. **One cluster shared by both
databases** would not fit their placement either: splitpro's database
runs on the cloud pool beside it (Wave A2), immich's on the worker
(Wave C), and immich's needs the `vchord` image, splitpro's the
`pg_cron` one. **New rule**, landing in workloads.md §4.

**A migrated database bootstraps by recovery from the legacy object
store.** The legacy databases archive to Google Cloud Storage, one
bucket per database, and the new cluster recovers from it through the
barman plugin, with the legacy credential ported as a namespace-wide
ciphertext (§4.6) — migration.md §3's "barman restore, not dump/restore
reinvention", against the store the legacy cluster actually writes. The
recovery runs once, at the land; the cluster then archives to the
backup bucket under the new layout, and the legacy store is read by
nothing after the cut. Whether the plugin recovers a backup that the
legacy in-tree archiver wrote is a live reading (immich's, §5.6).

**The restore drill ports with the shape.** The legacy cluster's drill
recovers a throwaway cluster from the latest backup, checks that it has
left recovery and that its rows and extensions are there, and deletes
it. The shape carries it for every database it declares, at
[operations.md](../operations.md) §4's monthly cadence, as a `CronJob`
with the role it needs in its namespace and a rule that alerts on a
failed or missing run, with its tier and playbook. A drill that stops
running then pages instead of passing silently, which is the freshness
cover operations.md §4 asks of an in-cluster drill.

### 4.4 Single sign-on

**The OpenID Connect clients are a census**, `conventions.sso.CLIENTS`:
each client's identifier, its redirect addresses, its authorization
policy and its authentication method. Two programs read it: `apps`,
which renders Authelia's client list and gives each application its
own client, and `k8s-base`, whose monitoring component gives Grafana
its client. A redirect address is built from the client's route row,
so a client and the name it redirects to cannot drift apart. The
clients' secrets are not in the census; each is a value sealed into the
configuration of the stack that declares its consumer (§4.6).

Authelia's access-control rules for the forward-auth applications are
its own configuration and stay in its component; the census holds only
what a second component has to agree on.

### 4.5 Static sites

The `StaticSite` component of workloads.md §4: a stock static server
with several hostnames, one route per zone, parameterized by its
content source.

*   **A git source** pulls a built branch through a git-sync sidecar
    into an empty directory. The blog instance runs two replicas across
    the cloud nodes, and carries the matrix delegation's two static
    files and the CORS header the route adds to them with a
    `ResponseHeaderModifier`.
*   **A NAS source** mounts its dataset read-only (§4.2), on the worker.
    The doors instance serves three hostnames from it.

The apex of each website zone is the `dns` stack's web origin (§4.1),
retargeted to the balancer at the cut. Its `www` becomes the static
site's own row, a CNAME to the cluster anchor, and the web origin block
splits: the served zones' apex moves at the cut, and the parked zones'
pair stays on the VPS until the decommission (dns.md §2).

### 4.6 Secrets

*   **The legacy ciphertexts port as they are.** Every legacy secret an
    application keeps is a SealedSecret sealed namespace-wide against
    the legacy key; its ciphertext is copied into `Pulumi.apps.yaml`'s
    `sealedSecrets` (rfc-007 §6.2), and the component declares it
    through `sealed_secret`, whose default scope is namespace-wide for
    exactly these. A namespace-wide ciphertext opens only in the
    namespace it was sealed to, so **an application keeps its legacy
    namespace's name wherever a ciphertext is ported into it** — the
    rule the mail relay's namespace already follows
    (`conventions.sealed.DKIM_EXIM`). Where the namespace changes, the
    value is sealed afresh against the cluster's own key: Grafana's two
    values, whose namespace becomes the monitoring component's. **New
    rule**, landing in workloads.md §5. These are per-application
    secrets, which the program enumerates and the register does not
    ([credentials.md](../credentials.md) §1, rule 8).
*   **The legacy sealing key is carried by hand once**, at Wave A1's
    head, from the legacy cluster's controller to the new one's, by a
    pipe between the two clusters' API servers that writes it to no
    file; the controller is restarted to read it. After that, the
    certificate the controller serves is confirmed to be the cluster's
    own, since a restored key can become the one that seals (rfc-007
    §6.3). It needs no channel of its own: it is a transitional
    credential whose last use is the decommission's re-sealing, which
    deletes it. Its register row says exactly that. **New rule**,
    landing in credentials.md §3.
*   **Each namespace that backs anything up has one backup value**, a
    row of `conventions.sealed` built from the backed namespaces of
    `conventions.backup`. It holds the namespace's B2 writer keys, one
    per prefix it writes (VolSync's and barman's), and its restic
    password. `credentials` mints the keys, generates the password,
    escrows the password (credentials.md §2.2), and seals the whole value
    namespace-wide, so that every repository Secret in the namespace
    reuses one ciphertext under its own name.
    *   **The password is escrowed** because a restic repository whose
        password is lost is unreadable, and the cluster's sealing key is
        escrowed nowhere (rfc-007 §6.1). A sealed copy alone would make
        the backups unreadable exactly when they are needed, after the
        cluster is lost.
    *   **The keys are sealed, and never configuration secrets**: under
        the stack passphrase they would reach every pull request's
        preview (credentials.md §1), and a writer key with the password
        reads every backup in its prefix.
    *   The register's two pending rows change to say so: the restic
        password is generated by `credentials`, escrowed and sealed,
        rather than generated in state. **New rule**, landing in
        credentials.md §3 and cluster-infra.md §1.1.
    The mint seals against the running controller, so it is an operator
    step at the head of each wave whose applications back anything up,
    and the ciphertext travels in that wave's land pull request.

--------------------------------------------------------------------------------

## 5. The waves

Each wave lists its applications in the order they land. For each:
what the component declares beyond its shapes, how its data moves, how
it is verified, what the cut deletes, and what its rollback costs. A
component's image is the legacy one, pinned in `Pulumi.yaml`'s
`versions:` block (§10, slice 2), and **the components declare their
workloads directly rather than through the legacy charts**: the immich
chart and the Bitnami Redis chart carry no security context that meets
`restricted`, and the Bitnami images have moved to a legacy namespace
of their own.

### 5.1 Before Wave A

*   M1 and M2 are done, and migration.md §1's verification gate has
    passed.
*   The shapes of §4 and the build slices of the waves' applications
    have merged (§10).
*   The NAS exports name every dataset of `conventions.nas.DATASETS`, at
    the worker's address: an aconfmgr change on the host, landed before
    Wave B's land, which is the first to mount one.

### 5.2 Wave A1: exim and authelia

**exim moves to the head of Wave A.** Authelia sends its notifications
— password resets and second-factor enrollment — through it, as immich
and Grafana do later, so a relay that moves in Wave B leaves the
authentication service without mail for a wave. Its placement is free
(workloads.md §5); it runs on the cloud pool beside its first sender.

*   **exim** declares its relay as workloads.md §5 does: the `dkim-exim`
    value mounted where exim reads `tls.key`, no `Certificate`, the
    `k8s` selector, its SMTP credential ported (§4.6) in `mail-system`.
    Its relay range is the cluster's pod ranges. No data. **Verified**
    by a message relayed from a pod and its DKIM signature checked at
    the receiver, and by sources-of-truth.md §X's digest against the new
    cluster's Secret, before the legacy relay stops. **Rollback** loses
    nothing; the relay holds no state.
*   **authelia** declares its server with its configuration rendered
    from the component, its session store, its users file and its
    database on one backed claim (`PRECIOUS`), its OpenID Connect
    clients from the census (§4.4), and the `ext_authz` endpoint the
    gateway filter calls. Its users and second factors are backed up
    nowhere today; the backed claim is what changes that. Its session
    store is not migrated, and every session signs in again.
    **Data**: the legacy claim's users file and SQLite database, copied
    into the new claim (§5.3's technique). **Verified** by a login with
    a second factor at the balancer, an unauthenticated request to a
    protected route refused, the canary's rule green, and a request
    with the gateway's filter target unreachable refused. **Cut**:
    `auth`. **Rollback** loses the enrollments made since the cut.

The canary of cluster-infra.md §2 ships with authelia: a `CronJob` that
requests a protected route with no credential every few minutes and
publishes the result, and a rule that fires on anything but a refusal,
with its tier and playbook.

### 5.3 Wave A2: blog, splitpro, matrix, syncthing, stdiscosrv

**Data technique for a legacy `local-path` claim**, here and in every
later wave: the legacy directory is streamed with `tar` through the
new cluster's API server into a pod that mounts the target claim
(`kubectl exec -i … tar -x`). migration.md §3's "over SSH" has no
second end here — a Talos node runs no SSH server — and the stream
needs no route between the sites beyond the one the operator already
has.

*   **blog**: the `StaticSite` with its git source (§4.5). No data; the
    built branch is the content. **Verified** at the balancer for every
    hostname, including both delegation files with their header.
    **Cut**: the web origin's served apex, and `www` in both website
    zones.
*   **splitpro**: its server on the cloud pool, its uploads on a backed
    claim, its database by recovery from the legacy store (§4.3), its
    client from the census. It sends no mail. **Data**: the uploads by
    stream; the database by recovery, once the legacy server is stopped
    and a final backup has run. **Verified** by a sign-in through the
    single sign-on flow and a recent expense read back. **Cut**:
    `split`. **Rollback** loses the expenses entered since the cut.
*   **matrix**: continuwuity on the cloud pool, its database and media
    on a backed claim, its client from the census. **Data**: the
    20 GiB claim by stream, after the server is stopped. **Verified** by
    a client sign-in and by a federation check of the server name, which
    reads the delegation the blog serves. **Cut**: `matrix` and its
    identity SRV, published direct with their reasons (§4.1).
    **Rollback** loses the events since the cut, which federation
    partners retry.
*   **syncthing**, the cloud replica: the shared component, in its cloud
    form — a node-volume claim over `syncthing-replica` (`backup=None`,
    reason on record: the NAS replica and every client hold the data),
    the device key ported, so the device keeps its identity, the index
    on a backed claim, and the WebDAV server beside it on the same
    volume, under the `dav` route with the gateway's basic-auth
    policy. Its peer port is a `public_port` on both transports, where
    the legacy Service exposed TCP alone. **Data**: none
    (migration.md §3); the replica reseeds from its peers.
    **Verified** by the peers connecting under its device identity and
    the reseed completing. **Cut**: `sync`, `dav`, and `btsync` in the
    website co-host zone as a `PORT` row. **Rollback** restarts the
    legacy replica on its JuiceFS volume, which the stop kept.
*   **stdiscosrv**: a `public_port` on its census row, terminating its
    own TLS with a certificate the cluster's issuer gives it for
    `syncapi`. **Data**: none; its announcements are rebuilt within
    minutes. **Verified** by a client's announcement and lookup against
    the new address. **Cut**: `syncapi` as a `PORT` row.

Legacy splitpro and matrix run on the homelab host, so Wave A frees
homelab memory without asking any of it (§6).

### 5.4 hath, after Wave A2 has soaked

hath moves as soon as Wave A2 has soaked, ahead of Wave B, as the
operator ruled (§11.1). It is cloud-side and depends on nothing that
Waves B to D move; the address it needs is exercised by nothing but
hath, so no wave before it proves it; and the VPS, which hath's egress
leaves through today, then retires as soon as its last legacy ingress
name is cut, in Wave C.

**hath leaves by the reserved address on its node's primary private
address**, rfc-007 §15.3 (a), in place of the secondary private address
`physical` declares today. Every pod on that node then leaves through
the reserved address by ordinary masquerading, and hath's Service
claims the node's own address, already an `internet` pool member; no
Cilium feature is involved. The choice is reversible — the reserved
address moves to another private address at any time — and the
alternative, the Egress Gateway, brings BPF masquerading and the
legacy-routing switch to every node for one workload, which nothing
else supports. Its costs are rfc-007's: hath is held to the node its
volume already holds it to, that node's public address is the reserved
one, and the node's other pods share hath's egress address.

**The `physical` change comes first**, in a slice of its own: the
reserved address moves onto the node's primary private address, and
the secondary address, its pool member and the outputs that name it
go. A private address carries one public address, so the node's
ephemeral one is released for it; whether the pinned provider releases
it in place, or only by replacing the instance, is that slice's first
reading. A control-plane node's replacement is a different design, and
the slice stops and reports if that is what it would take.

*   **hath**: its client on the node the volume follows, its cache on a
    node-volume claim over `hath-cache` (`backup=None`, preserved,
    `protect=True`), its client state on a backed claim, its login
    ported, strict CPU limits, and its port a `public_port` on the
    `hath` row. The legacy egress pods and their WireGuard keys do not
    port. **Data**: the legacy cache is on the NAS, larger than the
    volume, so the client's configured cache size is read first and set
    below the volume's free space before the copy; the cache is copied
    warm, ahead of the window, by a pair of throwaway pods — a reader on
    the worker over a temporary NAS dataset row, a writer on the node
    over the claim — and the final delta and the client state inside
    the window. **Verified** by the client starting with no cache
    rebuild, the network's test of its address passing, and its
    outbound connections arriving from the reserved address. **Cut**:
    none in DNS — hath is reached by its address — but the
    re-registration of the new address with the network, and the
    temporary dataset row's removal. **Rollback** within the window is
    the legacy client restarted on its NAS cache; after
    re-registration, a second re-registration back.

### 5.5 Wave B

Every application of Wave B runs on the worker. Its window grows the
worker only if the worker's allocatable cannot hold the wave's requests
on the reading of §6.3; the wave's applications are small, and the
expectation is that it does not.

*   **golinks**: its store on a backed claim, a LAN-only row with
    `auth=True`. Its bare name `go` is typed rather than followed, so it
    keeps resolving on the LAN: the row asks for the bare name's
    rewrite. **Cut**: the `go` vhost row.
*   **emailproxy**: its token cache on a backed claim, its configuration
    ported (§4.6), its IMAP port a LAN port with a host of its own,
    which Home Assistant's mail integration is pointed at in the same
    window. **Cut**: none in DNS — the name is new.
*   **spoolman**: its database on a backed claim, the `spool` row with
    `auth=True`, and its API a LAN port without the gate, for the
    printers, which are pointed at it in the window. **Data**: the legacy
    claim is on the shared JuiceFS volume, which migration.md §3 does
    not copy; its single file is streamed from the legacy pod's mount.
    **Cut**: `spool`, and its vhost row.
*   **The Home Assistant backend**: a relay on the worker, behind the
    `haos` row on `internet-gw`. workloads.md §4 designs a selectorless
    Service with an `EndpointSlice` at Home Assistant's LAN address, but
    a request through `internet-gw` reaches a cloud node's Envoy, which
    connects to the endpoint itself, and a cloud node has no route to a
    home VLAN (physical/homelab-host.md §5). So the Service selects a
    small reverse proxy pinned to the worker, which forwards to Home
    Assistant's address over the cluster zone's allow toward the home
    networks (physical/gateway.md §4.2) and passes the client's address
    on in `X-Forwarded-For`. Home Assistant's trusted proxies gain the
    worker's address in the same window. **Verified** by a sign-in at
    the balancer showing the client's address in Home Assistant's log.
    **Cut**: `haos`.
*   **The doors**: the `StaticSite`'s NAS instance, its content copied
    once from the VPS's directory onto its dataset. Its rows name the
    website co-host zone and `jiahui.love`, so the land brings
    certificates for zones the cluster has not served, and the
    cluster's DNS-01 token is minted afresh, scoped to them, into the
    land's branch. **Cut**: `games` and `game`, and `jiahui.love`'s
    apex in the web origin.
*   **thread-dashboard**: its image built in `docker/` (framework/ci.md
    §4: a cluster-consumed image is built where its consumer lives), a
    LAN-only row with `auth=True`. **Cut**: the `thread` vhost row, and
    its quadlet removed from yadm (sources-of-truth.md, row R13).
*   **Monitoring's name.** `mon` becomes a row of the monitoring
    component (rfc-007 §7.4): Grafana behind it with its single
    sign-on client, and `k8s-base` publishing its record. A stack that
    calls `route` for a public row builds a Cloudflare provider (rfc-007
    §9.1), so `k8s-base` reads the zones token: the token's slot gains
    `k8s-base` as a target. `dns`, `k8s-base` and `apps` share one
    passphrase, so no reader gains the token that does not hold it
    already (credentials.md §1). The alert read route rides `mon`,
    matched on method, path and header, and lands with the poller that
    reads it. **Cut**: `mon`.
*   **The legacy Prometheus stops** with the rest of the legacy
    monitoring, and its claim is kept cold until its retention would
    have aged out, rather than running read-only: running it would hold
    memory the interleave needs, for a history no rule reads.

The same cut takes the gateway's resolver and console names out of
`LEGACY_VHOSTS`, whose rows retire at Wave B: their public names have
had rewrites since `dns`'s first up.

### 5.6 Wave C

**The window** grows the worker and binds the A380 in one drained
apply, since each replaces the domain (migration.md, Wave C). Just
before it, kluster-code moves the legacy GPU plugin to the iGPU, and the
window's stop releases the A380 from legacy immich and jellyfin.

*   **immich**: its server and its machine-learning service, each
    requesting the A380 through what `k8s-base` offers when the wave
    opens; its library a NAS dataset; its thumbnails and model caches on
    `local-path` with `backup=None` (re-derivable); its queue store a
    Redis of its own; its database by recovery (§4.3) on the
    `vchord` image, with its extensions created on recovery as
    legacy creates them; its configuration rendered as legacy renders
    it, its client from the census, its mail through exim; its
    queue watchdog ported. Its row is `SPLIT`, published direct for
    uploads larger than the proxy accepts. **Data**: the library stays
    in place; the database by recovery after a final backup.
    **Verified** by an upload from a phone on each side of the split,
    a search answered by the model, and a video transcoded on the A380.
    **Cut**: `photos`, and its LAN rewrite rule.
    **Rollback** loses the uploads since the cut, which stay in the
    library on the NAS but outside the restored database.
*   **jellyfin** and **shoko**: each in a namespace of its own, with
    its own claim on the media dataset; jellyfin's configuration and cache on
    claims, the configuration backed; jellyfin's row `SPLIT_IOT`
    (§4.1). Discovery by multicast does not cross into a routed pool
    address, so the televisions reach jellyfin by its name; the host's
    discovery relays are pointed at the media VIP in the window.
    **Data**: each configuration claim by stream, with any render node
    path it names checked against the worker's one GPU. **Verified** by
    a television playing a title through the media VIP, and a
    transcode on the A380. **Cut**: `tube`, and its vhost row.
*   **syncthing-nas**: the shared component in its NAS form — the `Sync`
    dataset, the index on a backed claim, the peer port a LAN port.
    Its device identity lives in its configuration, which is copied
    whole, so its peers see the same device. **Cut**: `sync-nas`.

The VPS's last legacy ingress name is cut here. Its cancellation is the
decommission's (§5.8).

### 5.7 Wave D

*   **qbittorrent** with **seedwatch**, in one namespace on the worker
    (workloads.md §4): its downloads and the media links on the media
    dataset, its profile on a backed claim, its peer port's forward
    declared by `physical` (`qbittorrentOnWorker`, migration.md, Wave
    D), the Web UI `SPLIT` with `auth=True` on `bt`; seedwatch's image
    built in `docker/`, and its access to qbittorrent's API by the
    namespace's own address range rather than by running on the same host.
    **Data**: the profile copied with UPnP turned off. **Verified** by
    peers connecting on both families, and seedwatch's links counted as
    before. **Cut**: `bt`, the `bt` and `seedwatch` vhost rows, and the
    host units removed from aconfmgr and yadm (row R13). **Rollback**
    restarts the host units on the profile the stop kept.

### 5.8 What the decommission inherits

When Wave D's cut has soaked, every application serves from the new
cluster, and what the waves leave is the decommission's:

*   the legacy cluster stopped and uninstalled, its retained-volume
    census finished;
*   the VPS cancelled: nothing has addressed it since Wave C's cut, and
    hath has left its egress since §5.4;
*   `legacy.py` empty, and with it the legacy-held field of §3.3 and its
    relation test deleted;
*   the parked zones' web origin, the VPS's anchor and its overlay
    roster entry removed;
*   the legacy Google Cloud Storage buckets and their ported
    credentials, read by nothing since the cuts;
*   the sealing key's rotation, which re-seals every ported ciphertext
    and deletes the legacy key (§4.6);
*   the worker's final size (§6.2), and with it the host memory left for
    what the operator runs beside the cluster.

--------------------------------------------------------------------------------

## 6. Memory

### 6.1 The budget, and the per-app sizing

The host has 31 GiB. On 2026-11-05 legacy k3s took about 7.5 GiB, Home
Assistant's machine 4.5, the host's own services about 3, and the ZFS
cache 4, its floor (nodes.md §4.1). The worker boots at 10 GiB. That
leaves the worker about what legacy releases: roughly 17 to 18 GiB once
legacy has gone, short of nodes.md §4.2's 20 GiB, which was written
against the August measurement.

**The applications fit under that ceiling.** Their requests, taken from
the legacy components where those carry them and sized from evidence
otherwise:

| What runs on the worker | Requests (memory) |
| --- | --- |
| Platform: kubelet, the container runtime, Cilium and its Envoy | about 1.5 GiB, reserved (nodes.md §4.2) |
| Monitoring, from M2 | 0.7 GiB |
| Wave B's applications | about 0.3 GiB together |
| immich: server 4 GiB, machine learning 1 GiB, one database instance 2 GiB, its Redis 0.1 GiB | 7.1 GiB |
| jellyfin and shoko | 1 GiB |
| syncthing-nas | 0.1 GiB |
| qbittorrent and seedwatch | about 1 GiB |
| **Total** | **about 11.7 GiB** |

That leaves headroom for the limits above the requests, which are
large for immich's server and machine-learning service and for
jellyfin's transcodes, inside a worker grown to about 17 GiB, whose
allocatable is about 16. **So the per-app
sizing does not trigger the conditional ruling of §11.2**: the
migration fits the ceiling without the relief valve.

### 6.2 Growing to fit, in windows

**The worker grows to fit, not to absorb.** A window grows the worker by
what the next wave's requests and their headroom need, capped by what
legacy has released; whatever legacy releases beyond that stays with
the host. That keeps the end-state size a decision of the
decommission's rather than a side effect of the waves, and leaves the
host room for what it runs beside the cluster. **New rule**, landing in
migration.md §0 rule 4 and nodes.md §4.2.

A growth is a commit to `HOMELAB_MEMORY_GIB` and an apply of `physical`
that replaces the domain, so it is a drained window and a pull request
of its own. The windows:

*   **Wave A** needs none: its applications run on the cloud pool, and
    stopping their legacy counterparts frees homelab memory.
*   **Wave B** needs one only if the worker's allocatable does not hold
    its requests (§5.5).
*   **Wave C's window** grows the worker for Wave C and, where the host
    has already released enough, for Wave D too, so that Wave D needs no
    window of its own. It is also the A380's bind, which replaces the
    domain anyway.
*   **Wave D** has a window only where Wave C's could not fund it.

### 6.3 What is verified before a window grows the worker

Each item is read and recorded on the wave's issue before the growth
commit merges:

1.  every application of the previous wave has been cut and has soaked;
2.  each of their legacy workloads has been removed in kluster-code,
    not merely stopped (§3.5), so nothing can start it again;
3.  the host's available memory, read with the ZFS cache at its floor,
    covers the growth with a margin;
4.  the worker's requests after the next wave's land, read against the
    allocatable the growth gives, leave the headroom §6.1 assumes.

### 6.4 Rollback across a growth

A growth spends memory a legacy application needs to start again. **So
a rollback after a growth is the window reversed**: drain the worker,
shrink the domain by the commit's revert, start the legacy application,
then run §3.6's rollback. It costs a drained window, which restarts
every workload on the worker. Wave C is where this is real: its window
stops legacy immich and jellyfin and grows by what they free, and a
rollback restarts them on the iGPU, which kluster-code moved them to
before the window, once the worker has given the memory back. **New
rule**, landing in migration.md §0.

Every other growth is funded by applications already past their
rollback point (§6.3, items 1 and 2), so it never meets this case.

--------------------------------------------------------------------------------

## 7. What is already conformant

*   **The route census** carries `Exposure`, the zone set and the extras
    the rows need, and `dns` already derives a LAN-side row's rewrites at
    the VIP its exposure selects; §3.3 and §4.1 add to it rather than
    reshape it.
*   **`conventions.backup`** has the retention classes, the bucket and
    the repository layout every backed claim and database uses.
*   **`conventions.NODE_VOLUMES`** declares both preserved volumes, and
    `physical` attaches them.
*   **The public port census** has every row the migration's raw ports
    need: discovery, syncthing on both transports, and hath.
*   **`conventions.sealed.DKIM_EXIM`** and the `credentials` command
    that records it carry the mail relay's key across.
*   **`conventions.gateway.LEGACY_VHOSTS`** already gives each LAN-zone
    name the wave that deletes it.
*   **The self-built images** for the database operands, emailproxy and
    golinks are built multi-architecture by `images.yml`.

--------------------------------------------------------------------------------

## 8. What does not propagate

*   **From dns.md §6, a migration as three files in one pull request.**
    Rule 1's verification needs a merge between the deploy and the cut
    (§3.2).
*   **From migration.md §0 rule 1, scaling the legacy application
    down by hand.** kluster-code's deploy would undo it (§3.5).
*   **From migration.md §0 rule 4, growing the worker by everything
    legacy frees, toward 20 GiB.** It grows to fit (§6.2), and nodes.md
    §4.2's 20 GiB is not reachable on the measured host (§6.1).
*   **From migration.md §2, exim in Wave B.** Its first sender moves in
    Wave A1 (§5.2).
*   **From migration.md §2, hath last of the applications, in a Wave E
    of its own.** It moves after Wave A2 (§5.4).
*   **From migration.md §2, the legacy Prometheus kept running
    read-only.** It is kept cold (§5.5).
*   **From migration.md §3, rsync over SSH into the target claim.** A
    Talos node has no SSH server; the stream runs through the API server
    (§5.3).
*   **From migration.md §3, no copy for any shared-JuiceFS data.** True
    of the syncthing replica, false of spoolman's database (§5.5).
*   **From the legacy immich database, two instances.** One (§4.3).
*   **From the legacy charts, the immich and Redis releases.** The
    components declare their workloads (§5).
*   **From the legacy egress pods, hath's WireGuard gateway.** hath
    leaves by its node's address (§5.4).
*   **From workloads.md §4, the LAN-device backend as a selectorless
    Service.** A cloud node's Envoy cannot reach the endpoint; a relay on
    the worker does (§5.5).
*   **From the legacy LAN load balancers, multicast discovery.** A
    routed pool address does not receive it; clients reach the
    applications by name (§5.6).
*   **From credentials.md §3, the restic password generated in state.**
    It is generated by `credentials`, escrowed and sealed (§4.6).
*   **From rfc-007 §6.3, the legacy key's channel left open.** It is a
    one-off carry with a register row, retired at the decommission
    (§4.6).
*   **From architecture.md §3.2, the dedicated VIP on a secondary
    private address.** It is the node's primary private address (§5.4).

--------------------------------------------------------------------------------

## 9. The documents this content lands in

| Document | What lands there |
| --- | --- |
| [cluster/migration.md](../cluster/migration.md) | migration.md §0 rule 1: build, land, verify, cut, and the stop and removal in kluster-code, from §3.1, §3.2 and §3.5. migration.md §0 rule 4: growing to fit, the windows, the reading before each growth and the rollback across one, from §6. migration.md §1: the legacy key's carry at Wave A1's head, from §4.6. migration.md §2: exim at Wave A1's head, hath after Wave A2, Wave B's window conditional, Wave D funded by Wave C's window, the legacy Prometheus kept cold, from §5. migration.md §3: the stream through the API server, recovery from the legacy store, spoolman's copy, from §4.3 and §5. migration.md §4: what the decommission inherits, from §5.8 |
| [declarative/workloads.md](../declarative/workloads.md) | workloads.md §1: a component receives its rows and merges unwired, the LAN port, from §3.1 and §4.1. workloads.md §3: the namespace's backup value, from §4.6. workloads.md §4: the dedicated-VIP pattern on the node's primary address, one database instance with recovery and the drill, the static site's apex, the single sign-on census, the LAN-device backend as a relay on the worker, from §4, §5.4 and §5.5. workloads.md §5: a ported ciphertext keeps its namespace, from §4.6 |
| [declarative/dns.md](../declarative/dns.md) | dns.md §2: an apex row's record is the web origin, from §4.1 and §4.5. dns.md §5: the legacy-held state, the `unproxied` reason on a row, `SPLIT_IOT`, `PORT`, LAN ports' rewrites, from §3.3 and §4.1. dns.md §6: the cut's pull request and the land's, from §3.2 |
| [framework/ci.md](../framework/ci.md) | ci.md §3: `up-apps` after `up-dns`, from §3.4 |
| [sources-of-truth.md](../sources-of-truth.md) | sources-of-truth.md §3: a component that has not migrated is not wired, from §3.1. Note N4 replaced by its answer, from §3.2 and §3.4. Rows R12 and R13: each flip is a cut, and its pull request edits the row |
| [cluster/architecture.md](../cluster/architecture.md) | architecture.md §3.2 and §3.5: hath's address and egress on its node's primary private address, from §5.4. architecture.md §3.6: the LAN raw-port row on the shared LAN VIP, from §4.1 |
| [declarative/physical.md](../declarative/physical.md) | physical.md §1: the reserved address on the node's primary private address, and no secondary address, from §5.4 |
| [declarative/cluster-infra.md](../declarative/cluster-infra.md) | cluster-infra.md §1.1: the backup value's channel, from §4.6. cluster-infra.md §2: the canary as built with authelia, from §5.2. cluster-infra.md §3: the `mon` route and the Cloudflare provider of `k8s-base`, from §5.5 |
| [credentials.md](../credentials.md) | credentials.md §3: the B2 writer keys and restic password rows, the zones token's `k8s-base` target, the legacy sealing key's row, from §4.6 and §5.5. credentials.md §4: the backup value's mint |
| [cluster/storage.md](../cluster/storage.md) | storage.md §2: the NAS dataset census, from §4.2. storage.md §5: one database instance, and the restore drill in place, from §4.3 |
| [cluster/nodes.md](../cluster/nodes.md) | nodes.md §4.2: the end state as the decommission's decision, grown to fit, from §6 |
| [operations.md](../operations.md) | operations.md §4: the database restore drill and the authentication canary built, from §4.3 and §5.2 |
| [physical/homelab-host.md](../physical/homelab-host.md) | homelab-host.md §4: the export list follows the NAS dataset census, from §4.2 |
| [rfc-003](rfc-003-dns-and-github-stacks.md) | The status header: the route row's change of shape, from §4.1 |
| [rfc-007](rfc-007-k8s-base-and-apps.md) | The status header: slice 12 cut in two, from §10. That dated line ships in the pull request that builds the first half, not in this document's |

--------------------------------------------------------------------------------

## 10. How we get there

**Pre-live** means built and tested against Pulumi's mocks and fakes,
with nothing live; such a slice merges as soon as it is ready, and
changes nothing deployed (§3.1). **Live** means the slice needs the
running cluster, a sealed value, a window or an operator.

Each slice's **After** line names the slices it depends on and every
slice it shares an owned path with, so two slices whose lines do not
reach each other can run at once. The shape slices 3 to 7 form one
line, because each adds a census and its invariants
(`src/kluster/conventions/__init__.py`, `tests/test_conventions.py`).
The build slices, 10 to 23, own only their own package and test
module, and run together once their shapes have merged.
`docs/framework/ci.md` is serialized, and slice 1 is the one slice here
that touches it.

**Before the shapes and the builds: rfc-007 slice 12, cut in two.** Its first half —
the application base and `route`, with their tests and the documents
its done-means names for them — merges now and declares nothing, since
the `apps` program calls neither. Its second half — the echo
component, its row, its image pin and its wiring — merges after rfc-007
slice 9 is applied, as written. Every shape and build here extends the
first half. The pull request that builds the first half records the cut
in rfc-007's status header with a dated `Updated:` line (rfc.md §5.2).

### 10.1 Pre-live: the mechanisms and the shapes

**Slice 1: the deploy order.** Pre-live. **After** nothing.

*   **Done means**: `up-apps` needs `up-dns` and `up-k8s-base` and runs
    when `up-k8s-base` succeeded, whatever `up-dns`'s result; ci.md §3
    says so, with the reason (§3.4).
*   **Owned paths**: `.github/workflows/deploy.yml`,
    `docs/framework/ci.md` (serialized), `tests/test_workflow_shape.py`.
*   **Tests that fail without it**: `up-apps` names `up-dns` among its
    needs (drop it); its condition reads `up-k8s-base`'s result and not
    `up-dns`'s (require both to succeed).

**Slice 2: the image pins.** Pre-live. **After** nothing.

*   **Done means**: every image the waves' components run is a
    `versions:image-<name>` pin in `Pulumi.yaml`, at the legacy
    version, and Renovate opens a pull request for each one's next
    release under `renovate.json5`'s rules.
*   **Owned paths**: `Pulumi.yaml`, `renovate.json5`,
    `tests/test_versions.py`, `tests/test_images.py`.
*   **Tests that fail without it**: each new pin parses (write one
    that does not parse); the existing renovate fixture tests, with a
    pin of each new image's scheme (drop the rule a scheme needs).

**Slice 3: the route census for the migration.** Pre-live. **After**
rfc-007 slice 12's first half.

*   **Done means**: `Route` carries `unproxied`, with `proxied` derived;
    the legacy-held field; `Exposure.SPLIT_IOT` and `Exposure.PORT`;
    `route` attaches a held row's route and publishes nothing for it,
    publishes no record for an apex row, and for a `PORT` row publishes
    the record and attaches no route; `served_zones` skips `PORT` rows; `dns` writes no rewrite for a held or `PORT` row, and
    the media VIP for `SPLIT_IOT`; dns.md §2, §5 and §6, sources-of-truth.md
    §3 and note N4, and rfc-003's status header are true.
*   **Owned paths**: `src/kluster/conventions/routes.py`,
    `src/kluster/lib/k8s.py`, `src/kluster/components/dns/rewrites.py`,
    `tests/test_conventions.py`, `tests/test_dns_rewrites.py`,
    `tests/test_dns_records.py`, `tests/test_k8s.py`,
    `docs/declarative/dns.md`, `docs/sources-of-truth.md`,
    `docs/rfc/rfc-003-dns-and-github-stacks.md`.
*   **Tests that fail without it**: a row is held exactly while a record
    `dns` points at the VPS holds its name, against a census broken each
    way (keep the field after deleting the block); a direct row states its reason, and
    resolves through no proxied record (a direct row aliasing a proxied
    one); no row names a co-host label as a primary application; a held
    row's route attaches with no record and no rewrite (publish one); an
    apex row publishes no record (publish a CNAME at the apex); a `PORT`
    row costs no certificate (count its zone); `SPLIT_IOT`'s rewrite is
    the media VIP (answer the LAN one).

**Slice 4: exposure.** Pre-live. **After** slice 3.

*   **Done means**: `public_port` on the application base; the LAN port
    census in `conventions.cluster` and the LAN port shape on the base;
    `dns` writes a LAN port row's rewrite; `route(row, auth=True)`
    attaches the ExternalAuth filter; workloads.md §1, dns.md §5 and
    architecture.md §3.6 are true.
*   **Owned paths**: `src/kluster/components/apps/__init__.py`,
    `src/kluster/lib/k8s.py`, `src/kluster/conventions/cluster.py`,
    `src/kluster/conventions/__init__.py`,
    `src/kluster/components/dns/rewrites.py`, `tests/test_apps.py`,
    `tests/test_conventions.py`, `tests/test_dns_rewrites.py`,
    `tests/test_k8s.py`, `docs/declarative/workloads.md`,
    `docs/declarative/dns.md`, `docs/cluster/architecture.md`.
*   **Tests that fail without it**: `public_port` emits the Service
    alone, under `Cluster`, with no node ports and the row's front's
    addresses (state `Local`; allocate node ports; request the VIP for
    a balancer row); a LAN port shares the default VIP with `lan-gw`
    under one sharing key (request a fresh address); no two LAN port
    rows, and no Gateway listener, take one port on one VIP (a census
    invariant); `auth=True` attaches the filter and the default does
    not (attach it everywhere).

**Slice 5: storage.** Pre-live. **After** slice 4.

*   **Done means**: `backed_pvc` and the node-volume claim on the
    application base, and the NAS volume shape over the new
    `conventions.nas.DATASETS`; workloads.md §2 and §3, storage.md §2 and
    homelab-host.md §4 are true.
*   **Owned paths**: `src/kluster/components/apps/storage.py` (new),
    `src/kluster/conventions/nas.py` (new),
    `src/kluster/conventions/backup.py`,
    `src/kluster/conventions/__init__.py`, `tests/test_apps_storage.py`
    (new), `tests/test_conventions.py`,
    `docs/declarative/workloads.md`, `docs/cluster/storage.md`,
    `docs/physical/homelab-host.md`.
*   **Tests that fail without it**: a backed claim's source schedules
    by its class and writes to `volsync_repo_path` of its namespace and
    name (hard-code a cron line; name another path); its repository
    Secret reuses the namespace's backup value under its own name (seal
    a value per claim); a NAS volume mounts its row's export and is
    scheduled only to the worker (drop the affinity); a node-volume
    claim's path is inside the mount and never the mount point (point at
    the mount).

**Slice 6: backup credentials and the ported ciphertexts.** Pre-live;
the mints are live, at each wave's head. **After** slice 5.

*   **Done means**: `conventions.backup` lists the backed namespaces and
    `conventions.sealed` builds one backup value per namespace from it;
    `credentials` mints each namespace's writer keys, generates and
    escrows its restic password, and seals the value namespace-wide into
    `apps`'s configuration; every legacy ciphertext the waves' components
    declare is copied into `Pulumi.apps.yaml`; credentials.md §1 rule 6's
    channel and §3's rows, cluster-infra.md §1.1, and the legacy key's
    row are true.
*   **Owned paths**: `src/kluster/conventions/sealed.py`,
    `src/kluster/conventions/backup.py`,
    `src/kluster/scripts/credentials/`, `Pulumi.apps.yaml`,
    `tests/test_sealing.py`, `tests/test_slots.py`, `tests/test_b2.py`,
    `tests/test_conventions.py`, `docs/credentials.md`,
    `docs/declarative/cluster-infra.md`.
*   **Tests that fail without it**: a writer key is scoped to its
    namespace's prefix and lacks `deleteFiles`, against the B2 fake
    (scope it to the bucket; grant delete); the password is escrowed
    before it is sealed, and a mint that fails between them leaves
    neither (seal first); the value opens under any name in its
    namespace and in no other namespace (seal strict); every backed
    namespace has a row (drop one).
*   **Unproven live**: each mint, against the running controller and
    the real B2 account.

**Slice 7: single sign-on.** Pre-live. **After** slices 4 and 6.

*   **Done means**: `conventions.sso.CLIENTS`, each redirect address
    built from its client's route row; workloads.md §4 is true.
*   **Owned paths**: `src/kluster/conventions/sso.py` (new),
    `src/kluster/conventions/__init__.py`, `tests/test_conventions.py`,
    `docs/declarative/workloads.md`.
*   **Tests that fail without it**: every redirect address is a name a
    route row publishes, in a zone the row names (a client redirecting
    to an unrouted name); client identifiers are unique (a census
    invariant).

**Slice 8: databases.** Pre-live. **After** slices 5 and 6.

*   **Done means**: the database shape of §4.3 on the application base:
    one instance, the barman plugin to the backup bucket under the
    namespace's backup value, scheduled by its class, an optional
    recovery from a named object store with its credential, and the
    restore drill with its rule; storage.md §5 and operations.md §4 are
    true.
*   **Owned paths**: `src/kluster/components/apps/database.py` (new),
    `tests/test_apps_database.py` (new), `docs/cluster/storage.md`,
    `docs/operations.md`.
*   **Tests that fail without it**: the cluster runs one instance (run
    two); it archives to `barman_repo_path` of its namespace and name
    (name another); a recovering cluster bootstraps from the store it
    is handed and archives to the bucket (archive back to the legacy
    store); the drill's role reaches only its namespace, and its rule
    carries a tier and a playbook that resolves (grant it cluster-wide;
    name a missing section).

**Slice 9: static sites.** Pre-live. **After** slice 5.

*   **Done means**: `StaticSite` with both sources, the delegation
    files' header on the apex route; workloads.md §4 is true as it
    stands.
*   **Owned paths**: `src/kluster/components/apps/static_site.py`
    (new), `tests/test_static_site.py` (new).
*   **Tests that fail without it**: a git-source site syncs its branch
    into an empty directory and runs two replicas across the cloud pool
    (one replica; no spread); a NAS-source site mounts read-only and
    runs on the worker (mount read-write); the header is set on the
    delegation paths alone (set it site-wide).

### 10.2 Pre-live: the applications

Each build slice owns `src/kluster/components/apps/<application>/` and
`tests/test_app_<application>.py`, both new, and nothing else unless
its line says so; **done means** the component declares what §5 says
its application declares, the gate passes, and the component is not
wired. Its tests hold the relations §5 names: the claims backed or
exempted with their reason, the rows it receives attached to what their
exposure says, its ported values read from the configuration, its pod
security `restricted` unless its line states otherwise. Each pull
request carries an unproven-live note naming its wave's verification.

| Slice | Application | Wave | Size | After |
| --- | --- | --- | --- | --- |
| 10 | exim | A1 | S | 2, 4, 6 |
| 11 | authelia, with the canary | A1 | M | 2, 5, 6, 7 |
| 12 | blog | A2 | S | 2, 9 |
| 13 | splitpro | A2 | M | 2, 7, 8 |
| 14 | matrix | A2 | S | 2, 5, 7 |
| 15 | syncthing, both forms, and the WebDAV server | A2, C | M | 2, 4, 5 |
| 16 | stdiscosrv | A2 | S | 2, 4 |
| 17 | hath | after A2 | M | 2, 4, 5 |
| 18 | golinks, emailproxy and spoolman | B | M | 2, 4, 5, 6 |
| 19 | the Home Assistant relay, and the doors | B | S | 2, 4, 9 |
| 20 | thread-dashboard, and its image in `docker/` | B | S | 2, 4 |
| 21 | immich | C | L | 2, 4, 5, 7, 8 |
| 22 | jellyfin and shoko | C | M | 2, 4, 5, 7 |
| 23 | qbittorrent and seedwatch, and seedwatch's image in `docker/` | D | M | 2, 4, 5 |

**Slice 24: monitoring's name.** Pre-live. **After** slices 3, 6 and 7,
and rfc-007 slice 11.

*   **Done means**: the monitoring component takes the `mon` row and
    Grafana's client from the census; `k8s-base` builds its Cloudflare
    provider from the zones token, whose slot names it as a target; its
    two Grafana values sealed afresh into its configuration;
    cluster-infra.md §3 and credentials.md §3 are true.
*   **Owned paths**: `src/kluster/components/monitoring/`,
    `src/kluster/stacks/k8s_base.py`,
    `src/kluster/scripts/credentials/slots.py`,
    `tests/test_monitoring.py`, `tests/test_stack_programs.py`,
    `tests/test_slots.py`, `docs/declarative/cluster-infra.md`,
    `docs/credentials.md`.
*   **Tests that fail without it**: the component's route attaches the
    row it is handed (hard-code a host); `k8s-base` builds one
    Cloudflare provider from its own configuration (read it across a
    StackReference); the zones row targets every stack that builds one
    (drop `k8s-base`).

**Slice 25: hath's address in `physical`.** Pre-live to build; its apply
is live, behind `physical`'s reviewer, before slice 33. **After** the
bring-up.

*   **Done means**: the reserved address is assigned to the dedicated
    node's primary private address, the secondary address and its pool
    member are gone, and the outputs `k8s-base` reads for the pool no
    longer name it; architecture.md §3.2 and §3.5 and physical.md §1 are
    true.
*   **Owned paths**: `src/kluster/components/cloud/nodes.py`,
    `src/kluster/conventions/outputs.py`, `src/kluster/stacks/physical.py`,
    `src/kluster/components/cilium/`, `tests/test_cloud_nodes.py`,
    `tests/test_physical_stack.py`, `tests/test_cilium.py`,
    `docs/cluster/architecture.md`, `docs/declarative/physical.md`.
*   **Tests that fail without it**: the reserved address names the
    dedicated node's primary private address (name a secondary one); the
    pool holds no address the node does not carry (keep the secondary
    member).
*   **Unproven live**: whether the provider releases the node's
    ephemeral address in place (§5.4); the egress address hath's
    outbound connections arrive from.

### 10.3 Live: the windows

Each window is a sequence of slices, run in the order below; each
pull request is its own slice, and so is each step with none.

| Slice | Window | What | Owned paths |
| --- | --- | --- | --- |
| 26 | A1 | The operator's steps at the head: the legacy key carried (§4.6), authelia's namespace's backup value minted into a branch, kluster-code's stop of exim and authelia merged | none; kluster-code |
| 27 | A1 | **Land**: exim and authelia wired, `auth` held, the backup value | `src/kluster/stacks/apps.py`, `src/kluster/conventions/routes.py`, `Pulumi.apps.yaml` |
| 28 | A1 | **Cut**: `auth` | `src/kluster/conventions/routes.py`, `src/kluster/components/dns/legacy.py`, `docs/sources-of-truth.md` |
| 29 | A2 | The head: backup values minted, kluster-code's stop | none; kluster-code |
| 30 | A2 | **Land**: blog, splitpro, matrix, syncthing, stdiscosrv | as slice 27 |
| 31 | A2 | **Cut**: `split`, `matrix`, `sync`, `dav`, `syncapi`, `btsync`, the web origin's served apex and both `www` | as slice 28, and `src/kluster/components/dns/base.py` |
| 32 | after A2 | The head: the client's cache size set, the warm copy, the backup value, kluster-code's stop | none; kluster-code |
| 33 | after A2 | **Land**: hath wired, with the temporary NAS dataset row its copy reads; the final delta | as slice 27, and `src/kluster/conventions/nas.py` |
| 34 | after A2 | **Cut**: the re-registration, and the temporary dataset row removed | `src/kluster/conventions/nas.py`, `docs/sources-of-truth.md` |
| 35 | B | The head: the NAS exports, backup values, the doors' content and DNS-01 token, the window if §6.3 asks for one, kluster-code's stop | `src/kluster/conventions/homelab.py` if the window runs; kluster-code; aconfmgr |
| 36 | B | **Land**: Wave B's applications, and slice 24's row | as slice 27, and `Pulumi.k8s-base.yaml` |
| 37 | B | **Cut**: `spool`, `haos`, `mon`, `games`, `game`, `jiahui.love`'s apex, and the B rows of `LEGACY_VHOSTS` | as slice 31, and `src/kluster/conventions/gateway.py` |
| 38 | C | The head: kluster-code's move to the iGPU and stop, backup values, then the window: growth and the A380's bind | `src/kluster/conventions/homelab.py`, `src/kluster/components/homelab/`, `tests/test_homelab.py`, `docs/physical/homelab-host.md` |
| 39 | C | **Land**: immich, jellyfin and shoko, syncthing-nas | as slice 27 |
| 40 | C | **Cut**: `photos`, `tube`, `sync-nas`, their rewrite rules, and the `tube` vhost row | as slice 37 |
| 41 | D | The head: the window if Wave C's did not fund it, and the host units stopped; kluster-code has nothing to stop | `src/kluster/conventions/homelab.py` if the window runs; aconfmgr; yadm |
| 42 | D | **Land**: qbittorrent and seedwatch, and `qbittorrentOnWorker` | as slice 27, and `Pulumi.physical.yaml` |
| 43 | D | **Cut**: `bt`, the `bt` and `seedwatch` vhost rows | as slice 37 |

A cut's owned paths also take the resolvers' declared rule list, where
it names a moved name (sources-of-truth.md, row R2).

**Each window's done means** is the slice's own verification of §5,
transcribed on its issue, and for each cut, its soak passed and
kluster-code's removal of the cut applications merged (§3.5, §6.3).
The removals are kluster-code pull requests the operator merges, each
named on the cut's issue.

Each land and cut also edits the rows of sources-of-truth.md its
applications' flips change, and the last cut's pull request flips this
document's status to **Implemented** and names where its content lives.

--------------------------------------------------------------------------------

## 11. Open questions

### 11.1 Ruled: hath after Wave A2

The operator ruled on 2026-11-05 that hath moves directly after Wave A2
has soaked (§5.4), rather than last of the applications. The VPS then
retires once its last legacy ingress name is cut, in Wave C, rather than
after hath's soak. What decided it: hath is cloud-side and depends on
nothing Waves B to D move, and the address it needs is one that only
hath exercises, so the waves between them would not have proven it.

### 11.2 Conditional: the worker's end-state size

The worker's end-state size, against what the host keeps for the
operator's own work, was to be ruled only if the per-app sizing did not
fit under the ceiling of §6.1. It fits (§6.1), so nothing is asked of
the operator here; the end-state size is the decommission's decision
(§5.8). The question returns only if a wave's reading of §6.3 departs
from §6.1's sizing by more than its headroom.

### 11.3 Settled on first contact

*   Each application's memory requests where the legacy component
    states none, and its limits, from the first week's readings.
*   The cache size hath's client is set to, from the client's own
    reading against the volume (§5.4).
*   Whether jellyfin's or immich's configuration names a render node
    path, read when its claim is copied (§5.6).
*   The rewrite rules the resolvers' declared list carries for names
    the waves move, read off the list when its own declaration lands.
*   The host label each LAN port row names.
