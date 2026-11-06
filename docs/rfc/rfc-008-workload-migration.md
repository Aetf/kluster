# RFC 008: The Workload Migration

*   **Status:** Proposed, 2026-11-05. The operator ruled on an outline of
    this design on 2026-11-05: hath moves directly after Wave A2 has
    soaked (§11.1), and the unit of migration, the mechanisms ahead of
    the shapes and the shapes ahead of the builds, the wave order and
    every decision the outline listed as settled stand.
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
    §6.2, §9), and for the decisions it left to the migration's design
    (rfc-007 §15.3). Their precedents are cited at each use rather than
    re-argued.
*   **In scope:**
    1.  the unit of migration: how an application's component is built
        before its wave, deployed beside the legacy name, verified, and
        given the name, and how each step is rolled back;
    2.  the order in which the deploy chain applies `dns`, `k8s-base`
        and `apps`;
    3.  what each application needs, asked from scratch, and the shapes
        that follow: the exposure declaration every consequence derives
        from, the claims, databases, single sign-on, static sites, and
        the secrets the migration carries;
    4.  per wave, each application's component, its data move, its
        cutover with its DNS, its verification and its rollback, and
        what of the legacy estate keeps running for the applications
        that have not moved;
    5.  the homelab worker's memory across the waves: when it grows, by
        how much, what is verified first, and how a rollback after a
        growth is run;
    6.  the decisions rfc-007 §15.3 left to the migration's design;
    7.  what the decommission inherits from the waves;
    8.  how long the live phase takes, and what sets it;
    9.  the slices.
*   **Out of scope:**
    *   **The decommission itself**: the legacy cluster's teardown, the
        VPS's cancellation, the trackers' retirement, the sealing key's
        rotation and the worker's final size. migration.md §4 lists
        them, and the milestone that retires the legacy estate opens with
        a design of its own. §5.8 says what it inherits.
    *   **Whether the state-backend appliance's pins move into the
        `versions:` block**, a decision of rfc-007 §15.3 that is not the
        migration's: it is that appliance's own change.
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
    *   **The bring-up.** [physical/bring-up.md](../physical/bring-up.md)
        orders it, and a runbook is where it belongs: it is a sequence
        that runs once, and nobody will need its argument later.
    *   **What stays on the host or the gateway**: AdGuard, HAOS,
        dmarc-check, the NAS role and the legacy state backend
        ([cluster/migration.md](../cluster/migration.md) §2, "not
        migrating").
    *   **The applications retired outright** (migration.md §2), which
        never land.
    *   **The upgrade of immich's `vchord` extension.** It is immich's
        own change, made once on the new cluster after every workload
        has moved.
    *   **Edits inside kluster-code**, the legacy repository. Each wave
        that stops legacy applications needs changes there (§3.5); they
        are that repository's pull requests, and the slices here name
        them.

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

Most of the migration was decided before this document. migration.md
orders the waves and names each storage kind's data move.
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
    chain runs those two stacks in parallel while Cloudflare refuses the
    second record as long as the first stands (§3.4).
*   **Shapes the contract names and nothing builds**: `public_port`,
    `backed_pvc`, the database pattern, the static site, the
    authentication gate. The route census's `Exposure` is an enumeration
    over concerns that are independent of each other, and the readings of
    what the legacy estate really uses show that most of what a port of
    it would carry is unused (§4.1, §4.7).
*   **Facts the legacy estate holds that the canon does not.** Its
    database backups are in Google Cloud Storage, not in the backup
    bucket. Its authentication service's users and second factors are
    backed up nowhere. One application's data sits on the shared
    JuiceFS volume, which migration.md §3 treats as the syncthing
    replica's alone. Legacy applications that stay behind for a wave
    depend on the authentication service and the mail relay that move
    first. The selectorless backend workloads.md §4 designs for Home
    Assistant is one a cloud node cannot reach. Both node volumes are
    smaller than their data, and the discovery server's clients are on a
    port the census does not hold. Each is settled where its wave is
    (§4, §5).
*   **The memory interleave, against measured numbers.** migration.md §0
    rule 4 grows the worker as legacy releases memory, towards
    [cluster/nodes.md](../cluster/nodes.md) §4.2's 20 GiB, and a
    rollback after a growth has nowhere to start the legacy application.
    The host's readings of 2026-11-05 put the ceiling below that end
    state, and lower still while the legacy cluster's own control plane
    runs (§6.1).

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
| One `route(row)` per HTTP exposure, the row's `Exposure` deciding gateways, record and rewrite; the rewrite is `dns`'s, from the same row (dns.md §3, §5; rfc-003 §6) | One row still decides everything, and the row is composed: `Exposure` gives way to a declaration whose parts carry only what applies to them (§4.1), with the legacy-held wrapper (§3.3) |
| The public port census, each row naming its transports, front and what answers it (rfc-007 §5.3) | One `Port` shape for every raw port, its front deciding the rest; the Gateways' own ports leave it (§4.1) |
| Every record of a type the proxy can front is proxied unless it states why not, at its declaration (dns.md §2) | Route rows carry the same reason before the first direct name migrates (§4.1) |
| `legacy.py` is one block per application, deleted whole at its migration (dns.md §6) | The block goes at the cut, not at the deploy (§3.2) |
| The stacks apply as `physical → dns ∥ k8s-base → apps`, with `apps` reading `dns`'s zone identifiers (dns.md §1); the deploy chain runs `up-apps` after `up-k8s-base` alone (framework/ci.md §3) | `up-k8s-base` and `up-apps` each wait for `up-dns`, whatever its result (§3.4) |
| A component receives the census it acts on; a census read by more than one program is a convention, one read by one program is data in that program's area ([style/pulumi.md](../style/pulumi.md), "Data") | Each application's component receives its rows; the ports, the backed namespaces and the single sign-on clients are conventions (§4) |
| A sealed value is a plain value in the declaring stack's configuration; `conventions.sealed` lists what `credentials` seals, strict (rfc-007 §6.2) | The legacy ciphertexts, and the writer keys sealed namespace-wide (§4.6) |
| The writer keys are `physical`'s, one scope per namespace on the backup bucket; a restic password is generated in state by `backed_pvc` ([credentials.md](../credentials.md) §2.2, §3) | Kept, with the sealed delivery the register leaves pending (§4.6) |
| The legacy sealing key is restored at the first ported manifest, its channel left open (rfc-007 §6.3, §15.3) | A one-off carry just ahead of Wave A1, retired by the decommission's rotation (§4.6) |
| How hath leaves on its own address is the migration's choice, the reserved address on its node's primary private address recommended (rfc-007 §15.3) | Taken (§5.4) |
| Which name the alert read route answers on, and whether `k8s-base` publishes a record (rfc-007 §15.3) | `mon`, published by `k8s-base` (§5.5) |
| The Gateways' apex listener arrives with the first application served at an apex (rfc-007 §5.2) | It arrives with the blog (§4.1) |
| The worker grows as legacy releases memory, toward 20 GiB (migration.md §0 rule 4, nodes.md §4.2) | Grown to fit, in drained windows, with a reading before each (§6) |
| A flip keeps the live service's feature parity unless its row records otherwise (sources-of-truth.md rule 4) | §4.7 lists every capability that goes, with its cost; §11.4 puts the ones a user would notice to the operator |
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
    test's own, and its rows in the censuses arrive with the wiring, in
    the wave that lands it. The build therefore touches no census, and
    no two builds share a file.

### 3.2 Land, verify, cut

An application then moves in two merges and one verification between
them:

1.  **Land.** The legacy application is stopped (§3.5) and its data
    moved (§5, by storage kind). The land pull request wires the
    component into the `apps` program and adds its route rows in the
    legacy-held state (§3.3). The application runs on the new cluster;
    its route attaches to the Gateways; no public record and no rewrite
    names it, so the name still resolves where the legacy estate
    publishes it.
2.  **Verify.** The name is requested at the cluster's front door
    directly — `curl --resolve <name>:443:<address>`, at the balancer's
    address for a public row and at the LAN VIP for a LAN-side one. The
    Gateway matches the route by hostname, and the zone's certificate,
    which the cluster holds for every zone a row names (rfc-007 §5.2),
    answers the handshake, so the request reaches the new application
    exactly as a client's will after the cut. Each wave in §5 says what
    its applications are verified by.
3.  **Cut.** The cut pull request deletes every legacy declaration of
    the name and takes the row out of the legacy-held state, so `apps`
    publishes the record and `dns` the rewrite. The legacy declarations
    are, by kind: the application's block in `legacy.py`; the web
    origin's address, for an apex (§4.5); a `$dnsrewrite` rule on the
    resolvers' declared list for the same name (sources-of-truth.md, row
    R2); and a row of the gateway proxy's
    `conventions.gateway.LEGACY_VHOSTS`, for a name under the retiring
    LAN zone, which `physical` serves until the cut and whose apply
    waits on that stack's reviewer.

**Why two merges.** Verification has to run against what is deployed,
and nothing is deployed until a merge applies it; the cut has to wait
for the verification. One merge cannot do both. **New rule**, landing
in migration.md §0 rule 1 and dns.md §6, whose "three files in one pull
request" becomes the cut's pull request, with the component's wiring
moved to the land's.

**A window's applications land together and cut together.** Stopping
several applications in one window costs one drained apply instead of
several, and the cut's pull request carries every name that verified. An
application that does not verify is left out of the cut, and its land
is reverted on its own (§3.6).

**A home-only application needs no legacy-held state**, whether its new
name is under a public zone for the first time or keeps a legacy block's
name, because it publishes no record to collide. Its row lands
published, and the cut deletes the legacy name's vhost row or block.
Where the resolvers' rule list already rewrites the name (§11.3), the
land deletes that rule, so the row's own rewrite is the only one.

### 3.3 The legacy-held route

**A row may be held by the legacy estate**: the census wraps it in
`Held`. A held row's route attaches to its Gateways, and the row
publishes no record and no rewrite; `served_zones` still counts its
zones, because verification needs the zone's certificate. **New rule**,
landing in dns.md §5.

**A row with a public half is held exactly while `dns` points its name
at the VPS.** A record points at the VPS when its content is the VPS's
address or its anchor (`legacy.IP_ARCHVPS`, `legacy.ANCHOR_ARCHVPS`),
wherever the `dns` stack declares it: an application block of
`legacy.py`, or the web origin's pair. A home-only row cannot collide
with such a record, since it publishes none, so it lands published and
its cut deletes the record. An `Apex` publishes no record, and has no
held state. The invariant is a relation between
`conventions.routes.ROUTES` and those records — a row with a public half
is held if and only if such a record holds the row's host in one of the
row's zones — and it is held in `tests/test_conventions.py` with the census
invariants, against a census that breaks it in each direction. A land
that forgets the wrapper, or a cut that deletes the record and keeps it,
is red there.

**Derivation was weighed and loses.** Deriving the state from the
records would make the illegal state unwritable, but `apps` would then
read the `dns` stack's record blocks, which makes them a census of two
readers that belongs in `conventions` (style/pulumi.md, "Data"), and
moves the record types with them — for a fact that exists only until the
migration ends. The wrapper is the cheaper statement, and the relation
test makes its one illegal state red.

**The wrapper leaves with the last such record.** It is a
migration-period statement: when nothing in `dns` points at the VPS, the
wrapper and its relation test are deleted together, and no row changes
shape. Its boundary is the class, the branch in `route` and in the
rewrite derivation that reads it, and that test.

### 3.4 The record changes hands in one run

A cut deletes the legacy record in `dns` and creates the application's
record in `apps` — or in `k8s-base`, for the monitoring component's
name (§5.5) — at the same name. Two facts force their order:

*   the pinned Cloudflare provider's `DnsRecord` has no option to adopt
    or overwrite a record that exists (`pulumi-cloudflare` 6.22.1, whose
    record arguments carry no such field), so each stack creates and
    deletes its own;
*   Cloudflare refuses a CNAME beside another record of the same name,
    so a create that runs before the delete fails.

**So `up-k8s-base` and `up-apps` each run after `up-dns`, whatever
`up-dns`'s result.** In `deploy.yml`, both name `up-dns` among their
needs; `up-k8s-base` keeps its condition on the `physical` gate, and
`up-apps` its condition on `up-k8s-base`'s success. That is dns.md §1's
order, `physical → dns ∥ k8s-base → apps`, with `k8s-base` now behind
`dns` too. A cut's run then deletes before it creates, and the name is
absent for the time between them. **New rule**, landing in
framework/ci.md §3.

*   **A failed `up-dns` blocks neither stack.** A run in which it failed
    still applies `k8s-base` and `apps`, so a failing `dns` — the stack
    that reaches the LAN — never stops the daily driver, which keeps the
    separation dns.md §3 put there. A slow or hung `up-dns` does delay
    both, by up to its job's bound, and so does its wait on the
    concurrency group it shares with the previews and drift checks that
    join the overlay: that is the cost of the order.
*   **A cut's run can leave a name unpublished.** Where `up-dns` deleted
    the legacy records and `up-k8s-base` then failed, `up-apps` is
    skipped and the cut's names resolve nowhere until a run succeeds. A
    window therefore re-runs the chain until it succeeds before it
    verifies the cut. Where `up-dns` failed before its deletes, the
    create fails on Cloudflare's refusal and the names stay where they
    were; the next run converges.

**Adoption was weighed and loses.** A record kept by `dns` with
`retain_on_delete` and imported by `apps` would change hands with no
gap, but an import is refused unless the declared inputs match the
record as it stands, so retargeting it takes a second apply of `apps`,
and the import needs a record ID the program would have to look up.
The gap it saves is a minute, inside a window in which the application
is already stopped.

