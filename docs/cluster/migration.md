# Migration Plan: Legacy (kluster-code) → Next Gen

How the workloads and data move from the k3s legacy cluster to the new
one, in dependency-ordered waves, ending with the legacy deployment
decommissioned. Target shapes per app are
[declarative/workloads.md](../declarative/workloads.md); this document
owns sequencing, data movement, and teardown.

> **Status**: the plan [rfc-008](../rfc/rfc-008-workload-migration.md)
> designs. Its §5 carries each application's component, data move,
> verification, cut and rollback, and its §5.9 the soaks and the
> calendar; this document stays at the plan level.

## 0. Standing rules (apply to every step)

1.  **Build, land, verify, cut, per application.**
    -   **A component merges unwired before its application moves.** Its
        module, parameters and tests land in a pull request of their
        own, and no program calls it, so the merge declares nothing and
        changes nothing live. Its census rows arrive with the wiring.
    -   **The legacy application is stopped by a kluster-code change**,
        never by `kubectl scale`: kluster-code's deploy applies on every
        merge to its `main`, and would undo a scale made by hand. The
        stop sets the application's workloads to no replicas, keeps
        every claim it holds, and merges at the head of the window,
        before the data move (§3).
    -   **Land.** A pull request wires the component into the `apps`
        program and adds its route rows held by the legacy estate
        (rfc-008 §3.3). The application runs on the new cluster and its
        route attaches to the Gateways, while no public record and no
        rewrite names it, so the name still resolves where the legacy
        estate publishes it.
    -   **Verify.** The name is requested at the cluster's front door,
        `curl --noproxy '*' --resolve <name>:443:<address>`, at the balancer's address
        for a row on `internet` and at the VIP of each home Gateway the
        row sets, which reaches the new application exactly as a
        client's request will after the cut.
    -   **Cut.** A second pull request deletes every legacy declaration
        of the name — its block in `legacy.py`, the web origin's address
        for an apex, a rewrite rule on the resolvers' declared list, a
        row of `LEGACY_VHOSTS` — and takes the row out of the held
        state, so `apps` publishes the record and `dns` the rewrite.
        Verification needs something deployed, and a merge is what
        deploys, so the land and the cut are two merges; hath and matrix
        take a third, below.
    -   **A window's applications land together and cut together.** One
        that does not verify is left out of the cut, and its land is
        reverted on its own. A home-only application publishes no record
        to collide with, so it lands with its row published, and its cut
        deletes the legacy name's vhost row or block.
    -   **Legacy stays intact and re-startable until verification
        passes.** The rollback follows how far the move got: before the
        land, revert the stop; after the land, revert the land, which
        deletes the workload and published nothing, then the stop; after
        the cut, revert the cut in one merge, re-run the chain once its
        first run has failed in `up-dns` (rule 2), then revert the stop.
        Data written to the new application after its cut is not
        carried back. After a growth of the worker, rule 4's reversed
        window comes first.
    -   **A legacy service that other legacy applications depend on
        stops when its last legacy consumer has moved**, not with its
        successor's land. The mail relay keeps relaying for legacy
        immich and the legacy monitoring until Wave C's head. The
        authentication service stops at Wave A1's head, and the same
        kluster-code change points the legacy forward-auth checks at the
        new service's public authorization endpoints, so the gated
        legacy fronts ask the one service whose sessions the portal
        issues until each is cut; that target goes with `spool`, the
        last of them, at Wave B's cut.
    -   **The legacy application is removed by a second kluster-code
        change, after its cut has soaked**, as part of the next window's
        head: the component goes, and its claims' directories are mapped
        or deleted in the retained-volume census (§3). The removal is
        what releases its memory for the next growth (rule 4).
2.  **The DNS cut is per application, and the name changes hands in one
    run of the deploy chain.** `dns` deletes the legacy record and
    `apps` creates the application's — or `k8s-base`, for the name the
    monitoring component serves — at the same name. Neither stack can
    adopt the other's record, and Cloudflare refuses a CNAME beside
    another record of the same name, so `up-k8s-base` and `up-apps`
    run after `up-dns`, whatever its result
    ([framework/ci.md](../framework/ci.md) §3), and the name resolves
    nowhere between the delete and the create. Where `up-dns` deleted
    and `up-k8s-base` then failed, `up-apps` is skipped and the cut's
    names resolve nowhere until a run succeeds, so a window re-runs the
    chain until it succeeds before it verifies the cut. A reverted cut's
    first run fails in `up-dns`, on Cloudflare's refusal to recreate the
    legacy record beside the application's, while `up-k8s-base` and
    `up-apps` delete the application's record; the re-run recreates the
    legacy record. No big-bang DNS day.
