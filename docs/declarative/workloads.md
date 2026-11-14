# Declarative Design: Workloads (the `apps` Stack)

The per-app pattern: one Python component per application, declaring everything
the app needs — workload, storage, exposure, DNS, secrets, backups, placement —
so that "deploy an app" is one reviewable diff in one place. This is the top
stack of [README.md](README.md) §1 and the home of ~80–90% of all future
changes.

> **Status**: designed 2026-08-22. Declared: an application's namespace,
> its level, its route label and its policies, its exposure through
> `expose` with the records left out (§1), and its storage through `claim`
> and `nas_claim` (§2, §3). Nothing else is implemented.

## 1. The component contract

Every app is a `Component` subclass (putils, RFC-001) that owns:

| Concern | Declared as | Governed by |
| --- | --- | --- |
| Namespace | created by the component (never shared) with `application_namespace` (`kluster.lib.k8s`), which enforces its level and gives it `conventions.routes.ROUTE_LABEL`, the label the Gateways admit routes from; a namespace with no route carries it too | README.md §1, cluster-infra.md §2 |
| Workload | Deployment/StatefulSet with **honest requests/limits** — CPU limits are mandatory on anything scheduled to the cloud pool (etcd shares those cores, nodes.md §1); memory requests sized from evidence, not idle numbers (the JuiceFS-sidecar lesson, storage.md §6) | nodes.md §§1, 4.4 |
| Storage | by the two-axis selection in §2 (performance × persistence; fixed assets → NAS/object), through `claim` and `nas_claim` on the application base (`kluster.components.apps.storage`) | §2, storage.md §2 |
| Backup | the `retention` every `claim` takes: a **retention class**, never an ad-hoc schedule, or `off` with its reason (§3) | §3, storage.md §3.1 |
| Exposure | the component receives its application's `Exposure`, its row of `conventions.routes.EXPOSURES`, and makes one call, `expose(exposure)` on the application base (`kluster.components.apps`), with the Service its routes forward to, the pods its raw ports select and the fronts the `apps` program read (dns.md §5). Per `Route`, one `HTTPRoute` for each Gateway its fields set (`kluster.lib.k8s.route`); the gate is a field of its `internet` side and puts the ExternalAuth filter → Authelia on that route alone, so the home side is ungated (cluster-infra.md §2, which holds the gate's Cilium floor); every route without a gate removes the portal's identity headers from the request; `media` on the row is the explicit "IoT may reach this" decision, recorded on the row rather than at a call site (jellyfin; physical/gateway.md §4.2). A `Passthrough` row is refused by name: Cilium drops a passthrough listener together with the zone's wildcard listener on the same port (cluster-infra.md §2), and how the discovery server is reached is [Aetf/kluster-ops#723](https://github.com/Aetf/kluster-ops/issues/723). Per `Port`, one shape whatever its fronts: a LoadBalancer Service for each Gateway it sets, at that front's addresses, under the `Cluster` traffic policy every Service on a shared address takes (architecture.md §3.1) and with `allocateLoadBalancerNodePorts: false`, a home side at its Gateway's VIP under that Gateway's sharing key; the row's NLB listener, the site's forward and its pinhole are `physical`'s (physical.md §1); and one allow admitting the world on the port and its transports alone. A component merges unwired, and is wired into the `apps` program by its application's land (sources-of-truth.md §3) | architecture.md §3.6, dns.md §5 |
| DNS | the public records of the rows on `internet` are not declared by `expose` yet; the split-horizon rewrite a row implies is applied by the `dns` stack from the same declaration, not by this one (dns.md §3) | dns.md |
| Secrets | SealedSecret first choice, `template.data` pattern | cluster-infra.md §1.1 |
| Placement | scheduling constraints only: site pool (cloud/homelab), a node label for a volume or the dedicated VIP, GPU resource requests | architecture.md §3.6 |
| Network policy | per-namespace default-deny + explicit allows, part of the component. `application_namespace` declares two policies. The default-deny: every pod in the namespace admits the namespace's own pods and the `ingress` entity, the identity of what a Gateway's Envoy forwards, and no other peer but the pod's own node, which Cilium admits whatever the policies say; another peer is admitted by a policy the component declares beside it, as `expose` does for a raw port's clients. The default-deny is on ingress alone. The egress block: deny rules alone, with default-deny off, which deny every pod private space outside the pod, Service and `lan` pool ranges, a node's listeners and the kube-apiserver, and leave the rest of its egress open; a reach (`Reach`) and the namespace's API clients (`ApiClients`) are its exceptions, declared on the namespace as below. Restricting egress to an allow-list is the component's: its first egress rule switches its pods to default-deny on egress, so that policy carries the allow to the cluster's DNS too, and the block still applies beneath it, since a deny outranks every allow | architecture.md §4.1, cluster-infra.md §2 |
| Monitoring | scrape/dashboard labels per the legacy conventions (`release`, `grafana_dashboard`) so VictoriaMetrics/grafana pick them up | cluster-infra.md §1 |

What a component may **not** do: hostPort, hostNetwork, `externalIPs`
(architecture.md §6.6); `type: NodePort`, or a LoadBalancer Service
that allocates node ports — on a cloud node a raw Service's `NodePort` is
answered at the node's public address, ahead of its firewall
(cluster-infra.md §2);
cross-namespace reach-ins; unbudgeted sidecars;
cluster-scoped resources (those belong to `k8s-base` and go through its
closed-list rule), save the PersistentVolume of a `nas_claim`, which is
bound to its claim by name and to no other (§2).

Namespaces are created with **Pod Security `restricted` enforced** by
default — non-root, no added capabilities, RuntimeDefault seccomp
(part of containing third-party binaries on the combined CP+ingress
nodes, architecture.md §4.1). An exception is a declared component
parameter with the reason on record — the same discipline as
`off('<reason>')` (§2) — which `application_namespace` takes as its `looser`
argument, a `LooserLevel` carrying the reason. No workload in the
design holds one today.

An application's reach to private space outside the cluster, and a
pod's reach to the kube-apiserver, are declared on its namespace with the
reason, and the base denies both otherwise (cluster-infra.md §2). A reach
names one address, a port and its transports, and is refused where it is
written unless the address is private space the base denies; it opens
that port at that address and nothing beside it, which is where
physical/gateway.md §4.2 rule 3 leaves what a workload may call at home.
A namespace whose pods call the API declares `api_clients`, and the pods
that call it carry `conventions.API_CLIENT_LABEL`; without the
declaration the label opens nothing, and a labelled pod reaches the API's
port on a node and no other port there. The device relay declares its
device as its namespace's reach, and the database shape declares its
instances and its restore drill as API clients (rfc-008 §4.1, §4.3).

## 2. Choosing storage: two axes, then the data's character

The storage.md §2 classes are the *menu*; this is how a workload
chooses. Ask, in order:

**Axis 1 — does it need to persist?** State that is re-derivable
(caches, transcode scratch, ML model downloads) gets **a claim with no
backup, or an `emptyDir`** where it need not survive a restart — but the
exemption is explicit: `claim` requires its `retention`, and an unbacked
claim takes `off('<reason>')`, which refuses an empty reason, so
"unbacked" is always a decision on record, never an omission.
Beware the false "cache": **hath's cache is not re-derivable** — it is
this client's slice of the globally distributed H@H archive, and must
be preserved (see the worked example).

**Axis 2 — what performance does it actually need?**

| Profile | Backing | Why |
| --- | --- | --- |
| Latency/fsync-sensitive: databases, app state stores, sync indexes | node-local NVMe (local-path) | NAS adds network RTT + ZFS-over-JBOD latency to every fsync; DBs on NFS is a known failure mode |
| Sequential/streaming, read-mostly | NAS is fine | throughput is what ZFS+NFS is good at; latency hidden by streaming |
| Cold, rarely read | object storage | highest latency, cheapest, durable |

**Then the data's character decides among the survivors:**

-   **Working state** (small-to-medium, hot, mutated in place) →
    **`claim` under a retention class**, local-path backed up by VolSync
    — the default. If the app is a database, prefer **CNPG** (its
    replication + barman subsume the backup/mobility roles).
-   **Fixed assets** (large, append-mostly, immutable once written:
    media originals, photo masters, books, seeding payloads) → **NAS**
    when POSIX/streaming consumers live homelab-side, **object storage
    direct** when the app speaks S3 or the data is archival. Fixed
    assets never ride local-path: they'd bloat VolSync repos and pin
    huge data to a node for no latency benefit.
-   **The leftovers** — needs POSIX RWX over object capacity — is the
    JuiceFS quarantine gate (storage.md §6), per app, in writing.

**The calls.** The two calls are on the application base
(`kluster.components.apps.storage`), and the class is a name:

-   **`claim(resource_name, name, size, retention, *, namespace, uid,
    writer_key, generation=0, moving_to=None, stopped=None,
    storage_class=local-path, protect=False)`** is the one claim on a
    node, of `size` GiB. `retention` is a retention class (§3) or
    `off('<reason>')`, which emits the claim alone. `uid` is the user
    the workload runs as, which every mover of the claim runs as too, so
    that it reads and writes the data as the workload does.
    `writer_key` is the ciphertext of the namespace's sealed writer key
    as the program read it, `None` while it is not sealed in (§3). The
    generation, `moving_to` and `stopped`, the workload the copy's
    source is applied after, are how the claim moves (storage.md §3.1).
    `protect` protects the claim, for precious data and for a node
    volume's (storage.md §3.3). The caller's `opts` carries the
    dependency on the namespace, which `namespace` names as a plain
    string, since the repository's path is built from it.
-   **`nas_claim(resource_name, name, path, *, namespace, read_only)`**
    is a static PersistentVolume
    over `path` under the host's one export (`conventions.NAS_EXPORT`)
    and its claim, for data the cluster does not own. It takes no
    retention: the source's own regime is its safety net. The volume's
    node affinity is the worker's, the one node that reaches the export
    (physical/homelab-host.md §5), so every pod that mounts it lands
    there. The export is one filesystem with no dataset boundary under
    it, so a new path needs no census and no change to the export.
-   **`cloud-block-<row>`**, `conventions.node_volume_class(row)`, is the
    class a node volume's data is claimed under: an ordinary `claim`
    with that `storage_class` (storage.md §6). The pod that consumes it
    selects the volume's node label (`conventions.NODE_VOLUME_LABEL`),
    since a claim that waits for its first consumer holds no pod
    anywhere by itself.

**A pod writes to the NAS as the export's anonymous user.** The export
squashes every client, so every NFS write arrives as one user, whatever
the pod runs as, and `restricted` needs nothing of it. A path a pod
writes to is therefore that user's. A path the legacy pods wrote as root
is not, so the head of the wave that lands such an application reads the
owner and re-owns the path on the host.

Worked examples: immich = CNPG (db) + `nas_claim('Immich')`, the one
media root whole (originals, thumbnails and encoded video) + a claim,
`off`, for the ML model cache; qbittorrent = `nas_claim('')`, the whole
export, since hard links into `Media` need one mount + a claim,
`STANDARD`, for its config/state; syncthing-nas, the one instance, with
dav beside it = `nas_claim('Sync')` + a claim, `STANDARD`, for its index
and device key; hath = one claim of its row's class, `off`, the client's
state beside the cache — **preserved but not backed**: the cache is a
slice of the globally distributed H@H archive, not scratch, yet like NAS
volumes its safety net is outside the cluster backup regime: the H@H
network's own replication. Its volume is protected and moved, never
recreated (storage.md §3.3).

**Thumbnails stay with the library.** They are re-derivable, but they
sit under immich's one media root and run to hundreds of gigabytes,
which no worker disk holds. Their latency is NFS's until a measurement
says otherwise (physical/homelab-host.md §5's criterion for revisiting
it).

## 3. Backups: centrally classed, locally declared

Backup *declarations* live with the app (co-location, as everything
else), but their *policies* are central, so rotation is never an ad-hoc
per-app number:

-   **Retention classes in `conventions`** — e.g. `STANDARD` (daily,
    keep 30d), `PRECIOUS` (daily 30d + monthly 12), `BULKY` (weekly,
    keep 4) — each mapping to VolSync/restic `retain` settings plus a
    prune cadence. An app picks a class; nobody writes cron lines or
    retain counts inline. Changing a class is one diff that previews
    across every affected app.
-   **A `claim` under a class** emits, beside the claim, a VolSync
    `ReplicationSource` with the restic mover and `copyMethod: Direct`,
    scheduled by the class, to the repository at
    `conventions.volsync_repo_path` of its namespace and its base name
    (`<bucket>/volsync/<namespace>/<name>`). The repository's password
    is generated into state. Its Secret is a SealedSecret under the
    claim's base name that carries **the namespace's sealed writer
    key**, one ciphertext sealed namespace-wide so that it opens under
    every repository Secret's name in the namespace, with the
    repository and its password in its template (credentials.md §3).
    Because every backup flows through `claim`, **the program itself is
    the backup inventory** — a report of "everything backed, everything
    exempted (and why)" is derivable from the stacks, no external
    registry to drift.
-   **The other backup families plug into the same frame**: CNPG barman
    retention expressed from the same class constants; etcd snapshots
    (hourly, ~14d) and the state-backend pg_dump (ci.md §1) are
    physical/CI-side but follow the same bucket-layout and retention
    vocabulary (storage.md §5 stays the authority on targets/RPO).
-   **Freshness is monitored centrally, not per-app**: one vmalert rule
    family — *any* backup whose last success is older than its class
    threshold alerts. A volume's last success is its source's
    `.status.lastSyncTime`, which kube-state-metrics is to publish, and
    its class is the label `claim` puts on the source
    (`conventions.RETENTION_LABEL`), since VolSync's own metrics carry
    neither. The configuration that publishes the series is not written
    (rfc-008 §10, slice 24), so the family evaluates over no series yet;
    once it is, a new claim is covered with nothing added, because the
    label comes from `claim`. Restore drills are designed as unattended automations —
    monthly VolSync spot-restores among them — and none of them runs
    yet (operations.md §4): a backup that hasn't restored recently is
    assumed broken, and until the drills run that is the standing
    assumption about every one.

## 4. Shaped patterns (the non-trivial apps)

-   **Dedicated-VIP workload (hath)**: LB Service requesting the
    dedicated VIP + a `CiliumEgressGatewayPolicy` with `egressIP` = the
    secondary private IP + a claim of its cache volume's class
    (`cloud-block-<row>`, storage.md §6) + strict CPU
    limits. All four pieces in the one component (architecture.md
    §3.2), and none of them names a node: the volume's entry declares
    that it follows the dedicated VIP, so the volume's node label and
    the VIP are on the same machine by construction. The cache volume
    and its claim are `protect=True` and moved-never-recreated
    (storage.md §3.3).
-   **Split-horizon app (immich)**: one set of pods, two exposures
    (`route` over a row whose `Exposure` is `SPLIT`, which the matrix
    maps to both gateways) — the helper emits both routes and the
    public CNAME, and the `dns` stack reads the same row for the LAN
    rewrite. The immich LAN-direct rule (never via the cloud path) is
    thereby structural.
-   **Bulk-egress (qbittorrent + seedwatch)**: pinned to the homelab
    pool; outbound v6 via the cluster masquerade, inbound v4 via the
    UDM peer-port forward, inbound v6 via a pinhole — both declared
    in the **`physical` stack** with the rest of the UniFi resources
    (the co-location exception, architecture.md §5.1: gateway rules
    follow the gateway's credential tier; this component documents
    the flows and points there); seedwatch in
    the same namespace, talking to the qbittorrent Service and the NAS
    hardlink paths. The Web UI **keeps its public entrance** (decided
    2026-08-23): split-horizon exposure — Cloudflare-proxied public
    hostname behind Authelia forward-auth on `internet-gw`, plus the
    LAN rewrite to `lan-gw` — continuing the legacy `bt.` entry rather
    than going rewrite-only.
-   **Block-volume app (hath's cache)**: an ordinary `claim` of the
    class `local-path` serves from the node volume the `physical` stack
    attached (`conventions.node_volume_class(row)` over
    `conventions.NODE_VOLUMES`), protected, with the client selecting
    the volume's node label, because a claim that waits for its first
    consumer holds no pod anywhere by itself. The provisioner hands out
    a directory inside the volume, `/var/mnt/<name>/<namespace>/<claim>/`
    — never the mount point, so a volume that failed to mount is a
    claim that stays pending, or a path missing at the pod's start
    (storage.md §6). `off` on record — the H@H network's own
    replication is the dataset's redundancy (storage.md §3.3). No
    sidecar, no privileged namespace for the application, and no
    filesystem between the app and its data.
-   **CNPG-backed app (immich, splitpro, …)**: CNPG `Cluster` on
    local-path + barman to B2, a monthly restore drill to be inherited
    from the legacy discipline (storage.md §5; nothing runs it here
    yet). **Major-version policy
    (2026-08-24)** — minors ride the normal renovate image-bump flow;
    majors use CNPG's **declarative offline in-place upgrade**
    (operator ≥1.26 — the cluster-infra.md §1 floor): bumping the
    `imageName` major shuts the cluster down, runs
    `pg_upgrade --check` then `--link`, and restarts. Constraints on
    record: our operands are self-built images (ci.md §4), so the
    new-major image carrying the *same extensions* (pg_cron, vchord)
    must be built and published **before** the deploy PR — extension
    availability for the new major is the real gate; and pg_upgrade
    requires the same distro base across the bump (a Debian-release
    change must never share a PR with a Postgres major). Major pins
    are **never automerged**: they arrive as ordinary human-reviewed
    deploy PRs, preceded by a verified-fresh barman backup — the
    barman restore is the rollback, and until the monthly drill above
    runs here it is an undrilled one.
-   **Static-site template (one component, two content sources)**: a
    single `StaticSite` component — stock static server, multi-vhost
    (one instance serves several hostnames), `route` per zone —
    parameterized by where the content comes from:
    -   **Git source** (blog: the apex/www of three zones): the blog
        repo *drives* publishing — its CI pushes the generated
        `public/` to a **built branch** (hexo's git deployer, the
        repo's original GitHub-Pages shape); the cluster side adds a
        **git-sync sidecar** pulling that branch into an emptyDir.
        Decided 2026-08-22 over a content-baked image (content is
        data, not an artifact — no image rebuild per post). Properties:
        **zero cross-system credentials** (the cluster pulls; blog CI
        holds nothing of the cluster's), push-to-live in ~a sync
        interval, rollback = git revert on the built branch, no PVC
        (truly stateless → 2 replicas across cloud nodes, ingress HA
        for the blog). A private repo would work too (git-sync + a
        read-only deploy-key SealedSecret) — noted, not currently used.
    -   **NAS source** (the "doors" instance: `door-jiahui` on the
        jiahui.love apex, `door-shiyu`, `door` — three vhosts, one
        instance): frozen low-traffic assets whose content must not
        live in a public repo (decided 2026-08-23). They are fixed
        assets, and fixed assets live on the NAS (§2): a read-only
        NAS-backed volume, pod pinned to the homelab pool, ingress
        still via `internet-gw` (cloud → KubeSpan → homelab). Editing
        = dropping files on the existing NAS share — no deploy chain
        at all, which beats both a public repo and an inconvenient
        image/hostPath arrangement.

    The **matrix `.well-known` delegation** rides the blog instance:
    `/.well-known/matrix/{server,client}` become two static files in
    the blog source repo (hexo passthrough into the built branch —
    their content is public by definition), with the CORS header added
    by an HTTPRoute `ResponseHeaderModifier` on the apex route — no
    server-config magic (the legacy shape was two nginx
    `extraConfig` fixed-responses).

    Retired with the migration: the rsync deployer + SSH pinhole, the
    hostPath webroots (blog *and* doors), kluster-code's nginx
    component, and the blog repo's rsync `deploy:` config (old-tracker
    rule).
-   **LAN-device backend (haos.ucw → HAOS)**: the public entrance to a
    device that is deliberately *not* in the cluster
    (architecture.md §6.8). Shape: a selectorless Service + manually
    declared EndpointSlice pointing at the HAOS LAN IP, fronted by an
    `internet-gw` HTTPRoute; traffic flows cloud ingress → KubeSpan →
    homelab node → LAN. This replaces the legacy VPS proxy entry and
    is the reusable pattern for any future LAN-device backend.
    Cutover note: HA's `trusted_proxies` must gain the new
    X-Forwarded-For source (the gateway pods' CIDR) at migration, or
    client IPs — and HA's login rate-limiting/ip_ban — get judged
    against the proxy address.

## 5. Porting from kluster-code

Most apps port as a rewrite of their kluster-code component onto this
contract — same images, same SealedSecrets (after the sealing-key
restore, cluster-infra.md §1), new exposure/storage declarations. The
migration order and data movement are migration.md's concern; the shape
each app lands in is this document's.

**A ported ciphertext keeps its namespace.** Every legacy SealedSecret an
application keeps is sealed namespace-wide against the legacy sealing
key, which the bring-up carries into the cluster (cluster/migration.md
§1). Its ciphertext is copied as it is into `Pulumi.apps.yaml`'s
`sealedSecrets`, under the legacy Secret's name and then its data keys,
and the component declares it through `sealed_secret`, whose default
scope is namespace-wide for exactly these. A namespace-wide ciphertext
opens only in the namespace it was sealed to, so an application keeps
its legacy namespace's name wherever a ciphertext is ported into it, as
the mail relay does; every other namespace is named after the
application it holds (`conventions.backup.BACKED_NAMESPACES`). Where the
namespace changes, the value is sealed afresh against the cluster's own
key, as Grafana's values are. These are per-application secrets, which
the program enumerates and the register does not (credentials.md §1,
rule 8).

Two quiet straight-ports, on record so they aren't forgotten: **exim**
(the MTA relay behind immich/splitpro outbound mail — a plain small
app, ClusterIP SMTP; it smarthosts via smtp.gmail.com:587, so port-25
egress reachability constrains nothing and placement is free) and
**stdiscosrv** (syncthing's discovery server — it terminates its own
TLS and its clients name it with no port; the passthrough that would
keep them as they are is refused, and how it is reached is
[Aetf/kluster-ops#723](https://github.com/Aetf/kluster-ops/issues/723)).

exim's DKIM key is the one thing of exim's that "same SealedSecrets"
does not carry. The legacy relay signs with a cert-manager key, pinned
with `rotationPolicy: Never`, which a ported `Certificate` would mint
afresh, leaving every mail zone's `k8s._domainkey` naming a key nobody
signs with. So the port carries the key instead, as the sealed value
`dkim-exim` (`conventions.sealed.DKIM_EXIM`) in the namespace the
legacy relay already uses. `credentials derived dkim-exim record`
writes it from the legacy Secret, refusing a key whose public half is
not `conventions.DKIM_K8S` (credentials.md §4). The component
declares that SealedSecret and mounts it where exim reads `tls.key`,
with no `Certificate`, and keeps selector `k8s`, so the zones' TXT and
`DKIM_K8S` never change. The key is escrowed nowhere: a lost one, or a
retired one, is replaced by a fresh key minted in the cluster under a
new selector, published beside `k8s` in every mail zone before any
mail is signed with it, with `k8s` removed once nothing signs with it.