### 3.5 Stopping and removing the legacy side

**A legacy application is stopped by a kluster-code change, not by
`kubectl scale`.** kluster-code's deploy applies on every merge to its
`main`, so a scale made by hand is undone by the next merge there,
whatever it is about. The stop sets the application's workloads to no
replicas and keeps every claim it holds. It merges at the head of the
window, before the data move. **New rule**, landing in migration.md §0
rule 1.

**A legacy service that other legacy applications depend on stops when
its last legacy consumer has moved**, not with its own successor's
land. Two such services move first:

*   **The mail relay** keeps relaying for the legacy applications that
    send through it — immich and the legacy monitoring — until Wave C's
    head, when both stop. The new relay signs with the same key under
    the same selector, so two relays signing at once is invisible to a
    receiver.
*   **The authentication service** stops at Wave A1's head, and the
    same kluster-code change points the legacy forward-auth checks
    at the new service's public authorization endpoints, the session
    check and the HTTP Basic check alike. The gated legacy fronts —
    `sync`, `dav`, `spool`, `sync-nas` — then ask the one service whose
    sessions the portal issues, until each is cut. Two services side by
    side would each hold their own sessions, and a sign-in at the portal
    would never satisfy the other. The checks' target is the
    legacy cluster's transitional state; it goes with the last gated
    legacy front, at Wave C's cut.

**It is removed by a second kluster-code change, after its cut has
soaked**: the component goes, and its claims' directories are mapped
or deleted in the retained-volume census storage.md §3.3 asks for. The
removal is part of the next window's head (§10.3), and it is what
releases the application's memory for the next growth (§6.3).

These are kluster-code pull requests, which the operator merges; the
slices of §10 carry them.

### 3.6 Rollback

| Where it fails | The rollback |
| --- | --- |
| Before the land merges | Revert the stop in kluster-code; nothing here changed |
| After the land, before the cut | Revert the land: `apps` deletes the workload, and no record or rewrite was ever published. Then revert the stop |
| After the cut | Revert the cut in one merge, so that the row is held again, and its legacy records are back in the same commit, as §3.3's relation requires. Its first run fails in `up-dns`, on Cloudflare's refusal to recreate a record beside the application's, while `up-k8s-base` and `up-apps`, which run whatever that result, delete the application's record. A re-run of the chain then recreates the legacy record. Then revert the stop |
| After a growth of the worker | The window reversed (§6.4), before the legacy application can start |

Between `apps` deleting its record and the re-run, the name resolves
nowhere; the window re-runs the chain at once.

**Data written to the new application after its cut is not carried
back.** Rule 1's rollback restarts the legacy application on its own
data, which the stop left intact, and that is the rollback's cost for
each application with state; each wave names its own.

**A database whose land is reverted is landed again under a new server
name.** Its first land archived to the bucket, and barman refuses to
archive into a server's path that already holds an archive; emptying it
would hide the first attempt's archive (a delete is a hide here,
storage.md §4), so the retry's cluster takes the next generation of its
server name instead (§4.3).

--------------------------------------------------------------------------------

## 4. The shared shapes

Each shape is designed once and built in its own slice before any
application uses it. Placement on the application base or in
`kluster.lib.k8s` follows rfc-007 §9.1: what one stack calls lives with
the base, what two call lives in the library.

### 4.1 Exposure

**What each application needs, asked from scratch.** The question put to
each application is how it would be reached if it were added to this
cluster today, against what its clients do now. The readings are the
legacy estate's of 2026-11-06: the ingress log, which reaches 23 hours
and carries no client address; each application's own log; the host's
records; and 61 days of byte counts.

| Application | What the readings show | Reach |
| --- | --- | --- |
| authelia | every signed-in application | public |
| splitpro | a handful of requests a day | public |
| matrix | a browser client's long poll, and federation on 443 | public |
| the WebDAV server | 1.9 GiB in October, nothing since; its own files are live | public, Basic gate |
| the discovery server | six devices announcing, all on 443 | public, passthrough |
| the syncthing replica's peer port | every device, and the NAS instance by name | raw port, balancer |
| the syncthing interfaces | nothing but probes in the log | home, gated |
| Home Assistant | the companion application and a model client | public |
| immich | phones syncing and uploading, some over 100 MB | public direct, and home |
| jellyfin | the televisions, on the Default LAN at the host's name; light use from outside | public, and home |
| Grafana | nothing in the log's window; `mon` stays a public row (rfc-007 §7.4) | public |
| qbittorrent | Home Assistant, by its own login | public gated, and IoT |
| hath | the network, at one address | raw port, dedicated |
| golinks | typed as `go/<name>` | home, bare name |
| spoolman | Home Assistant, on the IoT VLAN | IoT |
| seedwatch | its interface | home |
| emailproxy | Home Assistant, on the IoT VLAN, and two host jobs | raw port, media VIP |
| the blog and the doors | visitors and crawlers | public |

**Most of what a port of the legacy estate would carry goes**, and §4.7
lists each with what it costs. What is left is small enough to state as
one type.

**Exposure is composed, so that a combination that means nothing cannot
be written.** `Exposure`, one enumeration over a cross-product of
independent concerns, gives way to a declaration whose parts each carry
only what applies to them. **New rule**, landing in dns.md §5 and
style/pulumi.md ("Data"), replacing rfc-003 §6's "one field rather than
two flags".

```python
class Gate(Enum):  # what the ExternalAuth filter asks Authelia for
    SESSION = ...  # the portal's session
    BASIC = ...  # HTTP Basic, for a client that cannot follow a redirect


class Side(Enum):  # which home VIP answers: which VLANs reach it
    LAN = ...
    IOT = ...


@dataclass(frozen=True)
class Proxied: ...


@dataclass(frozen=True)
class Direct:  # published past the proxy, and why
    reason: str


@dataclass(frozen=True)
class Public:  # the internet's half: a public record
    edge: Proxied | Direct
    gate: Gate | None = None


@dataclass(frozen=True)
class Home:  # the home's half: a rewrite
    side: Side
    gate: Gate | None = None
    bare: bool = False  # the bare label resolves and redirects


@dataclass(frozen=True)
class Both:
    public: Public
    home: Home


@dataclass(frozen=True)
class Name:  # an HTTPS name, routed by the Gateways
    host: str
    reach: Public | Home | Both
    zones: Sequence[str] = PRIMARY_ONLY


@dataclass(frozen=True)
class Apex:  # a website's apex; `dns` holds its address
    zones: Sequence[str]


@dataclass(frozen=True)
class Passthrough:  # TLS the pod terminates, on 443, by name
    host: str
    reason: str


@dataclass(frozen=True)
class Port:  # a raw port, named for what it serves
    name: str
    port: int
    transports: Transports
    front: Balancer | Dedicated | Vip | SiteForward
```

`Vip` carries its `Side` and the host `dns` rewrites to it; the other
three fronts carry nothing. `Held` wraps a `Name` or a `Passthrough`
while the legacy estate holds the name (§3.3), so the migration's state
is a wrapper the census sheds rather than a field every row carries.

**What can no longer be written**, each of which a census test or a
review would have had to catch:

*   an `unproxied` reason on a LAN-only row: `Direct` exists only inside
    `Public`;
*   a bare name on a row with no home half: it is a field of `Home`;
*   an apex with a home half, a gate or a reason: `Apex` has none of
    them, and `Name.host` is a label type that refuses `@`;
*   a gate on a raw port, or on a name the pod terminates: `Port` and
    `Passthrough` have no gate;
*   a raw port behind the proxy: `Port` has no edge and no record;
*   a row that reaches nobody: `reach` has no empty value, and a zone
    set is a non-empty tuple;
*   a reason that says nothing: `Direct` and `Passthrough` refuse an
    empty one where they are constructed;
*   a Gateway answering on the dedicated address, which a census test
    holds today: the Gateways' own ports leave the census for
    `GATEWAY_PORTS`, always on the balancer, and `Answerer` goes.

What a type cannot carry stays a census invariant: known zones; names
unique across `Name`, `Passthrough` and a `Vip`'s host; one claimant of
a port on a front, `GATEWAY_PORTS` counted; an `Apex` zone holding its
address record; and §3.3's relation.

**A gate is per side, because the two sides differ.** The home VLANs have
reached these interfaces ungated, and the legacy LAN vhosts carry no
gate. So a row states a gate where its side wants one, and `bt` is gated
from the internet and not from the home, where Home Assistant signs in
with qbittorrent's own login. That narrows cluster-infra.md §2's list of
applications behind the gate to their public side. `route` emits one
`HTTPRoute` per side.

**A gate is declared on the row, not at the call**, so the census says
which names are gated. Three route details stay in the component that
owns them, because no second program reads them: the delegation's header
on the blog's apex, the canary's protected path on the portal's name,
and the alert read route on `mon`. Each is a rule of its component's own
beside what `route` emits.

**`route` and the port are one census's two kinds, and they stay two.**
A `Name` is host-routed: it shares the Gateways, takes a wildcard
certificate and has no port. A `Port` is port-addressed: it has a
Service of its own, no hostname on the wire and no certificate.
Forcing them into one record would make each field optional on the
other kind, which is the state this section removes. `public_port` and
the LAN port, by contrast, are one shape: the `port` helper takes the
row, and its `Front` decides the rest. One component call, `expose(rows)`,
takes an application's rows of either kind.

**Every consequence but the apex's record derives from the row:**

| Consequence | `Name` | `Apex` | `Passthrough` | `Port` |
| --- | --- | --- | --- | --- |
| Gateways | `internet-gw` for a public half; `lan-gw` or `media-gw` by `Side` | `internet-gw` | `internet-gw` | none |
| Listener | the zone's wildcard HTTPS | an HTTPS listener for the apex, per zone | a TLS passthrough listener on 443, for the host | none |
| Certificate zones | each it names | its zones | none | none |
| Public record | a CNAME to the cluster anchor per zone, proxied by its edge | `dns`'s web origin, written in its block and held to the row by an invariant | a direct CNAME to the anchor | none |
| Rewrite | per zone, at its `Side`'s VIP, and the bare label if asked | none | none | `Vip`: its host, at its `Side`'s VIP |
| Service | none | none | none | LoadBalancer: the cloud nodes and the balancer's two addresses for `Balancer`, the dedicated node's for `Dedicated`, the VIP for `Vip`, the worker's for `SiteForward` |
| Balancer listeners | none | none | none | `Balancer`: one per family |
| Firewall | none | none | none | `Vip` on `IOT`: the IoT allow for its port; `SiteForward`: the forward and the pinhole; the others are answered ahead of the node firewall |

**The worked table is the proof that it fits.** The real rows, under the
recommendations of §11.4:

| Application | Row |
| --- | --- |
| authelia | `Name('auth', Public(PROXIED))` |
| splitpro | `Name('split', Public(PROXIED))` |
| matrix | `Name('matrix', Public(PROXIED))` |
| the WebDAV server | `Name('dav', Public(PROXIED, gate=BASIC))` |
| Grafana | `Name('mon', Public(PROXIED))` |
| Home Assistant | `Name('haos', Public(PROXIED))` |
| the blog | `Apex(WEB_ZONES)`, `Name('www', Public(PROXIED), WEB_ZONES)` |
| the doors | `Apex(('jiahui.love',))`, `Name('game', Public(PROXIED), COHOST)`, `Name('games', Public(PROXIED), COHOST)` |
| immich | `Name('photos', Both(Public(Direct('uploads past the proxy limit')), Home(LAN)))` |
| jellyfin | `Name('tube', Both(Public(PROXIED), Home(LAN)))` |
| qbittorrent | `Name('bt', Both(Public(PROXIED, gate=SESSION), Home(IOT)))` |
| the syncthing interfaces | `Name('sync', Home(LAN, gate=SESSION))`, `Name('sync-nas', Home(LAN, gate=SESSION))` |
| spoolman | `Name('spool', Home(IOT))` |
| seedwatch | `Name('seedwatch', Home(LAN))` |
| golinks | `Name('go', Home(LAN, bare=True))` |
| the discovery server | `Passthrough('syncapi', 'clients present a certificate the proxy would end')` |
| the syncthing replica | `Port('syncthing', 22000, TCP_AND_UDP, Balancer())` |
| hath | `Port('hath', 60011, TCP, Dedicated())` |
| emailproxy | `Port('imap', 1993, TCP, Vip(IOT, 'imap'))` |
| qbittorrent's peers | `Port('qbittorrent-peer', 53363, TCP_AND_UDP, SiteForward())` |

Two things the rows need of the platform:

*   **A raw LAN port shares its side's VIP.** `Port('imap', …)` takes the
    media VIP beside `media-gw`, under the sharing annotations
    `internet-gw` already carries (rfc-007 §4.4), so no address is
    added: ports tell the Services apart. `media-gw`'s Service gains
    them, and the raw Service takes `Cluster`. rfc-007 §4.4's sentence
    that each `lan` VIP is held by one Service changes with it.
*   **`SiteForward` needs an address to land on.** The gateway's forward
    and pinhole end at the worker's own address, which is the address
    qbittorrent's outbound traffic wears, and no Service may hold a host
    port. So `k8s-base` gains a third pool holding the worker's IPv4 and
    GUA, of the kind the `internet` pool's node addresses already are,
    and the peer Service asks for it: the IPv4 from `conventions`, and
    the GUA from a new `physical` output of the `workerGua` it already
    reads. `QBITTORRENT_PEER_PORT` becomes the row's, and `SiteFirewall`
    reads the port from it.