3.  **Every absorbed resource updates its old tracker**: the step that
    moves a domain onto a stack ends with that tracker's
    removal/pointer commit — nothing tracked twice or by nothing.
    Which tracker holds each domain until then, what a change there
    takes, the step that flips it, and the interim rule for a change
    made before the flip: [sources-of-truth.md](../sources-of-truth.md).
    What the gw-config commit covers, piece by piece, and what holds
    each piece instead: physical/gateway-cutover.md §7.
4.  **RAM is interleaved, and the worker grows to fit** (nodes.md
    §4.2). While both clusters coexist, legacy k3s holds its share of
    the host's memory, so the worker VM boots at **10 GiB RAM**. Its
    disk is created at 60 GiB (`HOMELAB_DISK_GIB` in
    `kluster.conventions.homelab`) and grown on the host to its target
    in Wave C's window, under the bound physical/homelab-host.md §1
    sets on the disk file's length.
    -   **A window grows the worker by what the next wave's requests
        and their headroom need, capped by what legacy has released.**
        Whatever legacy releases beyond that stays with the host, so the
        end-state size is the decommission's decision (§4) and not a
        side effect of the waves.
    -   **A growth is a drained window and a pull request of its own**:
        a commit to `HOMELAB_MEMORY_GIB` in `kluster.conventions.homelab`
        and an apply of `physical` that replaces the domain, stopping
        it, redefining it and starting it again.
    -   **One window is enough: Wave C's.** It grows the worker from
        10 GiB to about 16, for Waves C and D together, and is also the
        A380's bind, which replaces the domain anyway. Waves A and B put
        little on the worker and fit it as it booted, and Wave D's
        qbittorrent and seedwatch take 0.25 GiB.
    -   **Before the growth commit merges**, each of these is read and
        recorded on the wave's issue: every application of Wave B has
        been cut and has soaked; each of their legacy workloads has been
        removed in kluster-code, not merely stopped, so nothing can start
        it again; the host's available memory, read with the ZFS cache
        at its floor and the legacy CI runners at the load they will
        carry through the wave, covers the growth with a margin; the
        worker's requests after Wave C's land, and its peaks, leave the
        headroom against the allocatable the growth gives; and the root
        filesystem covers the disk file at its new length, read as
        physical/homelab-host.md §1 states. Where they do not, the wave
        waits, and the operator rules on the worker's size
        (rfc-008 §11.2).
    -   **A rollback across a growth is the window reversed.** A growth
        spends memory a legacy application needs to start again, so the
        worker is drained, the domain shrunk by the commit's revert, and
        the legacy application started, and only then is rule 1's
        rollback run. It costs a drained window, which restarts every
        workload on the worker.
5.  **Sealing key first, then rotated last**: the legacy sealed-secrets
    key is carried into the new cluster before any legacy ciphertext
    is ported (§1 item 5); it is regenerated, and the legacy key
    deleted at decommission (§4, Wave F) — carry-for-continuity,
    rotate-for-hygiene.