**`physical` still declares the firewall; only the source of its ports
moves.** `SiteFirewall` stays that stack's, and its rows stay its rows. The peer forward and pinhole read the port from the
census instead of a constant, and the IoT allow gains a rule per
`Vip(IOT, …)` port beside `MEDIA_PORT`. Nothing here depends on how the
open question on the Internal-to-External rule's order is ruled; the
slice that touches `SiteFirewall` follows the work that settles it.

**A TLS passthrough keeps the discovery clients on 443.** The discovery
server terminates its own TLS because it reads each client's
certificate, and every device is configured with the name and no port.
The legacy path is a passthrough on 443. A raw port on 8443, which the
census held, would have broken every device until it was reconfigured
by hand. A Gateway listener in passthrough mode for one hostname, with a
`TLSRoute` to the server, keeps them, and drops the census row and its
balancer listeners with it. architecture.md §6.7's refusal of Envoy for
raw traffic argues that a proxy hop buys nothing; here it buys not
touching a device. A raw port on 443 is unavailable, since the Gateways
hold it. The server sees no client address, as it does today. **Unproven
live**, with the raw port and a reconfiguration of each device as its
fallback.

**A LAN-device backend** is a small reverse proxy on the worker, which
the route's Service selects, forwarding to the device's address and
passing the client's address on in `X-Forwarded-For`. workloads.md §4
designs it as a selectorless Service with an `EndpointSlice` at the
device's address, but a request through `internet-gw` reaches a cloud
node's Envoy, which connects to the endpoint itself, and a cloud node has
no route to a home VLAN (physical/homelab-host.md §5). The worker reaches
the device over the cluster zone's allow toward the home networks
(physical/gateway.md §4.2). The device trusts the worker's address as
its proxy. **New rule**, landing in workloads.md §4.

**`apps` reads the `internet` pool's members across a StackReference**,
through `kluster.lib.stack_addresses` as `k8s-base` does, since a
`Balancer` or `Dedicated` port's addresses are `physical`'s outputs. It
is a recorded use beside the others style/pulumi.md names: machine facts,
read so that anything but an address stops the run. **New rule**,
landing in style/pulumi.md ("Layering") and declarative/README.md §2.

The route census states the reason a name is published direct, as
dns.md §2 requires of every record, because `Direct` carries it. A
direct row, and every record it aliases, holds the chain relation the
blocks do, and the route-side half of the record blocks' co-host check
(`tests/test_dns_records.py`) holds for rows too. The authentication
canary cluster-infra.md §2 requires lands with Authelia's component
(§5.2).

### 4.2 Storage

**Three kinds of claim, each with what it owes:**

*   **`backed_pvc(name, size, retention)`** is written as workloads.md
    §3 states it: a `local-path` claim, and a VolSync
    `ReplicationSource` with the restic mover, scheduled by the
    retention class, to the repository at
    `conventions.volsync_repo_path`. It generates the repository's
    password into state, as credentials.md §3 says it does, and its
    repository Secret is a SealedSecret under the claim's own name that
    carries the namespace's sealed writer key (§4.6) with the password
    and the repository's path in its template. The password reaches no
    file in the repository: the template's values are in state, which is
    where credentials.md §2.2 keeps the password. They are also in clear
    in the SealedSecret object, which encryption at rest does not cover,
    and so in every etcd snapshot the bucket keeps: a residual, recorded
    in the security audit, whose actor is a reader of the snapshots, who
    holds no key to the VolSync prefix and so opens no repository with
    it.
*   **`scratch(name, size, reason)`** is a `local-path` claim with no
    backup, for data that re-derives and is worth keeping across a
    restart: the reason is a required argument, which is workloads.md
    §2's `backup=None` as a shape. Data that need not survive a restart
    takes an `emptyDir` and no claim.
*   **`fixed_claim(name, source)`** is a static `PersistentVolume` and
    its claim, for data the cluster does not own, with no backup
    parameter to set, since the source's own regime is its safety net.
    NAS and node volumes differ only in `source`, so they are one shape:
    *   `Nfs(path, read_only)`: a path under the host's one export, on
        the worker alone, since no other node can reach it
        (physical/homelab-host.md §5). The host exports `/mnt/nas` whole
        to the worker, and the path is a single filesystem with no
        dataset boundary, so a component names its path at the call. No
        census is needed, and no export changes with a new path.
    *   `NodeVolume(row, directory)`: a `local` volume over a row of
        `conventions.NODE_VOLUMES`, at a directory inside the mount and
        never the mount point, with node affinity on the volume's label.
        The directory is created by a seeding step the shape declares,
        so a volume that failed to mount leaves the pod pending rather
        than writing into the node's root.

**A pod writes to the NAS as the export's anonymous user.** The export
is `all_squash`, so every NFS write arrives as one user, whatever the
pod runs as, and `restricted` needs nothing of it. A path a pod writes
to is therefore owned by that user. Paths the legacy pods wrote as root
are not, so the head of the wave that lands such an application reads
the owner and re-owns the path on the host, which for immich's library
is a long walk and runs ahead of the window. qbittorrent's paths are
already that user's.

**What each application stores, from scratch**, against what it holds
today:

| Application | Today, on disk | From scratch |
| --- | --- | --- |
| authelia | 1.1 MB | `backed_pvc`, `PRECIOUS`; its sessions in a Redis with no claim, since losing them costs a sign-in |
| splitpro | an empty uploads directory; a 16 MB database | no uploads claim, an `emptyDir`; the database (§4.3) |
| matrix | 18 MB, on a 20 GiB claim | `backed_pvc`, `PRECIOUS` |
| the WebDAV server | 1.1 GiB of its own files, on JuiceFS | `backed_pvc`: its own files are in no syncthing folder, so the replica's reseed does not cover them |
| syncthing, cloud | 125 GiB; a 200 MB index | one `fixed_claim` over `syncthing-replica`, the index beside the data, since it re-derives and the volume outlives the node |
| syncthing-nas | 115 GiB on the NAS; 145 MB index and device key | `Nfs('Sync')`; `backed_pvc` for what holds the device key |
| stdiscosrv | 2 MB | an `emptyDir`: it rebuilds in minutes |
| immich | 1.5 GiB database; library, thumbnails and encoded video together on the NAS; 11 GiB model cache; 27 MB queue | the database (§4.3); `Nfs('Immich')` whole; `scratch` for the models; a Redis with no claim, since a lost queue re-runs |
| jellyfin | 21 GiB of configuration: 11 GiB its own backups, 7.9 GiB scrub previews, 2.6 GiB metadata; a 512 GiB claim holding 116 MB | `backed_pvc` for the database and settings; `Nfs` for scrub previews and metadata; its backups not copied; an `emptyDir` for transcodes |
| golinks, emailproxy, spoolman | 21 KB, 5 KB, 4.5 MB | `backed_pvc` each |
| qbittorrent | a 175 MB profile; payloads on the NAS | `backed_pvc`; `Nfs('')`, the whole export, because hard links into `Media` need one mount, and a `Downloads` mount and a `Media` mount would fail them across the mount boundary |
| seedwatch | 97 MB in memory; audit history | `backed_pvc`; `Nfs('Downloads', read_only)` |
| hath | 52 GiB of cache, 13 KB of state, on the NAS | one `fixed_claim` over `hath-cache`, the state beside the cache, since nothing in it is worth a restore |
| the doors | static files | `Nfs` read-only |

**Both node volumes are too small for what they hold, and the first
`physical` apply creates them.** `syncthing-replica` is 110 GB against
115 to 125 GiB of data, and `hath-cache` 50 GB against a 52 GiB cache.
They become 150 and 60 GB, about $1.30 a month beyond the storage
overflow nodes.md already counts. It is one line each in
`conventions.NODE_VOLUMES`, and it lands ahead of this document, before
the first `physical` apply, by an issue of its own.

**Thumbnails stay with the library.** workloads.md §2 lists them as
`local-path`, re-derivable, but they sit under immich's one media root
and run to hundreds of gigabytes, which no worker disk holds. Their
latency is NFS's until a measurement says otherwise
(physical/homelab-host.md §5's criterion for revisiting it).

### 4.3 Databases

A database is a CNPG `Cluster` on `local-path`, archiving through the
barman plugin to the backup bucket at `conventions.barman_repo_path`,
with its scheduled backup and retention taken from a retention class
(workloads.md §3). Three decisions are taken here.

**One instance, `instances: 1`, with no standby, and the shape takes no
instance count.** The second instance a CNPG cluster can run would be
scheduled to the same machine as the first: every database here is
pinned to one site, and the homelab site has one worker. It would share
the primary's node, disk and failure domain, and add no availability
against any failure the installation plans for, while costing its whole
memory request a second time — legacy immich's database runs two
instances on one node, the VPS, each requesting 2 GiB. Durability is barman
to the backup bucket, and the restore drill below is what proves it.
**One cluster shared by both databases** would not fit their placement
either: splitpro's database runs on the cloud pool beside it (Wave A2),
immich's on the worker (Wave C), and immich's needs the `vchord` image,
splitpro's the `pg_cron` one. **New rule**, landing in workloads.md §4.

**A migrated database bootstraps by recovery from the legacy object
store.** The legacy databases archive to Google Cloud Storage, one
bucket per database, and the new cluster recovers from it through the
barman plugin, with the legacy credential ported as a namespace-wide
ciphertext (§4.6) — migration.md §3's "barman restore, not dump/restore
reinvention", against the store the legacy cluster actually writes. The
recovery runs once, at the land; the cluster then archives to the
backup bucket under the new layout, with its server name carrying a
generation, so that a land retried after a revert archives to a path of
its own (§3.6). The legacy store is read by nothing after the cut.
Whether the plugin recovers a backup that the legacy in-tree archiver
wrote is a live reading (immich's, §5.6).

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
for each client, the host its redirect addresses are under, their
paths, its authorization policy and its authentication method. Two programs read it: `apps`, which renders
Authelia's client list and gives each application its client, and
`k8s-base`, whose monitoring component gives Grafana its client.

**The census carries every client the legacy authentication service
carries, from Wave A1.** At A1's cut the new service takes the portal's
name, and every legacy application that signs in through it signs in
there from then on. So an application that has not moved has its client
already, under the host it keeps across its move — the legacy name,
which `dns` still points at the VPS until the cut and its route row
publishes after — and no land edits the census. The relation test holds
every client's host to a name that a route row publishes or that `dns`
points at the VPS (§3.3), in a zone the row or the record names.

**A client's identifier and secret are not in the census.** The legacy
service keeps both sealed, and the port keeps them so: each is a ported
ciphertext in the configuration of the stack that declares its consumer
(§4.6), and no one reads the legacy cluster to write an identifier
down.

Authelia's access-control rules for the forward-auth applications are
its own configuration and stay in its component; the census holds only
what a second component has to agree on.

### 4.5 Static sites

The `StaticSite` component of workloads.md §4: a stock static server
with several hostnames, one route per zone, parameterized by its
content source.

*   **A git source** pulls a built branch through a git-sync sidecar
    into an empty directory. The blog instance runs more than one
    replica, spread across the cloud nodes, and carries the matrix
    delegation's static files and the CORS header the route adds to
    them with a `ResponseHeaderModifier`.
*   **A NAS source** mounts its dataset read-only (§4.2), on the worker.
    The doors instance serves its hostnames from it.

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
    value is sealed afresh against the cluster's own key: Grafana's
    values, whose namespace becomes the monitoring component's. **New
    rule**, landing in workloads.md §5. These are per-application
    secrets, which the program enumerates and the register does not
    (credentials.md §1, rule 8).
*   **The legacy sealing key is carried by hand once**, at the
    bring-up's last stage, from the legacy cluster's controller to the new one's, by a
    pipe between the two clusters' API servers that writes it to no
    file; the controller is restarted to read it. After that, the
    certificate the controller serves is confirmed to be the cluster's
    own, since a restored key can become the one that seals (rfc-007
    §6.3). It needs no channel of its own: it is a transitional
    credential whose last use is the decommission's re-sealing, which
    deletes it. Its register row says exactly that. **New rule**,
    landing in credentials.md §3.
*   **The backup bucket's writer keys stay `physical`'s**, one scope per
    namespace and family on the bucket (`volsync_scope`,
    `barman_scope`), each minted from the B2 management key as
    credentials.md §3 already has it. Which namespaces back anything up
    becomes a census, `conventions.backup.BACKED_NAMESPACES`, because
    three programs agree on it: `physical` declares each namespace's
    scopes from it, `credentials` seals their keys, and `apps` declares
    the claims and databases that use them. It lists every namespace the
    waves back up from the start, so `physical`'s first apply after it
    mints every key the migration needs.
*   **`credentials` delivers each writer key into `apps`'s
    configuration**, reading it from `physical`'s state under that
    stack's passphrase and sealing it into the `apps` stack's
    `sealedSecrets` — the shape `credentials derived sync` already has
    for the kubeconfig. The keys are sealed and never configuration
    secrets: under the stack passphrase they would reach every pull
    request's preview (credentials.md §1), and a writer key with a
    repository's password reads every backup in its prefix. A key is
    sealed **namespace-wide**, rather than strict as rfc-007 §6.2 has
    every value `credentials` seals, because one ciphertext serves every
    repository Secret in its namespace under that Secret's own name. The
    seal needs the running controller, so it is one operator step at the
    bring-up's last stage, for every namespace at once, after the
    certificate the controller serves has been confirmed. **New rule**,
    landing in credentials.md §3 and §4 and cluster-infra.md §1.1.

### 4.7 What goes, and what it costs

sources-of-truth.md rule 4 keeps a flip's feature parity unless its row
records otherwise, so nothing below is silent: each flip's row in that
document carries what its own wave drops. An item with a cost someone
would notice is put to the operator (§11.4); the rest are settled here,
on the readings of §4.1.

| What goes | Why | What it costs a user |
| --- | --- | --- |
| The name `btsync.unlimitedcodeworks.xyz` | No configuration in the estate names it, on either syncthing instance | Nothing found; a device that held the old name would fall back on discovery |
| The discovery server's raw port on 8443 and its balancer listeners | A passthrough on 443 keeps the clients (§4.1) | Nothing |
| The matrix identity SRV, with `Extra`, `Srv` and `SELF` | Nothing answers it: the server has no identity endpoints, which answer 404 today | Nothing. Its one row was why the types existed |
| `matrix` published direct | Federation is on 443 (its delegation says so), which the proxy carries, so the block's reason is stale | Nothing, given the live check; media over the proxy's body limit would not upload |
| Jellyfin's ports 8096, 8920, 1900 and 7359, and the host's discovery relay | Each client is reached by name | Each television's saved server address, at the host's name and port, is re-entered once, and a new client types the name |
| immich's port 2283 | The phones use the public name, which the home half steers | The photo scripts' first choice of address moves to the name |
| spoolman's port 8000 | Home Assistant reaches the name over the IoT side, on HTTPS | Its integration's address is re-entered once |
| The ports of golinks, shoko and syncthing-nas | No client beyond the proxy; the NAS instance's peers are dialed outbound | Nothing found |
| The claims of authelia's and immich's Redis, stdiscosrv and splitpro's uploads, and jellyfin's 512 GiB cache | Each holds nothing a restart should keep | A Redis restart signs everyone in again, or re-runs immich's queue |
| The second immich database instance | §4.3 | A failover takes a restart rather than a promotion |
| shoko | §11.4, item 1 | Put to the operator |
| thread-dashboard's move | §11.4, item 2 | Put to the operator |
| The public reach of `sync`, `sync-nas` and `spool` | §11.4, item 3 | Put to the operator |
| The bare names but `go` | §11.4, item 4 | Put to the operator |

**The televisions were the reason `media-gw` exists**, and they are not
on the IoT VLAN today. It stays, for what is: Home Assistant, which
reaches `spool`, `bt` and `imap` from there. So **gateway.md §4.2's rule
1 narrows its source from the IoT VLAN to Home Assistant's own address**,
a literal in `conventions`, with a printer added if §11.3 finds one.
The rule's reason was the VLAN's televisions, and with them gone, an
allow for the whole VLAN would hand "the likeliest compromised host at
home" (threat-model.md) qbittorrent's login and an interface with none.
The narrowing keeps architecture.md §3.4's statement that those stay out
of the IoT VLAN's reach true of every device on it but one, and the one
is the home's own automation, which holds their integrations already.
**New rule**, landing in gateway.md §4.2 and architecture.md §3.4.
migration.md's Wave C check on the IoT path is replaced by re-entering
each television's address.

--------------------------------------------------------------------------------

## 5. The waves

Each wave lists its applications in the order they land. For each:
what the component declares beyond its shapes, how its data moves, how
it is verified, what the cut deletes, and what its rollback costs. Each
application's row is §4.1's, and its storage §4.2's.

**A component's image is the legacy one**, pinned in `Pulumi.yaml`'s
`versions:` block (§10, slice 2) — or, for a component on the cloud
pool, which is arm64, the oldest release after the legacy one that
publishes that architecture, where the legacy one does not. **The
components declare their workloads directly rather than through the
legacy charts**: the immich chart and the Bitnami Redis chart carry no
security context that meets `restricted`, and the Bitnami images have
moved to a legacy namespace of their own.

**Data technique for a legacy `local-path` claim**, here and in every
later wave. A claim bound for the worker is copied onto the NAS on the
host and read from there by a pod that mounts both, because the data is
already on the machine that serves the NAS. A claim bound for the cloud
pool is streamed with `tar` through the new cluster's API server into a
pod that mounts the target claim (`kubectl exec -i … tar -x`).
migration.md §3's "over SSH" has no second end, since a Talos node runs
no SSH server.

### 5.1 Before Wave A

[physical/bring-up.md](../physical/bring-up.md) orders everything from
the gateway window to a verified, empty cluster; it is a runbook, and
this document starts where it ends. Its last step passes on what the
first wave needs: the legacy sealing key carried, and the writer keys
sealed (§4.6). Beyond it:

*   The shapes of §4 and the build slices of the waves' applications
    have merged (§10).
*   `physical` has applied `BACKED_NAMESPACES`, so every writer key
    exists.
*   Row R2 of sources-of-truth.md has flipped before the first row with
    a home half lands, `sync` in Wave A2: adguardhome-sync stopped, both
    resolver instances reading their rewrites again, and the resolvers'
    declared list merged. Until then a rewrite applies, reports success
    and answers nothing.

### 5.2 Wave A1: exim and authelia

**exim moves to the head of Wave A.** Authelia sends its notifications
— password resets and second-factor enrollment — through it, as immich
and Grafana do later, so a relay that moves in Wave B leaves the
authentication service without mail for a wave. Its placement is free
(workloads.md §5), and it runs on the worker, the architecture its
legacy image is built for.

*   **exim** declares its relay as workloads.md §5 does: the `dkim-exim`
    value mounted where exim reads `tls.key`, no `Certificate`, the
    `k8s` selector, its SMTP credential ported (§4.6) in `mail-system`.
    Its relay range is the cluster's pod ranges. No data. **Verified**
    by a message relayed from a pod and its DKIM signature checked at
    the receiver, and by sources-of-truth.md §X's digest against the new
    cluster's Secret. The legacy relay keeps relaying for its legacy
    senders until Wave C's head (§3.5). **Rollback** loses nothing; the
    relay holds no state.
*   **authelia** declares its server with its configuration rendered
    from the component, its access-control rules ported with it, every
    client of the census (§4.4), and the `ext_authz` endpoint the
    gateway filter calls. Its users and second factors are backed up
    nowhere today; the backed claim is what changes that. **Data**: the
    legacy claim's users file and SQLite database, by stream. **Verified**
    before the cut by a login with a second factor at the balancer and by
    the canary green. The legacy applications and the legacy forward-auth
    checks reach the service by its public name, which resolves to the
    stopped legacy service until the cut, so they are **verified after
    it**, as the cut's own done-means: a sign-in to one legacy application
    through its client, and a request through one legacy gated front
    after a portal sign-in, with §3.6's rollback after a cut if either
    fails. **Cut**: `auth`. **Rollback** loses the enrollments made
    since the cut, and points the legacy forward-auth checks back.

**The canary of cluster-infra.md §2 ships with authelia**, with a target
of its own, because no gated route exists before Wave A2: a protected
path on the portal's name, served by a static responder in authelia's
namespace. A `CronJob` requests it with no credential every few
minutes, at the Gateway's address with the portal's name as its host,
so that it answers the same before the cut as after, and publishes the
result, and a rule fires on anything but a refusal, with its tier and
playbook. The same request, made with the filter's target unreachable,
is part of the verification, and closes the two items the bring-up's
gate could not run: the filter failing closed, and the canary alerting.
The third, a writer key's prefix isolation, is read at the head, once
the keys are sealed.

### 5.3 Wave A2: blog, splitpro, matrix, syncthing, stdiscosrv

**The wave's land brings the website co-host zone into the served
zones**, through the blog's rows, so the cluster's DNS-01 token is
minted afresh at the wave's head, scoped to them, into the land's
branch.

*   **blog**: the `StaticSite` with its git source (§4.5), and the
    Gateways' apex listeners. No data; the built branch is the content.
    **Verified** at the balancer for every hostname, including both
    delegation files with their header. **Cut**: the web origin's served
    apex, and `www` in both website zones.
*   **splitpro**: its server on the cloud pool, its database by recovery
    from the legacy store (§4.3), its client from the census. It sends
    no mail. **Data**: the database by recovery, once the legacy server
    is stopped and a final backup has run. **Verified** by a sign-in
    through the single sign-on flow and a recent expense read back.
    **Cut**: `split`. **Rollback** loses the expenses entered since the
    cut.
*   **matrix**: continuwuity on the cloud pool, its client from the
    census. **Data**: the claim by stream, after the server is stopped.
    **Verified** by a client sign-in and by a federation check of the
    server name, which reads the delegation the blog serves, through the
    proxy; where federation does not survive it, the row goes `Direct`
    with that as its reason. **Cut**: `matrix`, and its identity SRV goes
    (§4.7). **Rollback** loses the events since the cut, which federation
    partners retry.
*   **syncthing**, the cloud replica: the shared component, in its cloud
    form — the device key ported, so the device keeps its identity, and
    the WebDAV server beside it, serving its own files from a backed
    claim and the synced folder from the volume. Its peer port is the
    `syncthing` row on both transports, where the legacy Service exposed
    TCP alone. **Data**: the WebDAV server's own files by stream, off
    JuiceFS; the replica reseeds from its peers (migration.md §3), which
    takes the home uplink some hours for 125 GiB and blocks nothing.
    **Verified** by the peers connecting under its device identity, the
    interface refusing an unauthenticated request at the LAN VIP, and
    `dav` refusing one at the balancer. **Cut**: `dav`, and `sync`'s
    legacy block; its row lands published (§3.3). **Rollback** restarts the legacy
    replica on its JuiceFS volume, which the stop kept.
*   **stdiscosrv**: the `Passthrough` on `syncapi`, terminating its own
    TLS with a certificate the cluster's issuer gives it for the name.
    The syncthing-nas instance dials the replica at that name on port
    22000, which the same record serves. **Data**: none; its
    announcements are rebuilt within minutes. **Verified** by a client's
    announcement and lookup on 443. **Cut**: `syncapi`.

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

**The `physical` change comes first**, in a slice of its own applied at
hath's head: the reserved address moves onto the node's primary private
address, and the secondary address goes with everything that names it —
its pool member, the outputs, and the machine configuration's secondary
address. A private address carries one public address, so the node's
ephemeral one is released for it; whether the pinned provider releases
it in place, or only by replacing the instance, is that slice's first
reading. A control-plane node's replacement is a different design, and
the slice stops and reports if that is what it would take.

**hath moves in three merges**, because its cache is copied warm while
the legacy client still serves, and two clients online under one login
are refused by the network:

1.  **Stage.** The component is wired with its client not started, and
    next to it, a copy pair: a reader on the worker over `Nfs('Hath')`,
    and a writer on the dedicated node over the cache claim. The warm
    copy runs.
2.  **Land.** The legacy client is stopped, the final delta and the
    client state are copied, and the client starts.
3.  **Cut.** The new address is registered with the network, and the
    copy pair and the not-started parameter are deleted. The parameter
    is a migration layer of the component's own: its boundary is the
    parameter and the copy pair, and it goes here.

*   **hath**: its client on the node the volume follows, its login
    ported, strict CPU limits, and the `hath` row. The legacy egress pods
    and their WireGuard keys do not port. **Data**: 52 GiB over the home
    uplink, a few hours. **Verified** by the client starting with no
    cache rebuild, the network's test of its address passing, and its
    outbound connections arriving from the reserved address.
    **Rollback** before re-registration restarts the legacy client on
    its NAS cache; after it, a second re-registration back.

### 5.5 Wave B

Every application of Wave B runs on the worker, and its requests fit the
worker as it booted (§6.1), so the wave needs no window.

*   **golinks**: its row, with its bare name `go`, which is how it is
    used. **Data**: its store through the NAS. **Verified** by a link
    followed through the bare name. **Cut**: the `go` vhost row.
    **Rollback** loses the links made since the land.
*   **emailproxy**: its configuration ported (§4.6), and the `imap` row,
    which Home Assistant's mail integration and the two host jobs are
    pointed at in the same window, on the media VIP because Home
    Assistant sits on the IoT VLAN. It serves TLS itself, as it does
    today, on a certificate from a self-signed issuer of its own
    namespace, since its clients do not verify it and the name is not
    one to publish in certificate logs. **Data**: the token cache through
    the NAS, so the account needs no fresh consent. **Verified** by
    Home Assistant reading the mailbox through it. **Cut**: none in DNS
    — the name is new. **Rollback** points the clients back.
*   **spoolman**: its row, which Home Assistant's integration is pointed
    at in the window. **Data**: the legacy claim is on the shared
    JuiceFS volume, which migration.md §3 does not copy; its single file
    is streamed from the legacy pod's mount. **Verified** by Home
    Assistant reading a spool through the name, which the row answers
    from its land. **Cut**: `spool`'s legacy block, and its vhost row. **Rollback** loses the spool changes since the land.
*   **The Home Assistant backend**: the LAN-device relay (§4.1) behind
    the `haos` row. Home Assistant's trusted proxies gain the worker's
    address in the same window. **Verified** by a sign-in at the
    balancer showing the client's address in Home Assistant's log.
    **Cut**: `haos`.
*   **The doors**: the `StaticSite`'s NAS instance, its content copied
    once from the VPS's directory onto the NAS. Its rows name the
    website co-host zone, served since Wave A2, and `jiahui.love`, which
    is not, so the cluster's DNS-01 token is minted afresh at the head,
    scoped to `jiahui.love` too, into the land's branch. The labels that
    alias `jiahui.love`'s apex — `www`, `gift`, `ji`, `peifeng` — reach
    the Gateway with no route, as they reach the legacy ingress today.
    **Verified** at the balancer for each hostname. **Cut**: `games` and
    `game`, and `jiahui.love`'s apex.
*   **thread-dashboard** stays on the host, on the recommendation of
    §11.4: it needs multicast on the IoT bridge for its HomeKit names,
    which no pod has, and moving it would save 35 MB. Its vhost row
    moves onto the gateway's declared vhosts, as the UPS card's does.
*   **Monitoring's name.** `mon` becomes a row of the monitoring
    component (rfc-007 §7.4): Grafana behind it with its single sign-on
    client, and `k8s-base` publishing its record. A stack that calls
    `route` for a public row builds a Cloudflare provider and reads the
    `dns` stack's zone identifiers (rfc-007 §9.1), so at this wave's
    land `k8s-base` gains the zones token, whose slot names it as a
    target, and a StackReference to `dns` for the identifiers, the
    recorded use `apps` already makes. `dns`, `k8s-base` and `apps`
    share one passphrase, so no reader gains the token that does not
    hold it already (credentials.md §1). Grafana's values are sealed
    afresh at the head (§4.6). The alert read route rides `mon`, matched
    on method, path and header, and lands with the poller that reads it.
    **Verified** by a sign-in to Grafana through its client at the
    balancer. **Cut**: `mon`.

**The legacy monitoring keeps running until Wave C's head**, because it
is what alerts on the legacy applications that have not moved — immich,
jellyfin and syncthing-nas — and on the legacy node they run on. It
stops with them, and its claim is kept cold until its retention would
have aged out, rather than running read-only for a history no rule
reads. The legacy cluster's own conditions are alerted on by nothing
after that: they end with the legacy applications they watched, and the
arrangement is recorded in row R12 (sources-of-truth.md rule 4).

The same cut takes the gateway's resolver and console names out of
`LEGACY_VHOSTS`, whose rows retire at Wave B: their public names have
had rewrites since `dns`'s first up.

### 5.6 Wave C

**The head** removes Wave B's legacy applications and the legacy
JuiceFS stack, whose last consumer was spoolman, with their claims kept;
moves the legacy GPU plugin to the iGPU; and stops legacy immich,
jellyfin, syncthing-nas, the legacy monitoring and the legacy mail
relay, whose last senders those are (§3.5). **The window** then grows
the worker and binds the A380 in one drained apply, since each replaces
the domain (migration.md, Wave C).

*   **immich**: its server and its machine-learning service, each
    requesting the A380 through what `k8s-base` offers when the wave
    opens; its queue store a Redis of its own; its database by recovery
    (§4.3) on the `vchord` image, with its extensions created on recovery
    as legacy creates them; its configuration rendered as legacy renders
    it, its client from the census, its mail through exim; its queue
    watchdog ported. **Data**: the library stays in place; the database
    by recovery after a final backup. **Verified** by an upload from a
    phone on each side of the split, a search answered by the model, and
    a video transcoded on the A380. **Cut**: `photos`, and its LAN
    rewrite rule. **Rollback** loses the uploads since the cut, which
    stay in the library on the NAS but outside the restored database.
*   **jellyfin**, in a namespace of its own, with its claim on `Media`
    read-only. Discovery by multicast does not cross into a routed pool
    address, and no client depends on it: each television's saved
    address, at the host's name and port, is re-entered in the window as
    the row's name, and the host's discovery relay is retired. **Data**:
    the database and settings through the NAS, with any render node path
    they name checked against the worker's one GPU; scrub previews and
    metadata stay on the NAS. **Verified** by each television playing a
    title through the name, and a transcode on the A380. **Cut**: `tube`,
    and its vhost row.
*   **syncthing-nas**: the shared component in its NAS form, its
    interface on `sync-nas`, whose row lands published (§3.3). Its device identity
    lives in its configuration, which is copied whole, so its peers see
    the same device. **Data**: the configuration and the index through
    the NAS; the data stays in place. **Verified** by its peers
    connecting under its device identity. **Cut**: `sync-nas`'s legacy
    block.

The cut also ends the transitional target of the legacy forward-auth
checks, whose last gated front is `sync-nas` (§3.5). The VPS's last
legacy ingress name is cut here. Its cancellation is the
decommission's (§5.8).

### 5.7 Wave D

*   **qbittorrent** with **seedwatch**, in one namespace on the worker
    (workloads.md §4): `bt`, its peer port on the worker's own address
    (§4.1), and the forward declared by `physical`
    (`qbittorrentOnWorker`, migration.md, Wave D). seedwatch's image is
    built in `docker/`, and its access to qbittorrent's API is by the
    namespace's own address range rather than by running on the same
    host. Home Assistant's integration is pointed at `bt` in the window.
    **Data**: the profile through the NAS with UPnP turned off.
    **Verified** by peers connecting on both families, and seedwatch's
    links counted as before. **Cut**: `bt`, the `bt` and `seedwatch`
    vhost rows, and the UPS card's row, the one row of `LEGACY_VHOSTS` no
    application claims, which moves onto the gateway's declared vhosts as
    an appliance upstream (slice 26), so that the census empties with
    this wave (migration.md §2). The host units are stopped at the head
    and removed from aconfmgr and yadm after the soak (row R13).
    **Rollback** restarts the host units on the profile the stop kept.

### 5.8 What the decommission inherits

When Wave D's cut has soaked, every application serves from the new
cluster, and what the waves leave is the decommission's:

*   the legacy cluster stopped and uninstalled, its retained-volume
    census finished, and with it **the CI runners a neighboring project
    runs on the legacy cluster**, which no document of this repository
    accounts for and which need a home before the uninstall;
*   the VPS cancelled: no application is served from it after Wave C's
    cut, and hath has left its egress since §5.4; the records that
    still name it are the parked zones' web origin and its anchor;
*   with those records gone, nothing in `dns` points at the VPS, and the
    `Held` wrapper of §3.3 and its relation test are deleted;
*   the VPS's overlay roster entry removed;
*   the legacy Google Cloud Storage buckets and their ported
    credentials, read by nothing since the cuts;
*   the sealing key's rotation, which re-seals every ported ciphertext
    and deletes the legacy key (§4.6);
*   the worker's final size (§6.2), and with it the host memory left for
    what the operator runs beside the cluster.

### 5.9 The calendar

**Three durations, kept apart.** Construction and the bring-up cost
little while they run, because an idle cloud fleet is free and an idle
worker takes about 1.5 GiB of the host's memory. The bring-up needs
rfc-007's slices 9 to 11, and slices 3 and 4 here for its last stages;
the rest of construction runs beside it. The live phase is what
coexistence costs.

| Phase | Elapsed | What sets it |
| --- | --- | --- |
| **Construction**: every pre-live slice of §10 merged | about 2 weeks from acceptance | The shape slices are one line of about a day each; the builds then run three at a time |
| **Bring-up**: [physical/bring-up.md](../physical/bring-up.md), from the window to its gate | about 4 days, of which 3 need the operator | The window's day, one day of `physical` and `dns`, and the readings of the first `up` |
| **Live**: the first land to the end of Wave D | **9 days** | The soaks below, with the operator on 7 of them |
| **Live**: the first land to the VPS's retirement | **12 days** | hath's soak, which starts on day 5 |

**A soak is the time a wave runs on the new cluster before the next
step spends its way back.** Each is the shortest that covers what the
wave cannot show on its first day:

| Wave | Soak | What it watches | What a shorter one would miss |
| --- | --- | --- | --- |
| A1 | 1 day | The first nightly backup of authelia's claim; a DMARC aggregate report showing the relay's mail aligned; the canary through a night | The first backup, and any DKIM failure: receivers report daily |
| A2 | 3 days | The first base backup and WAL archive to the backup bucket; the reseed finished; the cloud pool under real load, as etcd's fsync with the applications' CPU limits; federation's backlog | A slow fault in the cloud pool, which hath would then land on, and the reseed itself |
| hath | 7 days | The network's quality score and the client's static ranges, which recover over days; the reserved address staying the egress | A demotion the network applies slowly. This soak gates nothing but the VPS's cancellation, and it runs beside Waves B to D |
| B | 1 day | The first nightly backups; Home Assistant's integrations through a day | The first backups, and an integration that only polls daily |
| C | 3 days | The first base backup of immich's database and a by-hand restore drill; the machine-learning queues; the A380 under real transcodes; the worker's memory at its peaks | The memory peak, which comes with a backlog of jobs and not at the cut; the VPS's way back goes when this ends |
| D | 2 days | Peers on both families; seedwatch's first full cycle, which has a day of grace built in | A reap that goes wrong after its grace period |

**The schedule these give**, with the operator present on the marked
days:

| Day | Step | Operator |
| --- | --- | --- |
| 0 | A1 land, verify, cut | yes |
| 1 | A2 land, verify, cut | yes |
| 4 | B land, verify, cut; hath's address in `physical`, and its stage; A2's removal | yes |
| 5 | hath land, cut; B's soak ends | yes |
| 6 | C's window, land, verify, cut | yes, the whole day |
| 7 | D land, verify, cut | yes |
| 9 | C's and D's soaks end: Wave D is done | yes, a reading |
| 12 | hath's soak ends: the VPS can be cancelled | a reading |

**What sets the lower bound**, and what compressing each buys:

*   **Soaks are most of it**, and the nightly backup is what sets their
    floor: one night cannot be skipped for any wave that holds data
    without flying blind on its restore. Taking A2 to one night saves 2
    days, and spends the time a slow fault would have surfaced: the next
    wave lands on a cluster with the fault in it. Taking C to one night
    saves nothing on Wave D's end, which D's own soak sets, and gives up
    C's way back through the VPS a day after the cut.
*   **Hath's soak sets the VPS's date.** Taking it to 4 days cancels the
    VPS on day 9, the same day as Wave D. A demotion that shows after
    that has no old address to return to, only a repair on the new one.
    It saves 3 days of a $30 bill, about $3; the choice is the operator's
    and cheap either way.
*   **Data copies are no bound.** Every claim is small, and the two
    large moves run ahead of their window or block nothing: hath's warm copy, and the reseed of the replica.
*   **Worker growth is one window**, Wave C's (§6.2), so it costs one
    drained apply, and that day is the one that needs the operator
    throughout.
*   **The operator's presence is the real constraint.** The schedule is
    seven days of presence inside ten, and a wave moves only when the
    operator can read its verification. If those are weekends alone, the
    same steps take about four weeks, and the soaks stop mattering.
*   **What runs in parallel already:** B beside hath, and D beside C's
    soak. D could land before C, since nothing joins them. Running A1
    and A2 together saves a day and removes the night between the
    service everything signs in through and everything that signs in.

--------------------------------------------------------------------------------

## 6. Memory

### 6.1 The budget, and the per-app sizing

The host has 31 GiB. Before the worker starts, on the reading of
2026-11-06, 12.3 GiB of it is available, with Home Assistant's machine
at 4.1, legacy k3s's server at 2.6 and its pods at 5.6. The worker boots
at 10 GiB and leaves about 2.3. What it can grow by after that is what
legacy releases, less a margin of 1 GiB.

**Two ceilings matter.**

*   **While the waves run**, the legacy server and what stays on it —
    its system pods and the idle CI runners of §5.8, about 3.6 GiB
    together — stay resident. At Wave C's window, with legacy's
    applications stopped, the worker can reach about 16 GiB, about 14.5
    of it allocatable to workloads. The runners are the assumption: one
    under load has taken up to 6 GiB, so this holds only while the
    neighboring project's dispatching stays paused, as the operator has
    ruled for the waves, or the runners are capped through Wave C's
    window and soak.
*   **After the decommission**, legacy releases the rest, and the worker
    could reach about 19 GiB, short of nodes.md §4.2's 20 GiB, which was
    written against the August measurement.

**The applications fit under the transitional ceiling.** Requests are
sized from what each runs at, and limits from its 30-day peak, as
workloads.md §1 asks; the legacy requests, immich's 4 GiB among them,
were not. The database of immich is on the VPS today, so its move adds to the
host, where the rest of the table replaces what legacy releases.

| What runs on the worker | Today, peak | Request | Limit |
| --- | --- | --- | --- |
| Monitoring, from M2 | | 0.7 GiB | |
| exim, and Wave B's applications | | 0.3 GiB | |
| immich's server | 1.0, 4.2 GiB | 2 GiB | 5 GiB |
| immich's machine learning | 0.1, 4.1 GiB | 1 GiB | 5 GiB |
| immich's database and Redis | 0.7 GiB | 1.1 GiB | 2 GiB |
| jellyfin | 0.4, 3.3 GiB | 0.5 GiB | 4 GiB |
| syncthing-nas, qbittorrent, seedwatch | 0.4 GiB | 0.4 GiB | |
| **Total** | **every peak at once: about 14 GiB** | **about 6 GiB** | |

The requests take less than half of the allocatable, and every peak at
once fits inside it. **So the per-app sizing does not trigger the
conditional ruling of §11.2**, on these numbers; Wave C's reading
decides it (§6.3).

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
of its own. **One window is enough.**

*   **Waves A and B** need none. A's applications run on the cloud pool,
    and B's requests fit the worker as it booted.
*   **Wave C's window** grows the worker from 10 GiB to about 16, for
    Wave C and Wave D together, and it is also the A380's bind, which
    replaces the domain anyway.
*   **Wave D** needs none: qbittorrent and seedwatch take 0.25 GiB.

### 6.3 What is verified before the window grows the worker

Each item is read and recorded on the wave's issue before the growth
commit merges:

1.  every application of Wave B has been cut and has soaked;
2.  each of their legacy workloads has been removed in kluster-code,
    not merely stopped (§3.5), so nothing can start it again;
3.  the host's available memory, read with the ZFS cache at its floor
    and the CI runners at the load they will carry through the wave,
    covers the growth with a margin;
4.  the worker's requests after Wave C's land, and its peaks, read
    against the allocatable the growth gives, leave the headroom §6.1
    assumes. Where they do not, the wave waits, and §11.2 is asked.

### 6.4 Rollback across a growth

A growth spends memory a legacy application needs to start again. **So
a rollback after the growth is the window reversed**: drain the worker,
shrink the domain by the commit's revert, start the legacy application,
then run §3.6's rollback. It costs a drained window, which restarts
every workload on the worker. Wave C's window stops legacy immich and
jellyfin and grows by what they free, and a rollback restarts them on
the iGPU, which kluster-code moved them to at the head, once the worker
has given the memory back. **New rule**, landing in migration.md §0.

--------------------------------------------------------------------------------

## 7. What is already conformant

*   **The rewrite derivation** in `dns`, which already answers a
    home-side row at its side's VIP; §4.1 changes the type it reads and
    not what it does.
*   **`conventions.backup`** has the retention classes, the bucket and
    the repository layout every backed claim and database uses, and
    `physical`'s `BackupBucket` already declares a writer scope per
    namespace and family.
*   **`conventions.NODE_VOLUMES`** declares both preserved volumes, and
    `physical` attaches them.
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
*   **From framework/ci.md §3, `up-apps` after `up-k8s-base` alone, and
    `up-k8s-base` beside `up-dns`.** Both follow `up-dns` (§3.4).
*   **From migration.md §0 rule 1, scaling the legacy application down
    by hand.** kluster-code's deploy would undo it (§3.5).
*   **From migration.md §0 rule 4, growing the worker by everything
    legacy frees, toward 20 GiB.** It grows to fit (§6.2), and nodes.md
    §4.2's 20 GiB is not reachable on the measured host (§6.1).
*   **From migration.md §2, exim in Wave B, and the legacy relay
    stopping with its successor's land.** The new relay moves in Wave
    A1, and the legacy one stops with its last sender (§3.5, §5.2).
*   **From migration.md §2, hath last of the applications, in a Wave E
    of its own.** It moves after Wave A2 (§5.4).
*   **From migration.md §2, the legacy Prometheus kept running
    read-only.** It runs until Wave C's head for the alerts, and is then
    kept cold (§5.5).
*   **From migration.md §3, rsync over SSH into the target claim.** A
    Talos node has no SSH server; the stream runs through the API server
    (§5.3).
*   **From migration.md §3, no copy for any shared-JuiceFS data.** True
    of the syncthing replica, false of spoolman's database (§5.5).
*   **From rfc-003 §6, `Exposure` as one field of one enumeration.** Its
    argument against independent flags stands, and a composed type meets
    it better: a row that reaches nobody has no value at all (§4.1).
*   **From rfc-007 §5.3 and §4.4, the census's `Answerer`, and each
    `lan` VIP held by one Service.** The Gateways' ports leave the census,
    and a raw LAN port shares its side's VIP (§4.1).
*   **From cluster-infra.md §2, every listed application behind the gate
    on every side.** The gate is per side (§4.1).
*   **From workloads.md §1 and §5, the discovery server as a raw port on
    8443.** Its clients are on 443 (§4.1).
*   **From workloads.md §1, architecture.md §3.6, and gateway.md §4.2,
    jellyfin as `IOT`, the televisions on the IoT VLAN, and rule 1 open to
    that VLAN.** They are on the Default LAN, jellyfin is public too, as
    it is today, and rule 1 narrows to Home Assistant (§4.1, §4.7).
*   **From workloads.md §2, thumbnails on `local-path`, and hath's client
    state under VolSync.** The thumbnails are too large and stay with the
    library; hath's state is 13 KB beside its cache (§4.2).
*   **From rfc-007 §3.2, a consumer of a sealed value merging after its
    value exists, and from style/pulumi.md, absence stopping the run at
    the read, for the length of the bring-up.** It merges ahead of the
    cluster and waits for the value (§10).
*   **From workloads.md §4, the LAN-device backend as a selectorless
    Service.** A cloud node's Envoy cannot reach the endpoint; a relay on
    the worker does (§4.1).
*   **From the legacy immich database, two instances.** One (§4.3).
*   **From the legacy charts, the immich and Redis releases.** The
    components declare their workloads (§5).
*   **From the legacy egress pods, hath's WireGuard gateway.** hath
    leaves by its node's address (§5.4).
*   **From the legacy LAN load balancers, every raw port but one.** §4.7
    lists them.
*   **From rfc-007 §6.2, the strict scope of every value
    `credentials` seals.** The writer keys are sealed namespace-wide, one ciphertext
    per namespace for every repository Secret in it (§4.6).
*   **From credentials.md §4, "a new volume will need no `credentials`
    run".** True of a volume in a namespace that already backs up; a
    namespace that starts backing up needs `physical`'s scope and a seal
    (§4.6).
*   **From rfc-007 §6.3, the legacy key's channel left open.** It is a
    one-off carry with a register row, retired at the decommission
    (§4.6).
*   **From architecture.md §3.2, the dedicated VIP on a secondary
    private address.** It is the node's primary private address (§5.4).
*   **From rfc-007 slice 5's test, `apps` reading nothing across a
    StackReference.** It reads the `internet` pool's members (§4.1).

--------------------------------------------------------------------------------

## 9. The documents this content lands in

| Document | What lands there |
| --- | --- |
| [cluster/migration.md](../cluster/migration.md) | migration.md §0 rule 1: build, land, verify, cut, the stop and removal in kluster-code, and the legacy services that outlive their successor's land, from §3. migration.md §0 rule 4: growing to fit, the windows, the reading before each growth and the rollback across one, from §6. migration.md §1: the legacy key's carry and the writer keys' seal, from §4.6. migration.md §2: exim at Wave A1's head, hath after Wave A2, one window at Wave C, the legacy monitoring until Wave C's head, from §5. migration.md §3: the stream through the API server, recovery from the legacy store, spoolman's copy, from §4.3 and §5. migration.md §4: what the decommission inherits, from §5.8 |
| [cluster/nodes.md](../cluster/nodes.md) | nodes.md §4.2: the transitional and end-state ceilings, and the end state as the decommission's decision, grown to fit, from §6 |
| [declarative/workloads.md](../declarative/workloads.md) | workloads.md §1: a component receives its rows and merges unwired, one `Port` shape, the gate per side, the discovery server, from §3.1 and §4.1. workloads.md §2: the three kinds of claim, and the thumbnails, from §4.2. workloads.md §3: the namespace's sealed writer key in each repository Secret, from §4.2 and §4.6. workloads.md §4: the dedicated-VIP pattern on the node's primary address, one database instance with recovery and the drill, the static site's apex, the single sign-on census, the LAN-device relay, from §4 and §5.4. workloads.md §5: a ported ciphertext keeps its namespace, from §4.6 |
| [declarative/dns.md](../declarative/dns.md) | dns.md §1: the order of the stacks' applies, and the reference `k8s-base` makes, from §3.4 and §5.5. dns.md §2: an apex row's record is the web origin, from §4.1 and §4.5. dns.md §5: the composed declaration, `Held`, bare names, ports' rewrites, from §3.3 and §4.1. dns.md §6: the cut's pull request and the land's, from §3.2 |
| [declarative/README.md](../declarative/README.md) | §2: the StackReferences `apps` makes for the pool's members and `k8s-base` makes for the zone identifiers, from §4.1 and §5.5 |
| [framework/ci.md](../framework/ci.md) | ci.md §3: `up-k8s-base` and `up-apps` after `up-dns`, from §3.4 |
| [sources-of-truth.md](../sources-of-truth.md) | sources-of-truth.md §3: a component that has not migrated is not wired, from §3.1. Note N4 replaced by its answer, from §3.2 and §3.4. Rows R12 and R13: each flip is a cut, and its pull request edits the row; R12's arrangements, from §5.5 and §5.6 |
| [cluster/architecture.md](../cluster/architecture.md) | architecture.md §3.2 and §3.5: hath's address and egress on its node's primary private address, from §5.4. architecture.md §3.4: the IoT VLAN's reach, from §4.7. architecture.md §3.6: the LAN raw-port row on its side's VIP, and jellyfin's row, from §4.1. architecture.md §6.7: where a TLS passthrough stands against the refusal of Envoy for raw traffic, from §4.1 |
| [declarative/physical.md](../declarative/physical.md) | physical.md §1: the reserved address on the node's primary private address, and no secondary address, from §5.4 |
| [declarative/cluster-infra.md](../declarative/cluster-infra.md) | cluster-infra.md §1.1: the writer keys' channel, from §4.6. cluster-infra.md §2: the canary as built with authelia, the apex listeners, from §4.1 and §5.2. cluster-infra.md §3: the `mon` route and the Cloudflare provider of `k8s-base`, from §5.5 |
| [credentials.md](../credentials.md) | credentials.md §3: the writer keys' sealed delivery, the zones token's `k8s-base` target, the legacy sealing key's row, from §4.6 and §5.5. credentials.md §4: the seal of the writer keys from `physical`'s state, and the sentence on a new volume, from §4.6 |
| [cluster/storage.md](../cluster/storage.md) | storage.md §2: the three kinds of claim, from §4.2. storage.md §5: one database instance, its server name's generation, and the restore drill in place, from §4.3 |
| [operations.md](../operations.md) | operations.md §4: the database restore drill and the authentication canary built, from §4.3 and §5.2 |
| [physical/gateway.md](../physical/gateway.md) | gateway.md §1: a declared vhost's appliance upstream, and what answers the UPS card's name on the LAN, from §5.7. gateway.md §4.2: rule 1's source narrowed to Home Assistant, and the peer rows read the census, from §4.1 and §4.7 |
| [cluster/security-audit.md](../cluster/security-audit.md) | The restic password in clear in each repository SealedSecret and in the etcd snapshots, with its actor and why it opens nothing, from §4.2 |
| [style/pulumi.md](../style/pulumi.md) | "Data": the composed declaration in place of `Exposure` as its example, from §4.1. "Layering": the StackReference uses `apps` and `k8s-base` gain, from §4.1 and §5.5 |
| [rfc-003](rfc-003-dns-and-github-stacks.md) | The status header: `Exposure` and the route row replaced by the composed declaration, from §4.1 |
| [rfc-007](rfc-007-k8s-base-and-apps.md) | rfc-007 §3.2, §14's slices 9 to 12 and the status header: the order of the sealed values, and the cut of slice 12, from §10. rfc-007 §4.4 and §5.3 in the same header: the VIP's sharing and the census's shape, from §4.1. Those edits ship with their slices, not in this document's pull request |

--------------------------------------------------------------------------------

## 10. How we get there

**Pre-live** means built and tested against Pulumi's mocks and fakes,
with nothing live; such a slice merges as soon as it is ready, and
changes nothing deployed (§3.1). **Live** means the slice needs the
running cluster, a sealed value, a window or an operator, or its merge
changes what is deployed.

Each slice's **After** line names the slices it depends on and every
slice it shares an owned path with, so two slices whose lines do not
reach each other can run at once. The shape slices 3 to 9 form one
line, because each adds a census, its invariants or a section of
workloads.md. The build slices, 10 to 23, own their own package and test
module and nothing shared unless their line says so, and run together
once their shapes have merged. `docs/framework/ci.md` is serialized, and
slice 1 is the one slice here that touches it. A slice of rfc-007 still
open counts too: where one shares an owned path with a slice here, the
line names it.

**What of rfc-007 runs ahead of this list.** Slices 9, 10 and 11 merge
pre-live, ahead of the window, with their sealed values arriving later
as one commit to the stack file: physical/bring-up.md says how, and it
is what stops coexistence time being spent waiting on code. Each is
a detail move rfc.md §5.2 provides for, nothing having been built from
them yet: the pull request that builds the first of them edits rfc-007
§3.2 and §14 in place, and the status header gains a dated `Updated:`
line.

**Until rfc-007 slice 13's gate has passed, an absent sealed value warns
at the run and declares no SealedSecret.** It is a dated exception to
style/pulumi.md's rule that absence stops the run at the read. **New
rule**, landing in physical/bring-up.md, where it retires. rfc-007 slice
9 builds it, with a test of the absent path, and the last step of slice
13 deletes it, with a test of the refusal that replaces it.

**Slice 12 is cut.** Its application base — the namespace at `restricted`,
the route label, the default-deny policy and the admission of the
Gateways — depends on nothing below and can start now. `route` and the
census's rows wait for slices 3 and 4 here, since they are the
declaration of §4.1, and the echo server waits for those. The pull
request that builds the base records the cut the same way, editing
slice 12's entry in rfc-007 §14 in place.

**The node volumes' sizes land before anything else**, by an issue of
their own, before the first `physical` apply (§4.2).

### 10.1 Pre-live: the mechanisms and the shapes

**Slice 1: the deploy order, and the migration plan.** Pre-live.
**After** rfc-007 slice 11, which owns `docs/cluster/migration.md` too.

*   **Done means**: `up-k8s-base` and `up-apps` each need `up-dns` and
    run whatever its result, `up-k8s-base` when the `physical` gate
    passed and `up-apps` when `up-k8s-base` succeeded; ci.md §3 says so,
    with the reason (§3.4); dns.md §1 states the order; migration.md and
    nodes.md §4.2 state this design, as §9 lists for them.
*   **Owned paths**: `.github/workflows/deploy.yml`,
    `docs/framework/ci.md` (serialized), `tests/test_workflow_shape.py`,
    `docs/cluster/migration.md`, `docs/cluster/nodes.md`,
    `docs/declarative/dns.md` (§1).
*   **Tests that fail without it**: both jobs name `up-dns` among their
    needs (drop it from one); each job's condition, evaluated over every
    result its needs can take, runs it exactly when its own gate passed,
    whatever `up-dns`'s result (require `up-dns` to succeed).

**Slice 2: the image pins.** Pre-live. **After** rfc-007 slice 12's echo
server, if it is open, which pins its image in the same file.

*   **Done means**: every image the waves' components run, the copy
    pair's included, is a `versions:image-<name>` pin in `Pulumi.yaml`,
    at the version §5 states, and Renovate opens a pull request for each
    one's next release under `renovate.json5`'s rules. The two images
    built in `docker/` are their slices' to pin.
*   **Owned paths**: `Pulumi.yaml`, `renovate.json5`,
    `tests/test_versions.py`, `tests/test_images.py`.
*   **Tests that fail without it**: each new pin parses (write one that
    does not parse); the existing renovate fixture tests, with a pin of
    each new image's scheme (drop the rule a scheme needs).
*   **Unproven live**: that each cloud-pool image runs on arm64, which
    the slice reads from each pinned manifest's platforms.

**Slice 3: the exposure declaration.** Pre-live only ahead of the first
full `physical` apply: its row removal and rule 1's narrower source change
`physical`'s plan, so after that apply its merge waits at that stack's
gate, and the chain with it, once. **After** slice 1, the application
base of rfc-007 slice 12, and Aetf/kluster#661, which owns `SiteFirewall`
too. The resolvers' declared list, Aetf/kluster#454, is held for R2's
flip and rebases onto this slice.

*   **Done means**: `conventions.routes` holds §4.1's types and `Held`,
    `Exposure`, `Extra`, `Srv` and `SELF` are gone, `conventions.cluster`
    holds `Port`, its four fronts and `GATEWAY_PORTS`, and `Answerer` and
    `QBITTORRENT_PEER_PORT` are gone; every reader derives what it did
    from the new rows: `dns`'s rewrites, `served_zones`, the balancer's
    listeners, the node firewall's openings, the Gateways' listeners and
    `SiteFirewall`'s peer and IoT rows, whose rule 1 takes Home Assistant's
    address as its source; dns.md §2, §5 and §6, sources-of-truth.md §3
    and note N4, `legacy.py`'s module docstring, gateway.md §4.2,
    architecture.md §3.4, style/pulumi.md ("Data"), and rfc-003's status
    header are true.
*   **Owned paths**: `src/kluster/conventions/routes.py`,
    `src/kluster/conventions/cluster.py`,
    `src/kluster/conventions/__init__.py`,
    `src/kluster/components/dns/rewrites.py`,
    `src/kluster/components/dns/legacy.py` (its docstring),
    `src/kluster/components/cloud/nodes.py`,
    `src/kluster/components/talos/__init__.py`,
    `src/kluster/components/gateway/unifi.py`,
    `src/kluster/components/cilium/`,
    `tests/test_conventions.py`, `tests/test_dns_rewrites.py`,
    `tests/test_dns_records.py`, `tests/test_dns_stack.py`,
    `tests/test_sealing.py`, `tests/test_cloud_nodes.py`,
    `tests/test_talos_config.py`, `tests/test_unifi.py`,
    `tests/test_front_door.py`,
    `docs/declarative/dns.md`, `docs/declarative/physical.md`,
    `docs/declarative/cluster-infra.md`, `docs/sources-of-truth.md`,
    `docs/physical/gateway.md`, `docs/cluster/architecture.md`,
    `docs/cluster/security-audit.md`, `docs/style/pulumi.md`,
    `docs/rfc/rfc-003-dns-and-github-stacks.md`.
*   **Tests that fail without it**: rows of the test's own, one of each
    shape, derive §4.1's table's consequences, gateway by gateway, against
    literals (derive the rewrite from the wrong side; add a certificate
    zone for a `Passthrough`); an empty reason, an empty zone set and `@`
    as a host are refused (accept one); a row is held exactly while a record
    pointing at the VPS holds its name, against a census broken each way,
    and a home-only row is never held (keep the wrapper after deleting
    the record; hold a home-only row); a direct row resolves
    through no proxied record (a direct row aliasing a proxied one); the
    route-side co-host check; a `Vip(IOT, …)` port opens the allow for
    its port alone, from Home Assistant's address alone (open the VIP;
    name the VLAN).

**Slice 4: exposure.** Pre-live. **After** slice 3, and rfc-007 slices 9,
11 and 12's base, which own its Gateways, its documents and its stack
program before it.

*   **Done means**: `route`, emitting one `HTTPRoute` per side with its
    gate, and its redirect for a bare name; the `TLSRoute` for a
    `Passthrough`; `port`, for every front, with `apps` reading the
    `internet` pool's members across a StackReference and handing them
    in; `expose(rows)` on the application base; the LAN-device relay;
    the Gateways' apex and passthrough listeners; `media-gw`'s sharing
    annotations and the worker's address pool; workloads.md §1 and its
    §4 relay, dns.md §5, architecture.md §3.6 and §6.7,
    cluster-infra.md §2, and style/pulumi.md and declarative/README.md
    §2 for the reference `apps` makes, are true.
*   **Owned paths**: `src/kluster/components/apps/__init__.py`,
    `src/kluster/lib/k8s.py`, `src/kluster/components/cilium/`,
    `src/kluster/conventions/outputs.py`,
    `src/kluster/stacks/apps.py`, `src/kluster/stacks/k8s_base.py`,
    `src/kluster/stacks/physical.py`, `tests/test_physical_stack.py`,
    `tests/test_apps.py`, `tests/test_k8s.py`,
    `tests/test_front_door.py`, `tests/test_cilium.py`,
    `tests/test_stack_programs.py`, `docs/declarative/workloads.md`,
    `docs/declarative/dns.md`, `docs/cluster/architecture.md`,
    `docs/declarative/cluster-infra.md`, `docs/style/pulumi.md`,
    `docs/declarative/README.md`.
*   **Tests that fail without it**: `route` attaches each side's gateway
    with that side's gate alone (gate both sides from one); a `Port`
    emits the Service alone, under `Cluster`, with no node ports and its
    front's addresses from what it is handed (state `Local`; allocate
    node ports; hand a balancer row the dedicated node's address); an
    output the pool reads that is not an address stops the run naming it
    (read a node address as the unknown sentinel); a raw LAN port shares
    its VIP with its side's Gateway under one sharing key (drop the key
    from one); each form of the gate asks Authelia for its own
    authorization (ask for the session on the Basic form); the relay's
    Service selects the relay, which is scheduled only to the worker (a
    selectorless Service); an apex listener exists exactly for each zone
    an `Apex` names, and a passthrough listener for each `Passthrough`
    (derive from every served zone).