6.  **Rule: every wave passes a gate on the control planes before the
    next one starts**, judged against the budget of nodes.md §1.1–1.2.
    -   **The window** runs from the wave's cut to the end of its soak
        (rfc-008 §5.9), less the peak windows the wave's issue records:
        the land's image pulls, a claim streamed through the API server,
        hath's warm copy. Each reading over it is recorded beside the
        previous soak's value. Wave A1's baseline is 24 hours of the
        empty cluster after the bring-up's gate (§1 item 4).
    -   **A wave moves on only when, on every control plane:**
        1.  **etcd**: WAL fsync p99 under 10 ms in every 5 minutes, and
            no fsync over 1 s; backend commit p99 under 25 ms; mean
            apply at most 100 ms; no leader change; heartbeat send
            failures not increasing.
        2.  **Disk**: OCI's throttled I/O is 0 on every boot volume;
            writes other than etcd's within 300 IOPS and 2.4 MB/s,
            hourly; I/O pressure (some) at most 10 % in every 10
            minutes.
        3.  **CPU**: CPU pressure (some) at most 20 % in every 10
            minutes and at most 10 % over the window; and no pod is
            left Pending for want of CPU (`FailedScheduling`,
            `Insufficient cpu`) over the window.
        4.  **Memory**: available memory at least 1 GiB at its lowest;
            no memory pressure (full); and no pod is left Pending for
            want of memory (`FailedScheduling`, `Insufficient memory`)
            over the window.
        5.  **Network**: public and KubeSpan traffic at most 62.5 MB/s
            at the 99th percentile of 5-minute rates; no KubeSpan peer
            down.
    -   **Every peak inside the wave**, the excluded windows included,
        holds on every member: no leader change, and no fsync and no
        apply over 1 s.
    -   **A breach stops the next wave** and goes to the operator with
        the reading. A reading that moved but stays inside the budget is
        reported beside its baseline and decides nothing.
    -   The readings come from the control-plane series the monitoring
        component keeps (Aetf/kluster-ops#790) and from OCI's
        block-volume series for throttling. The command that evaluates
        a window against this rule is not built
        (Aetf/kluster-ops#785).

## 1. Phase 0 — foundations (before any app moves)

[physical/bring-up.md](../physical/bring-up.md) orders these as one
runbook, from the gateway window to a verified, empty cluster.

1.  Manual preconditions: OCI tenancy on PAYG (home region choice is
    permanent), the homelab host-prep aconfmgr change-set (bridge,
    subvolume (the pool on it is `physical`'s), libvirt SSH identity,
    NFS exports — physical/homelab-host.md §4). The state backend is a
    precondition and not a manual one: the `state-backend` stack applied
    (physical/state-backend.md). The Talos image is not among them:
    the stack builds it through the Image Factory and imports it itself
    (declarative/physical.md §1). ZeroTier Central config is
    Pulumi-managed (architecture.md §5.3).
    **The overlay's home-LAN routes are net-new** (today the overlay and
    the LANs are not connected at all): the UDM container deploys via an
    operator-local run over the LAN, routes are added, and only after
    the flow-rules verification does CI's per-run overlay join become
    load-bearing (physical/gateway.md §2.5).
2.  `physical` up: 3× A1 (A1 capacity confirmed at creation), worker VM
    (its disk created at its ~60 GB bootstrap size,
    physical/homelab-host.md §1), NLB, the UDM's BGP configuration and container services, B2. The gateway's share of this is
    **three applies, not one, and operator-local by construction** — CI
    reaches the site over ZeroTier and the gateway is not on ZeroTier
    yet. Set `gatewayBootstrapHost` to a LAN address for the UDM and
    apply, which lands the container services and with them the ZeroTier container; commit
    the gateway's roster entry, carrying the node id that container
    mints, and apply again, which authorizes the member at the address
    the routes already name as their next hop — they ride the overlay
    network resource, which the first of those applies leaves to the
    run that applies the rest of the stack (physical/gateway-cutover.md
    §5); unset the knob and apply once more, which dials over the
    overlay and is thereby the verification. Step by step, and why each
    step is one: physical/gateway.md §2.5. The device is already
    running the layout gw-config built, so the first of those applies
    opens with a cutover window that moves the live container state
    under the declared paths — procedure, verification and rollback:
    physical/gateway-cutover.md. **The controller-side verifications
    come before that window, not after `k8s-base`**: the zone-policy
    round trip (declarative/physical.md §6) is first exercised by the
    window's own apply, so it runs as a probe on scratch objects on some
    day before it, beside the probe of the device's image pull
    (physical/gateway-cutover.md §3). The same probe writes a scratch
    port forward, whose real counterpart is first written in Wave D
    rather than in the window (physical/gateway.md §4.2). `dns` up:
    zones + base records imported wholesale (records still pointing at
    `archvps.hosts`; the import census also drops dead weight —
    `abacus.hosts`, its overlay entry, jupyter/mc records).
3.  `k8s-base` up; backup-freshness alerts live. (Talos-level checks
    that need no CNI — etcd fsync, A1 capacity, talosctl→homelab via
    the NLB's management address — may run before this step; everything
    Cilium-shaped cannot.)
4.  **Verification gate** — the consolidated checklist
    (physical.md §6) less the controller-side items item 2 ran before
    the window, run **after `k8s-base`** because most items
    exercise Cilium: LB-IPAM pool with the on-the-wire node IPs; NLB
    dual-stack + source preservation; reserved-IP NAT; MTU over
    KubeSpan; the security
    verifications (pod→IMDS denied, bogus-BGP rejected) — alongside the Talos-level checks item 3 names, etcd
    fsync among them. **No app migrates until this gate passes** —
    every item is cheaper to fix on an empty cluster. ExternalAuth
    failing closed and the auth canary need what the first wave
    brings, and move to it
    ([physical/bring-up.md](../physical/bring-up.md) §6).
5.  **Handing over to the first wave** (bring-up.md's last stage), after
    `physical` has applied `conventions.backup.BACKED_NAMESPACES`, which
    lists every namespace the waves back up, so every writer key
    exists:
    -   **The legacy sealing key is carried by hand once**, from the
        legacy cluster's controller to the new one's, by a pipe between
        the two clusters' API servers that writes it to no file, and the
        controller is restarted to read it. The certificate the
        controller serves is then confirmed to be the cluster's own,
        since a restored key can become the one that seals. The key has
        no channel of its own: its last use is the decommission's
        re-sealing, which deletes it (§4).
    -   **The writer keys are sealed**, once the certificate is
        confirmed: `credentials` reads each namespace's key from
        `physical`'s state under that stack's passphrase and seals it
        namespace-wide into the `sealedSecrets` of the stack that
        declares what reads it, one ciphertext serving every consumer in
        its namespace: `apps` for an application's namespace, and
        `k8s-base` for the monitoring stack's, whose stores' backups and
        restores read it (`conventions.sealed.WRITER_KEY_STACKS`). It is
        one operator step for every namespace at once.

## 2. Waves

Ordered by dependency and risk; cloud first (no NVMe contention, and
the VPS empties progressively). Each wave soaks before the next step
spends its way back (rfc-008 §5.9).

-   **Wave A1 — exim and authelia.** **exim moves at the head of Wave
    A**: authelia sends its notifications through it, as immich and
    Grafana do later. It runs on the worker, the architecture its legacy
    image is built for (workloads.md §5); its DKIM key is in the `apps`
    stack's config before the wave, by
    `credentials derived dkim-exim record`, and once `apps` is up,
    sources-of-truth.md §X, run against the new cluster's `dkim-exim`
    Secret, gives the digest the legacy relay's key did. The legacy
    relay keeps relaying for its legacy senders until Wave C's head
    (§0 rule 1). **authelia** (SSO gates everything else) moves with it,
    its users and second factors on a backed claim, and the legacy
    authentication service stops at this wave's head (§0 rule 1). The
    authentication canary ships with it.
-   **Wave A2 — blog, splitpro, matrix, syncthing, stdiscosrv.** This
    wave reaches the worker as well as the cloud pool. **blog/static
    sites** (prerequisite: blog CI publishes the built branch; cluster
    side is static server + git-sync, workloads.md §4 — the rsync
    `deploy:` config and the SSH pinhole retire with it) → splitpro
    (small CNPG, its database by recovery from the legacy store, §3 —
    **prerequisite: the multi-arch pgcron image from the ported
    images.yml**, ci.md §4) → matrix (continuwuity, its claim streamed,
    §3, in three merges: a land that wires it with a receiver in the
    server's place, which the legacy claim is streamed into, a second
    merge that starts the server on what was streamed, and the cut,
    which deletes the receiver, rfc-008 §5.3; its `.well-known`
    delegation rides the blog instance, workloads.md §4, so it is
    already serving by this point) →
    **syncthing, with the WebDAV server beside it, on the worker**: no
    cloud replica moves, since its one role was an always-reachable
    port for roaming devices, and the balancer's peer port now reaches
    the NAS instance, which keeps its own device identity; so the NAS
    instance and the WebDAV server move here rather than in Wave C →
    **stdiscosrv** on `syncapi`, on the cloud pool, at a raw port on
    8443, which a redirect on 443 sends each device to, so no device's
    discovery address changes
    ([Aetf/kluster-ops#723](https://github.com/Aetf/kluster-ops/issues/723)).
    Legacy splitpro and matrix run on the homelab host, so Wave A frees
    homelab memory and asks for no growth (§0 rule 4).
-   **hath, after Wave A2 has soaked** (operator ruling, rfc-008 §11.1),
    ahead of Wave B: it is cloud-side and depends on nothing Waves B to
    D move, and the address it needs is exercised by nothing but hath,
    so no wave before it would prove it. hath is the highest-stakes
    workload (global-archive data, IP re-registration, strict downtime
    cap). It leaves by the reserved address on its node's primary
    private address (architecture.md §3.2, rfc-008 §5.4), which a
    `physical` change of its own moves there at hath's head, in place of
    the secondary private address. It moves in three merges: a stage
    that wires the client not started and copies the 50 GiB cache warm
    while the legacy client still serves; a land that stops the legacy
    client and copies the final delta and the client state; and a cut
    that registers the new address with the network. The VPS holds no
    ingress after Wave C's cut, and is cancelled when hath's soak ends
    (§4).
-   **Wave B — homelab VM, light**, and no window: every application of
    it fits the worker as it booted. **Its head waits** for the
    operations handler's first push delivered over the overlay, which
    the operations repository builds under Aetf/kluster-ops#715, and
    then for rfc-008's slice 47, which turns `deploy.yml`'s
    `notify-failure` into the `alert` job; until the handler delivers,
    that job is the one deploy alert that reaches a phone. If the
    handler is late, Wave B's head and Wave C behind it move by the
    delay; hath and Wave D need not, and nothing is stopped while they
    wait. The applications: golinks, emailproxy, spoolman (its single
    file streamed off the shared JuiceFS volume, §3); **`haos` on a
    device relay in the cluster**, a pod on the worker that backs the
    row on both Gateways, with its home side on `media`, the side that
    serves every home VLAN, the IoT one included, from the worker; the
    **UPS card's and thread-dashboard's names**, device rows at
    `lan-gw` on relays of the same kind, whose rows leave
    `LEGACY_VHOSTS` at this wave's cut, so the gateway's proxy answers
    the gateway's own names alone once that census is empty; the doors
    static sites (NAS-sourced, workloads.md §4 — their content moves
    from the VPS hostPath onto a NAS share once); and **monitoring's
    name** (the monitoring component already runs, installed by
    `k8s-base` from M2 with stores of its own, and the legacy series of
    what moves to the control planes reach its history store at Wave
    C's head, §2.1;
    what the wave moves is the dashboard's `mon` name from the legacy
    block to that component, published by `k8s-base`, and with it the
    alert read route, which needs a public name and lands with its
    reader, the operations repository's poller — rfc-007 §7.4). **The
    legacy monitoring keeps running until Wave C's head**: it alerts on
    the legacy applications that have not moved, immich and jellyfin,
    and on the legacy node they run on.
-   **Wave C — homelab heavy, opened by the GPU window**: the head
    removes Wave B's legacy applications and the legacy JuiceFS stack,
    whose last consumer was spoolman, with their claims kept, and stops
    legacy immich, jellyfin, shoko, the legacy monitoring and the legacy
    mail relay, whose last senders those are. Its first step, before the
    change that stops the legacy monitoring merges, imports the legacy
    series of what moves to the control planes into the history store
    (§2.1). The wave then opens with
    one drained window that both grows the VM (§0 rule 4) and runs the
    A380's vfio-pci cutover (drain → bind → hostdev → reboot,
    physical/homelab-host.md §3), since each replaces the worker's
    domain. The cutover takes the legacy cluster's GPU: since
    kluster-code#253 the legacy device plugin is limited to the A380,
    which every legacy GPU consumer holds. At the head, a kluster-code
    change moves that plugin to the UHD 770 iGPU (`8086:4680`), which
    stays with the host. The legacy GPU consumers are already down when
    the window binds the A380, and §0 rule 4 grows the VM by what they
    free. They stay down from the head until each move verifies, and a
    legacy app runs again only through §0 rule 1's rollback, which
    starts it on the iGPU at any point in the wave and, after the
    growth, comes after §0 rule 4's reversed window (kluster-ops#672).
    Then the moves, with the new immich and jellyfin taking the A380
    from their first start: immich (CNPG by recovery from the legacy
    store, §3 — a path this cluster has not drilled; NAS media PVs
    re-point in place; ML/thumbnail caches re-derive), jellyfin+shoko
    (NAS re-point + config copied through the NAS; **verify the TVs'
    path — IoT → media-VIP allow — before its DNS re-points**,
    physical/gateway.md §4.2). Jellyfin's discovery stays, through the
    heard relay on the host (nodes.md §4.1). The VPS's last legacy
    ingress name is cut here.
-   **Wave D — host-native onboarding**: qbittorrent-nox
    (`/var/lib/qBittorrent` profile copy; verify seedwatch category
    paths and hardlink counts survive; outbound-v6 via masquerade
    first, inbound pinhole later) + seedwatch together. **The v4 peer
    port moves with it.** Until this wave the legacy qbittorrent holds
    the port's WAN side and `physical` declares no forward for it
    (physical/gateway.md §4.2), so the move is: read the console for
    what holds the port (the port-forward list, and the UPnP lease
    table); stop the legacy qbittorrent and delete a hand-kept forward
    if one exists; turn UPnP off in the copied profile, since the
    declared forward is the mechanism from here on; then
    `physical config set qbittorrentOnWorker true`, through the
    `physical()` shell function physical/gateway-cutover.md defines,
    which runs `pulumi` under that stack's own passphrase
    (credentials.md §4.4), and apply `physical`, whose plan creates the
    forward to the worker. The cut takes the `bt` and `seedwatch` rows,
    the last of `LEGACY_VHOSTS`, so that census empties with this wave.
    The host units are stopped at the head and removed from aconfmgr
    and yadm after the soak.
-   **Wave F — decommission** (§4).

Explicitly **not** migrating: AdGuard alice/bob (stay on the UDM, now
Pulumi-managed), HAOS (a host-level libvirt domain, and one this program
does not declare — declarative/physical.md §3), the NAS role, the legacy
state backend (serves kluster-code until F), **dmarc-check** (stays
a host timer using the host's claude credentials, nodes.md §4.1 —
in-cluster adoption deferred until the cluster is stable, and a
different approach may supersede it), and **thread-dashboard** (stays a
service of the host, operator ruling: its HomeKit names need multicast
on the IoT bridge, which no pod has, and moving it would save 35 MB;
its name is served through a device relay from Wave B, nodes.md §4.1).

**Retired outright, never lands in the new cluster** (decided
2026-08-23): the dormant legacy apps — **mc** (Minecraft; its world/map
PVC holds real data: tidy it and archive to the NAS as fixed assets
*before* the PV is deleted in the census below), **ukulele**, the old
**bt** stack (superseded by the host qbittorrent, which *does*
migrate — Wave D), and the **genshin** daily cronjob. Their config
flags, dead DNS records (the jupyter/mc leftovers, dns.md §2 — the
`game`/`games` names stay, they are doors), and images drop during the
import census; resurrection, if ever wanted, is
git history.

### 2.1 Wave C's head: the legacy series imported

The control planes' readings compare every period with every other
(§0 rule 6), and for what moves onto them the period before the move
is on the legacy Prometheus alone. That store stops at Wave C's head
with the rest of the legacy monitoring, so the head's first step, run
once before the kluster-code change that stops it merges, copies those
series into the history store
([declarative/cluster-infra.md](../declarative/cluster-infra.md) §1
item 6) with `vmctl`, which `mise.toml` pins.

-   **Which series.** The history store's class, `HISTORY_CLASS` in
    `kluster.components.monitoring.stores`, less its entry for the
    `etcd` job, which describes the new control planes alone. It is
    taken in three selections, each the class's names and every label
    match listed:
    -   the workloads Aetf/kluster-ops#785's design places on the
        control planes, by their legacy namespace: `serving-system`,
        which holds traefik and authelia with its Redis, and `hath`,
        `splitpro`, `matrix` and `nginx`, the blog's legacy server;
    -   stdiscosrv: the pods of `syncthing` whose names start with
        `stdiscosrv-`, since the namespace's other pods are the cloud
        syncthing replica, which does not move;
    -   the VPS node, as the legacy node exporter reports it: the
        `node-exporter` job, `node="aetf-arch-vps"`.

    A workload that left the legacy cluster in an earlier wave is
    selected all the same, its series ending where it stopped. Every
    series is written with `cluster="legacy"` beside its own labels and
    the two the legacy Prometheus adds to what it serves over remote
    read, its external labels `prometheus` and `prometheus_replica`;
    `cluster="legacy"` is how a reading over the history selects it.
-   **The time range.** From midnight UTC 100 days before the run: the
    legacy store's retention is 90 days (`retention` in kluster-code's
    monitoring component), and it drops a block only once the whole
    block is older, so it holds up to a week more; a day it holds
    nothing for costs one empty request. The range ends at `END`, the
    moment the run starts, which fixes the range both readings compare.
    `vmctl`
    reads it a day per request, which bounds what one request costs the
    legacy Prometheus. What the legacy store takes after `END`, until
    the stop merges, is not carried.
-   **The source.** The legacy Prometheus, which the chart
    `kube-prometheus-stack` runs in the legacy cluster's `mon`
    namespace, reached through a
    port-forward of `prometheus-operated`, the Service the Prometheus
    operator gives every Prometheus it runs, from the workstation's
    port 19090 to its 9090. `vmctl` reads it through Prometheus's
    remote-read API, `/api/v1/read`.
-   **The target.** The history store's import endpoint,
    `/api/v1/import` of `vmsingle-history` in `monitoring`, reached
    through a port-forward from port 18428. The import writes to the
    store directly rather than through vmagent, so the relabeling that
    holds vmagent's remote write to the class does not apply to it: the
    selections name the class themselves.
-   **The reading that confirms it**, taken on each side over the same
    range: for each selection, the number of series, equal on the two
    sides; and for one name, the VPS node's `node_load1`, the number
    of samples, equal too.

From the root of a checkout, with `<legacy>` the `kubectl` context
that reaches the legacy cluster (sources-of-truth.md, K3):

```sh
mise x -- kubectl --context <legacy> -n mon port-forward svc/prometheus-operated 19090:9090 & OLD=$!
mise x -- kubectl --kubeconfig ~/.kube/kluster -n monitoring port-forward svc/vmsingle-history 18428:http & NEW=$!
curl --noproxy '*' -sS http://127.0.0.1:19090/-/ready; echo; curl --noproxy '*' -sS http://127.0.0.1:18428/health; echo
NAMES=$(mise x uv -- uv run python -c 'from kluster.components.monitoring.stores import HISTORY_CLASS as c; print("|".join(k.names for k in c if k.job == ".*"))')
START=$(date -u -d '100 days ago' +%Y-%m-%dT00:00:00Z); END=$(date -u +%Y-%m-%dT%H:%M:%SZ); FROM=$START
RANGE=$(( $(date -u -d "$END" +%s) - $(date -u -d "$START" +%s) ))s
sel() { s="__name__=~\"$NAMES\""; for p in "$@"; do s="$s,${p%%=*}=~\"${p#*=}\""; done; echo "{$s}"; }
each() { "$@" namespace='serving-system|hath|splitpro|matrix|nginx' && "$@" namespace=syncthing pod='stdiscosrv-.*' && "$@" job=node-exporter node=aetf-arch-vps; }
answer() { mise x -- jq -n -r "(input? // error(\"no answer\")) | if .status == \"success\" then $1 else error(.error // \"no answer\") end"; }
series() { curl --noproxy '*' -sS -G "http://127.0.0.1:$1/api/v1/series" --data-urlencode "match[]=$2" --data-urlencode "start=$START" --data-urlencode "end=$END" | answer '.data | length'; }
src() { n=$(series 19090 "$(sel "$@")") && echo "$n" && [ "$n" -gt 0 ]; }
dst() { series 18428 "$(sel "$@" cluster=legacy)"; }
samples() { curl --noproxy '*' -sS -G "http://127.0.0.1:$1/api/v1/query" --data-urlencode "query=sum(count_over_time(node_load1{job=\"node-exporter\",node=\"aetf-arch-vps\"$2}[$RANGE]))" --data-urlencode "time=$END" | answer '.data.result[0].value[1] // 0'; }
newest() { curl --noproxy '*' -sS -G http://127.0.0.1:18428/api/v1/query --data-urlencode "query=max(tlast_over_time($(sel "$@" cluster=legacy)[$RANGE]))" --data-urlencode "time=$END" | answer '.data.result[0].value[1] // empty'; }
import() {
    f=(--remote-read-filter-label=__name__ "--remote-read-filter-label-value=$NAMES")
    for p in "$@"; do f+=("--remote-read-filter-label=${p%%=*}" "--remote-read-filter-label-value=${p#*=}"); done
    env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
        mise x -- vmctl remote-read -s --disable-progress-bar \
        --remote-read-src-addr=http://127.0.0.1:19090 --remote-read-step-interval=day \
        "--remote-read-filter-time-start=$FROM" "--remote-read-filter-time-end=$END" \
        --vm-addr=http://127.0.0.1:18428 --vm-concurrency=1 --vm-extra-label=cluster=legacy "${f[@]}"
}
each src && samples 19090 '' &&
    each import &&
    curl --noproxy '*' -sS --fail http://127.0.0.1:18428/internal/force_flush &&
    each dst && samples 18428 ',cluster="legacy"'
kill $OLD $NEW
```

-   **The probes** on the third line are taken once each port-forward
    has printed its `Forwarding from` line, and answer
    `Prometheus Server is Ready.` and `OK`. A probe that fails is
    recorded with the error line `curl` printed
    ([framework/dispatch.md](../framework/dispatch.md) §4), and so is
    the `vmctl` run, which the proxy variables are removed for.
-   **Every reading fails on anything but an answer**: `series`,
    `samples` and `newest` print the store's error text, or
    `no answer` where the port-forward gave `curl` nothing, and return
    non-zero, so an error is never read as a count of zero. Each step
    of the last four lines runs only once the one before it succeeded.
-   **`each src` reads the legacy store first**: one count per
    selection, then the samples of the VPS node's `node_load1`. A
    selection that counts no series stops the step there, since its
    label matches are not the legacy store's labels, and an import
    through it would write nothing and pass its own reading.
-   **`each import` runs one `vmctl` per selection** and stops at the
    first that exits non-zero. Each prints its importer's statistics,
    the samples it wrote and the retries it took among them. Each
    writes through one import worker (`--vm-concurrency=1`): a worker
    sends a batch only once it is full, and a `vmctl` that fails drops
    the batch each worker holds, so with one writer what the store
    holds is a prefix of the day-ordered stream, and the day of its
    newest sample is the only one that can be partial.
-   **The forced flush** makes what the store was just written
    searchable: VictoriaMetrics holds new samples in memory for up to a
    few seconds before a query sees them, and `/internal/force_flush` is
    the endpoint its documentation gives for querying them at once.
-   **`each dst` and the second `samples`** are the reading: each line
    equal to the one `each src` and the first `samples` printed.

**A selection is not imported twice over the same days.** The history
store keeps every sample it is written, a second copy of one already
held included (VictoriaMetrics' documentation, "Data updates"), and its
backup refuses a store that holds fewer rows than the last backup
recorded (declarative/cluster-infra.md §1 item 6), so samples written
twice are not deleted once the next hourly backup has counted them. A
selection that stopped partway is run again from the first day of its
newest sample in the history, or from `START` where the history holds
no sample of it, and each selection after it from `START`. The newest
sample is read after a forced flush, like the reading, since what the
stopped run wrote last is otherwise not yet searchable:

```sh
curl --noproxy '*' -sS --fail http://127.0.0.1:18428/internal/force_flush &&
    t=$(newest <the selection's matches>) &&
    FROM=$(if [ -n "$t" ]; then date -u -d "@$t" +%Y-%m-%dT00:00:00Z; else echo "$START"; fi) &&
    import <the selection's matches>
FROM=$START
```

That day's samples written before the stop are then held twice, and
where the selection was the VPS node's, the second `samples` reads
more than the first by those.

The history's earliest sample, which the rebuild's baseline reads
(operations.md §3.5), is the legacy range's first from this step on.

## 3. Data movement, by storage kind

| Kind | Technique |
| --- | --- |
| CNPG databases | recovery from the legacy object store, not dump/restore reinvention. The legacy databases archive to Google Cloud Storage, one bucket per database, and the new cluster bootstraps from it through the barman plugin once, at the land, with the legacy credential ported; it then archives to the backup bucket, under a server name carrying a generation, so that a land retried after a revert archives to a path of its own. The drill that stands behind this path is the legacy cluster's: its quarterly `immich-restore-drill` CronJob recovers a scratch cluster (`immich-db-restore`) from the same barman backup, on the legacy operator and against the legacy bucket. That is evidence the backup restores, not that a restore has run on this cluster — none has (operations.md §4) |
| Legacy local-path PVCs | a claim bound for the worker is copied onto the NAS on the host, where the data already is, and read from there by a pod that mounts both; a claim bound for the cloud pool is streamed with `tar` through the new cluster's API server into a pod that mounts the target claim (`kubectl exec -i … tar -x`), since a Talos node runs no SSH server (VolSync takes over *after* landing) |
| NAS-backed data | **no movement** — PVs re-point at the same datasets; the NAS backup regime is untouched |
| Legacy shared-JuiceFS data | **the cloud syncthing replica goes**, uncopied: the NAS instance holds the full set and serves roaming devices through the balancer. **Two copies are made**, each from the legacy pod's mount: the WebDAV server's own files (Wave A2) and spoolman's single database file (Wave B) |
| hath cache | copied warm ahead of its land, then the final delta + client state at the land (preserved, not backed — workloads.md §2) |
| Monitoring TSDB | not migrated whole: the legacy monitoring runs until Wave C's head, whose first step imports the series of what moves to the control planes into the history store with `vmctl` (§2.1), and its claim is then kept cold until its retention would have aged out |

**Retained-PV census** (storage.md §3.3): while touching each legacy
app, its retained-PV directories are identified and either mapped to
the migration or deleted — the one-time cleanup of the `Retain`-era
orphans.

## 4. Wave F — decommission checklist

When Wave D's cut has soaked, every application serves from the new
cluster. The decommission opens with a design of its own; what the
waves leave it:

1.  Legacy VPS: no application is served from it after Wave C's cut,
    and hath has left its egress since its own move; it is cancelled
    when hath's soak ends (the $30/mo baseline ends). The records that
    still name it then are the parked zones' web origin and its anchor;
    with them gone, nothing in `dns` points at the VPS, and the legacy-held
    wrapper of route rows (rfc-008 §3.3) and its relation test are
    deleted together. `archvps.hosts`, which nothing references by
    then, is deleted from the `dns` stack (dns.md §6). The
    `Aetf-Arch-VPS` entry leaves `conventions.overlay.ROSTER` no later
    than the cancellation, which takes its member off the network at the
    next `physical` apply and its `*.zt` record out of `dns`
    (physical/gateway.md §2.1, dns.md §2). DNSControl is not among the
    trackers retired here: it retired at the zones' cutover, the `dns`
    up of §1 item 2 (sources-of-truth.md, row R1).
2.  Homelab host: the legacy cluster stopped and k3s uninstalled after
    Waves C/D, its retained-volume census finished. **The CI runners a
    neighboring project runs on the legacy cluster need a home before
    the uninstall.** The worker's final memory size is decided here,
    against what the host keeps for what the operator runs beside the
    cluster (nodes.md §4.2); JuiceFS redis/mount residue gone with k3s;
    `lan.ucw.phd` entries emptied (dns.md §4).
3.  Trackers: gw-config repo retired (provider owns the device);
    adguardhome-sync unit removed, here at the latest: it may stop at
    any time and must stop before the `dns` stack first writes bob
    (operator ruling, sources-of-truth.md note N2);
    qbittorrent/quadlet units removed
    from yadm/aconfmgr; the legacy state backend's Postgres stops last,
    once kluster-code needs no further `pulumi` operations. The legacy
    Google Cloud Storage buckets and their ported credentials, read by
    nothing since the cuts, go too.
4.  **Sealing-key rotation**: with every app migrated, all
    SealedSecrets are re-sealed against a freshly generated key and
    the carried legacy key (rule 0.5) is deleted from the cluster.
    The legacy key exists in years of backups; leaving it valid would
    let any old backup copy decrypt future secrets
    (cluster/security-audit.md).
5.  Success criteria: every app serving from the new cluster with green
    backup-freshness alerts; orphan audit reports zero; legacy spend
    $0; the legacy sealing key retired; the only homelab standing
    services are the ones nodes.md §4.1 lists as staying.