*   **Unproven live**: the passthrough listener beside the wildcard on
    443, with the raw port as its fallback (§4.1).

**Slice 5: storage.** Pre-live. **After** slice 4.

*   **Done means**: `backed_pvc`, `scratch` and `fixed_claim` with its
    two sources, and the rule that a pod writes to the NAS as the
    export's anonymous user, on the application base; workloads.md §2 and §3,
    storage.md §2 and the security audit's residual are true.
*   **Owned paths**: `src/kluster/components/apps/storage.py` (new),
    `tests/test_apps_storage.py` (new),
    `docs/declarative/workloads.md`, `docs/cluster/storage.md`,
    `docs/cluster/security-audit.md`.
*   **Tests that fail without it**: a backed claim's source schedules
    by its class and writes to `volsync_repo_path` of its namespace and
    name (hard-code a cron line; name another path); its repository
    Secret carries the namespace's sealed writer key under the claim's
    own name, with a password generated in state (seal a value per
    claim; write the password into configuration); an `Nfs` source is
    scheduled only to the worker (drop the affinity); a `NodeVolume`
    source's path is inside the mount and never the mount point (point
    at the mount); `scratch` refuses no reason (an empty reason).

**Slice 6: the writer keys and the ported ciphertexts.** Live: its merge
leads to the gated `physical` apply that mints the keys, so it merges
when the operator is ready to approve that apply. **After** slice 5.

*   **Done means**: `conventions.backup.BACKED_NAMESPACES` lists every
    namespace the waves back up, and `physical` declares its scopes from
    it; `credentials` seals each namespace's writer keys from
    `physical`'s state, namespace-wide, into `apps`'s configuration, at
    paths its rows in `conventions.sealed` derive; every legacy
    ciphertext the waves' components declare is copied into
    `Pulumi.apps.yaml`; credentials.md §3 and §4, cluster-infra.md §1.1
    and workloads.md §5 are true.
*   **Owned paths**: `src/kluster/conventions/backup.py`,
    `src/kluster/conventions/sealed.py`,
    `src/kluster/stacks/physical.py`,
    `src/kluster/scripts/credentials/`, `Pulumi.apps.yaml`,
    `tests/test_conventions.py`, `tests/test_sealing.py`,
    `tests/test_slots.py`, `tests/test_physical_stack.py`,
    `tests/test_backup_bucket.py`, `docs/credentials.md`,
    `docs/declarative/cluster-infra.md`,
    `docs/declarative/workloads.md`.
*   **Tests that fail without it**: `physical` declares exactly the
    scopes the census names, beside the etcd snapshots' scope (derive from a fixed
    list); a sealed key opens under any name in its namespace and in no
    other namespace (seal strict); the seal reads `physical`'s state
    under that stack's passphrase and writes a plain value at its row's
    path, through the `pulumi` runner the package fakes (write it as a
    configuration secret); every backed namespace has a row (drop one).
*   **Unproven live**: the keys' mint, by `physical`'s apply, and their
    seal, at the bring-up's last stage.

**Slice 7: single sign-on.** Pre-live. **After** slice 6.

*   **Done means**: `conventions.sso.CLIENTS` holds every client the
    legacy authentication service carries, each under the host it keeps
    across its move; workloads.md §4 is true.
*   **Owned paths**: `src/kluster/conventions/sso.py` (new),
    `src/kluster/conventions/__init__.py`, `tests/test_conventions.py`,
    `docs/declarative/workloads.md`.
*   **Tests that fail without it**: every client's host is a name a
    route row publishes or a record points at the VPS, in a zone the row
    or the record names (a client under a name nobody publishes);
    clients are unique by name (a census invariant).

**Slice 8: databases.** Pre-live. **After** slice 7, and rfc-007 slice
11, which owns `docs/operations.md` too.

*   **Done means**: the database shape of §4.3 on the application base:
    a single instance, the barman plugin to the backup bucket under the
    namespace's sealed writer key, scheduled by its class, a server name
    with its generation, an optional recovery from a named object store
    with its credential, and the restore drill with its rule;
    workloads.md §4, storage.md §5 and operations.md §4 are true.
*   **Owned paths**: `src/kluster/components/apps/database.py` (new),
    `tests/test_apps_database.py` (new),
    `docs/declarative/workloads.md`, `docs/cluster/storage.md`,
    `docs/operations.md`.
*   **Tests that fail without it**: the cluster archives to
    `barman_repo_path` of its namespace and its server name with its
    generation (drop the generation); a recovering cluster bootstraps
    from the store it is handed and archives to the bucket (archive back
    to the legacy store); the drill's role reaches only its namespace,
    and its rule carries a tier and a playbook that resolves (grant it
    cluster-wide; name a missing section).

**Slice 9: static sites.** Pre-live. **After** slice 8.

*   **Done means**: `StaticSite` with both sources, the delegation
    files' header on the apex route; workloads.md §4's static site,
    with its apex, is true.
*   **Owned paths**: `src/kluster/components/apps/static_site.py`
    (new), `tests/test_apps_static_site.py` (new),
    `docs/declarative/workloads.md`.
*   **Tests that fail without it**: a git-source site syncs its branch
    into an empty directory and runs more than one replica, spread
    across the cloud nodes (one replica; no spread); a NAS-source site
    mounts read-only and runs on the worker (mount read-write); the
    header is set on the delegation paths alone (set it site-wide).

### 10.2 Pre-live: the applications

Each build slice owns `src/kluster/components/apps/<application>/` and
`tests/test_apps_<application>.py`, both new, and the shared paths its
line adds; **done means** the component declares what §5 says its
application declares, the gate passes, and the component is not wired.
Its tests hold the relations §5 names: the claims backed or exempted
with their reason, the rows it receives attached to what their exposure
and their gate say, its ported values read from the configuration, its
pod security `restricted` unless its line states otherwise. Each pull
request carries an unproven-live note naming its wave's verification.

| Slice | Application | Wave | Size | Shared paths beyond its own | After |
| --- | --- | --- | --- | --- | --- |
| 10 | exim | A1 | S | none | 2, 4, 6 |
| 11 | authelia, with the canary and its target | A1 | M | `docs/declarative/cluster-infra.md`, `docs/operations.md` | 2, 5, 6, 7, 8; rfc-007 slice 11 |
| 12 | blog | A2 | S | none | 2, 9 |
| 13 | splitpro | A2 | M | none | 2, 7, 8 |
| 14 | matrix | A2 | S | none | 2, 5, 7 |
| 15 | syncthing, both forms, and the WebDAV server, whose own files need `backed_pvc` | A2, C | M | none | 2, 4, 5 |
| 16 | stdiscosrv | A2 | S | none | 2, 4 |
| 17 | hath, with its stage and copy pair | after A2 | M | none | 2, 4, 5 |
| 18 | golinks, emailproxy and spoolman | B | M | none | 2, 4, 5, 6 |
| 19 | the Home Assistant relay, and the doors | B | S | none | 2, 4, 9 |
| 20 | thread-dashboard, and its image in `docker/`, only if §11.4 moves it | B | S | `Pulumi.yaml`, `docker/` | 2, 4 |
| 21 | immich | C | L | none | 2, 4, 5, 7, 8 |
| 22 | jellyfin, and shoko only if §11.4 keeps it | C | M | none | 2, 4, 5, 7 |
| 23 | qbittorrent and seedwatch, and seedwatch's image in `docker/` | D | M | `Pulumi.yaml`, `docker/` | 2, 4, 5, 20 |

**Slice 24: monitoring's route roll.** Pre-live. **After** slices 3, 4
and 7, and rfc-007 slice 11.

*   **Done means**: the monitoring component takes a required roll of
    route rows and an optional single sign-on client; it builds no
    Cloudflare provider while it routes no public row, and configures no
    sign-on while it is handed no client. `k8s-base` hands it an empty
    roll and no client. The `mon` row, Grafana's client and its values,
    the provider, the zones token and the reference to `dns` arrive
    together with Wave B's land (slice 37).
*   **Owned paths**: `src/kluster/components/monitoring/`,
    `src/kluster/stacks/k8s_base.py`, `tests/test_monitoring.py`,
    `tests/test_stack_programs.py`.
*   **Tests that fail without it**: the component's route attaches each
    row it is handed (hard-code a host); handed no public row, it builds
    no Cloudflare provider and reads no zones token (build one
    regardless); handed no client, it configures no sign-on and reads no
    sealed value for one (configure one regardless).

**Slice 25: hath's address in `physical`.** Live: its merge is the gated
`physical` apply that moves the dedicated node's public address, made at
hath's head, before slice 34. **After** slices 4, 6, 8, 9, 11 and 24.

*   **Done means**: the reserved address is assigned to the dedicated
    node's primary private address; the secondary address, its pool
    member, its outputs and the machine configuration's secondary
    address are gone, with the mechanism that carried it; dns.md §2's
    anchor reads the address it now names; architecture.md §3.2 and
    §3.5, physical.md §1, cluster-infra.md §1 and §2, workloads.md §4
    and operations.md are true.
*   **Owned paths**: `src/kluster/components/cloud/nodes.py`,
    `src/kluster/conventions/outputs.py`,
    `src/kluster/conventions/cloud.py`, `src/kluster/stacks/physical.py`,
    `src/kluster/stacks/k8s_base.py`, `src/kluster/components/cilium/`,
    `src/kluster/components/talos/__init__.py`,
    `src/kluster/components/dns/base.py`, `tests/test_cloud_nodes.py`,
    `tests/test_physical_stack.py`, `tests/test_cilium.py`,
    `tests/test_stack_programs.py`, `tests/k8s_base_installation.py`,
    `tests/test_talos_config.py`, `tests/test_talos_day1.py`,
    `tests/test_talos_validate.py`, `tests/test_dns_stack.py`,
    `docs/cluster/architecture.md`, `docs/declarative/physical.md`,
    `docs/declarative/dns.md`, `docs/declarative/cluster-infra.md`,
    `docs/declarative/workloads.md`, `docs/operations.md`.
*   **Tests that fail without it**: the reserved address names the
    dedicated node's primary private address (name a secondary one); the
    pool holds no address a node does not carry (keep the secondary
    member); every address a node's machine configuration states is one
    its network interface carries (keep the secondary one).
*   **Unproven live**: whether the provider releases the node's
    ephemeral address in place (§5.4); the egress address hath's
    outbound connections arrive from.

**Slice 26: the gateway's declared vhosts.** Pre-live: nothing declared
uses what it adds, so `physical`'s plan is unchanged by its merge.
**After** slice 3, which owns `docs/physical/gateway.md` too.

*   **Done means**: a declared vhost may carry an appliance upstream, the
    form a `LEGACY_VHOSTS` row gives a LAN appliance with a certificate
    of its own, and a bare label if §11.4 keeps one on the gateway's own
    names; physical/gateway.md §1 says so, and names what answers the UPS
    card's and, if §11.4 keeps it on the host, thread-dashboard's names on
    the LAN once their rows move.
*   **Owned paths**: `src/kluster/components/gateway/` (its proxy's
    template), `src/kluster/conventions/gateway.py`,
    `tests/test_gateway.py`, `docs/physical/gateway.md`.
*   **Tests that fail without it**: an appliance upstream renders as a
    legacy row's does (render it as a container service's); where a bare
    label is kept, it renders the redirect to its full name (render
    none).

### 10.3 Live: the windows

Each window is a sequence of slices, in the order below within it;
Wave B and hath overlap, and §5.9 gives the days. Each pull request is
its own slice, and so is each step with none. A removal waits for its
wave's soak (§3.5), so a head carries only the removals whose soak has
ended, and slice 45 carries the rest.

| Slice | Window | What | Owned paths |
| --- | --- | --- | --- |
| 27 | A1 | The head: physical/bring-up.md's last stage has run; kluster-code stops authelia and points its forward-auth checks at the new service | kluster-code |
| 28 | A1 | **Land**: exim and authelia wired, `auth` held; the echo component, its row, its pin and its tests deleted | `src/kluster/stacks/apps.py`, `src/kluster/conventions/routes.py`, `src/kluster/conventions/cluster.py`, `docs/sources-of-truth.md`, the echo component's module, `tests/test_apps.py`, `Pulumi.yaml` |
| 29 | A1 | **Cut**: `auth` | `src/kluster/conventions/routes.py`, `src/kluster/components/dns/legacy.py`, `docs/sources-of-truth.md` |
| 30 | A2 | The head: A1's legacy authelia removed; the DNS-01 token minted for the co-host zone into the land's branch; kluster-code stops A2's applications | kluster-code |
| 31 | A2 | **Land**: blog, splitpro, matrix, syncthing, stdiscosrv; the discovery server's raw port leaves the census, its apply waiting at `physical`'s gate | as slice 28, and `Pulumi.k8s-base.yaml` |
| 32 | A2 | **Cut**: `split`, `matrix` with its SRV, `dav`, `syncapi`, the web origin's served apex and both `www`; `btsync` and `sync`'s legacy block deleted | as slice 29, and `src/kluster/components/dns/base.py` |
| 33 | after A2 | The head and the **stage**: A2's legacy applications removed; slice 25 applied; hath wired with its client not started, and the copy pair; the warm copy | as slice 28; kluster-code |
| 34 | after A2 | **Land**: kluster-code stops legacy hath; the final delta; the client started | as slice 28; kluster-code |
| 35 | after A2 | **Cut**: the re-registration; the copy pair and the not-started parameter deleted | as slice 28, and `src/kluster/components/apps/hath/`, `tests/test_apps_hath.py` |
| 36 | B | The head: the doors' content; the DNS-01 token minted for `jiahui.love` and Grafana's values sealed afresh, both into the land's branch; kluster-code stops Wave B's applications | kluster-code |
| 37 | B | **Land**: Wave B's applications, the `imap` row's IoT allow waiting at `physical`'s gate; `k8s-base` hands the monitoring component the held `mon` row and Grafana's client, and builds the Cloudflare provider and the reference to `dns` | as slice 28, and `src/kluster/stacks/k8s_base.py`, `src/kluster/scripts/credentials/slots.py`, `Pulumi.k8s-base.yaml`, `tests/test_stack_programs.py`, `tests/test_slots.py`, `docs/credentials.md`, `docs/declarative/cluster-infra.md`, `docs/style/pulumi.md`, `docs/declarative/README.md`, `docs/declarative/dns.md` (§1) |
| 38 | B | **Cut**: `spool`, `haos`, `mon`, `games`, `game`, `jiahui.love`'s apex, and the B rows of `LEGACY_VHOSTS` | as slice 32, and `src/kluster/conventions/gateway.py` |
| 39 | C | The head: the library re-owned on the host; Wave B's legacy applications and the JuiceFS stack removed; shoko stopped and archived, on §11.4's answer; the legacy GPU plugin onto the iGPU; kluster-code stops legacy immich, jellyfin, syncthing-nas, the legacy monitoring and the legacy relay; then the window: growth and the A380's bind | `src/kluster/conventions/homelab.py`, `src/kluster/components/homelab/`, `tests/test_homelab.py`, `docs/physical/homelab-host.md`; aconfmgr; yadm; kluster-code |
| 40 | C | **Land**: immich, jellyfin, syncthing-nas | as slice 28 |
| 41 | C | **Cut**: `photos`, `tube`, `sync-nas`, their rewrite rules, and the `tube` vhost row; kluster-code drops the transitional target of the legacy forward-auth checks | as slice 38; kluster-code |
| 42 | D | The head: the host units stopped | aconfmgr; yadm |
| 43 | D | **Land**: qbittorrent and seedwatch, and `qbittorrentOnWorker`, whose apply waits at `physical`'s gate | as slice 28, and `Pulumi.physical.yaml` |
| 44 | D | **Cut**: `bt`, the `bt` and `seedwatch` vhost rows, and the UPS card's row onto the gateway's declared vhosts | as slice 38 |
| 45 | D | After the soaks: legacy hath and Wave C's legacy applications removed, the host units removed from aconfmgr and yadm, and this document's status flipped to **Implemented**, naming where its content lives | `docs/rfc/rfc-008-workload-migration.md`, `docs/rfc/README.md`; aconfmgr; yadm; kluster-code |

A land's and a cut's owned paths also take the resolvers' declared rule
list, where it names a moved name (sources-of-truth.md, row R2).

**A head's done means** is each of its steps done, with its reading
recorded on the slice's issue; **a land's** is its verification of §5
transcribed there; **a cut's** is its names answering from the cluster,
the chain re-run to success where §3.4 asks for it, and the checks §5
runs after the cut passing. Each land and cut
edits the rows of sources-of-truth.md its applications' flips change.

--------------------------------------------------------------------------------

## 11. Open questions

### 11.1 Ruled: hath after Wave A2

The operator ruled on 2026-11-05 that hath moves directly after Wave A2
has soaked (§5.4), rather than last of the applications. The VPS then
retires once its last legacy ingress name is cut, in Wave C, rather than
after hath's soak. What decided it: hath is cloud-side and depends on
nothing Waves B to D move, and the address it needs is one that only
hath exercises, so the waves between them would not have proven it.

### 11.2 Conditional: the worker's size

The worker's size, against what the host keeps for the operator's own
work, is to be ruled only if the per-app sizing does not fit. On §6.1's
numbers it fits, with every peak at once inside the transitional
allocatable, so nothing is asked of the operator now; the end-state size
is the decommission's decision (§5.8). The question is asked if Wave C's
reading (§6.3, item 4) departs from §6.1, and the choice then is the RAM
upgrade, nodes.md §4.2's relief valve, or tighter limits for the rest of
the transition.

### 11.3 Settled on first contact

*   Each application's memory requests where the legacy component
    states none, and its limits, from the first week's readings.
*   Whether jellyfin's or immich's configuration names a render node
    path, read when its claim is copied (§5.6).
*   The upstream rate of the home line, which sizes the reseed and hath's
    warm copy.
*   The rewrite rules the resolvers' declared list carries for names
    the waves move, read off the list when its own declaration lands.
*   Whether a printer talks to spoolman at all, which the printer's own
    configuration says and no log shows; on the IoT VLAN, it reaches
    `spool` by the same row Home Assistant does.

### 11.4 Put to the operator

Each is a capability a user would notice, and the canon does not decide
it. The document is written to the recommendation, and each answer is a
line.

1.  **Retire shoko?** Recommended. Jellyfin stopped loading its plugin in
    February, no vhost or client reaches it, 60 days of traffic come to
    5.9 MB, and it holds 0.7 GiB of memory. It would retire as the
    dormant applications of migration.md §2 did, its 3.3 GiB of
    configuration archived to the NAS. Keeping it is `Name('shoko',
    Home(LAN))` and one small build.
2.  **Does thread-dashboard stay on the host?** Recommended. It uses
    multicast on the IoT bridge for its HomeKit names, which a pod on the
    worker cannot see. Moving it loses those names; keeping it adds one
    host service to nodes.md §4.1's list, at 35 MB.
3.  **Are `sync`, `sync-nas` and `spool` reachable from the home alone?**
    Recommended. The log shows no client from outside on any of them,
    over 18 days for `spool` and 23 hours for the other two. Each is an
    administrative interface, and home-only means the gateway no longer
    has it on its public side. Away from home they are unreachable, and
    no overlay member is steered to a `lan` VIP. Keeping one public is
    a `Both(Public(PROXIED, gate=SESSION), …)` in place of its row.
4.  **Does only `go` keep its bare name?** Recommended. `go/<name>` is
    how golinks is used. Nothing shows anyone typing `tube/`, `bt/`,
    `gw/` or `dns/`; each stops redirecting at its cut, and the full name
    keeps working. Keeping `tube` or `bt` is `bare=True` on its row;
    keeping `gw` or `dns` is a field slice 26 adds.
