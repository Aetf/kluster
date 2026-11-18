# Declarative Design: the `k8s-base` Stack

How the in-cluster foundations are declared — the component set, its
install/dependency order, and the configuration points already decided
elsewhere (architecture.md for the network design, storage.md for
storage, nodes.md §4.4 for why this list is as small as it is). This is
the middle stack of [README.md](README.md) §1: everything cluster-scoped
that speaks the k8s API, consumed by `apps`.

> **Status**: designed 2026-08-22. Declared: Cilium (§2: the datapath,
> the Gateway API definitions and class, the Gateways, the pools, the
> baseline and the BGP session), the sealed-secrets controller,
> cert-manager with its issuer and certificates, CNPG and its Barman
> Cloud plugin, VolSync, NFD and the Intel GPU plugin,
> local-path-provisioner, reloader, and monitoring with metrics-server.

## 0. Scope and rules

-   Inputs: the kubeconfig, a config secret of the stack's own that
    `credentials derived sync --only kubeconfig` copies out of
    `physical`'s state, since a StackReference elides a secret across
    `physical`'s passphrase ([credentials.md](../credentials.md) §3); and,
    by StackReference, the outputs of the stacks below it that its
    components consume. Today every one is `physical`'s, asked for by its
    name in `conventions.PHYSICAL_OUTPUTS`, and they are of two kinds.
    The addresses the pools are made of: for the `internet` pool, each
    cloud node's primary private IPv4 (`node_private_ips`), each node's
    GUA (`node_guas`), the dedicated VIP's secondary private address
    (`vip1_private`) and the balancer's public IPv4 and IPv6
    (`cluster_endpoint`, `cluster_endpoint_v6`), and for the worker's
    own pool, the worker's GUA (`worker_gua`), each read so that
    anything but an address of its family stops the run naming the
    output (`kluster.lib.stack_addresses`). And the OCID of each cloud
    node's volume (`volume_ids`), which the pull of OCI's block-volume
    series names its series by (§1 item 6), read only once that pull's
    key is sealed in, and so that anything but a map from each node's
    volumes to an OCID each stops the run (`kluster.lib.stack_volumes`).
    A stack holding no
    copy of the kubeconfig, or a blank one, stops at the read and names
    that command (`kluster.lib.k8s.kubeconfig_from`). The program builds
    one Kubernetes provider from it and disables the package's default
    provider in its stack file (rfc-007 §3.1).
-   **Names: explicit for shared singletons, outputs for the dynamic.**
    Cross-stack-referenced singletons (StorageClasses, Gateways, pools,
    shared Secret names) get explicit `metadata.name`s with autonaming
    disabled — they are well-known singletons where autonaming only
    hurts (the legacy autonamed-PVC lesson) — and those fixed names live
    in `conventions`. Where autonaming is deliberately kept, the
    generated name is a machine fact and flows as a stack output. The
    point of minimizing outputs is CI: every cross-stack output widens
    the "stale downstream preview" window (ci.md §3).
-   **The component list is closed.** Every entry below pays standing
    rent (nodes.md §4.4); additions require the same justification in
    writing. Notably absent, on purpose: Longhorn (storage.md §3.2),
    JuiceFS CSI (storage.md §6), external-dns (architecture.md §6.4),
    prometheus-operator (replaced by VictoriaMetrics).
-   Namespaces belong to app components (`apps` stack); this stack
    creates only the namespaces of its own components.
-   **Every namespace this stack creates states its Pod Security
    level** in its `enforce` label. Talos enforces `baseline`
    cluster-wide and exempts `kube-system` alone, and `baseline` refuses
    the host network, the host PID namespace and host paths. So a
    namespace is `restricted`, the level workloads.md §1 gives an
    application, unless its component's pods need the host, and then
    `privileged`, with the reason in the component: the monitoring
    namespace (the node exporter's host network, PID namespace and
    paths), local-path-provisioner's (its helper pods mount the path
    they provision), and NFD's and the GPU plugin's (device and host
    mounts). What installs into `kube-system` — Cilium and the sealing
    controller — stays under Talos' exemption. A chart whose pods need
    no host and still miss `restricted` has its security-context values
    set to meet it where the chart exposes them (reloader's container
    context, empty at its pin, is the instance, and the component sets
    it), and takes `baseline`, with the reason, only where the chart
    does not (rfc-007 §8). Every such namespace is created through
    `kluster.lib.k8s.namespace`, which gives it `restricted` unless the
    declaration names a looser level with its reason (`LooserLevel`).

## 1. Install order

The order is a real dependency chain, so a single `up` converges from
an empty cluster. Between components, it is encoded by Pulumi
`depends_on` and parent relationships; inside every chart, by the
classes its objects fall into (§1.2):

1.  **Gateway API CRDs — the experimental channel** (2026-08-24: the
    ExternalAuth HTTPRoute filter, GEP-1494, ships in experimental,
    not standard — §2) — must exist before Cilium starts its gateway
    controller.
2.  **Cilium** (§2) — the cluster has no CNI until this lands (Talos is
    configured `cni: none`; nodes sit NotReady between physical
    bootstrap and this step, which is fine — CI runs the stacks
    back-to-back, and nothing else can schedule anyway).
3.  **sealed-secrets controller** — fully self-contained (generates its
    own key pair), so it comes right after the CNI and every later
    component's credentials can be SealedSecrets (§1.1). Installed as
    `sealed-secrets-controller` in `kube-system`, the name and namespace
    `kubeseal` looks for unless told otherwise, so `kubeseal` run by hand
    finds it with no flag; the sealing command names both all the same,
    from `conventions` (rfc-007 §6.1). Migration note: the legacy sealing key is restored
    into the new cluster *before* any legacy SealedSecret manifests are
    ported, or everything gets re-sealed (migration.md).
4.  **cert-manager** — ACME with Cloudflare DNS-01; the solver
    credential is a SealedSecret per §1.1 (a *separate*,
    minimally-scoped token from the one the pulumi-cloudflare provider
    uses); every later component may reference issuers.
5.  **CNPG operator** (≥1.26 — the floor for declarative offline
    in-place major upgrades, workloads.md §4), **VolSync** —
    independent of each other; both before any app declares a
    database or a backup schedule. VolSync is behind the **volume
    snapshot definitions** (`snapshot.storage.k8s.io`, from
    `external-snapshotter`), which its controller watches whatever a
    mover's copy method; they are served and nothing more, with no
    snapshot controller (storage.md §3.1). `external-snapshotter`
    publishes them only in its source tree, so they are a
    `versions:source-external-snapshotter` pin
    (framework/pulumi.md §3.2) and a component of their own,
    `components/backup/snapshots.py`.
6.  **Monitoring**: VictoriaMetrics (vmsingle + vmagent + vmalert) +
    grafana, PromQL-compatible replacement for the legacy
    prometheus-operator stack at ~1/5 the RAM (nodes.md §4.4), from the
    VictoriaMetrics stack chart, with metrics-server beside it, both in
    the one monitoring component (`components/monitoring`). **It runs
    on the homelab worker**, the node exporter apart: alertmanager
    posts to Home Assistant at Home Assistant's own address, which only
    the worker reaches ([rfc-008](../rfc/rfc-008-workload-migration.md)
    §4.1), and the rest goes with it for the worker's memory and local
    disk. **Its memory there is budgeted at 0.7 GiB** (nodes.md §4.4),
    which rfc-008 §6.1 reserves as a request. Every container the
    component runs on the worker takes its request, and its limit where
    its chart offers one, from one table,
    `components/monitoring/memory.py`, which charges each container to
    its line of nodes.md §4.4: the stack's containers to the monitoring
    line, and metrics-server to the line for the rest of kube-system,
    where that table counts it. A test holds the requests charged to the
    monitoring line to the budget: 0.65 GiB of them, leaving 49 Mi,
    against the 0.9 GiB of working set the stack measured on 2026-11-17
    under the charts' and the operator's defaults, Grafana's pod 0.3 GiB
    of it. Grafana fits by reading the
    data sources the stack renders when it starts, and starting again
    whenever what renders them changes, rather than through a second
    sidecar, and by downloading no plugin at start. **Two stores keep what is scraped**, so that every period
    compares with every other
    ([Aetf/kluster-ops#790](https://github.com/Aetf/kluster-ops/issues/790)).
    The chart's vmsingle is the *recent* store: every series, kept 31
    days. The *history* store is a second vmsingle the component
    declares, kept for good, and written only the series that measure
    what a node, etcd or a container consumes and how long an operation
    of etcd or the API server takes, beside the recording rules under
    `kluster:` and OCI's block-volume series. vmagent writes that class
    to it through a second remote write, and vmalert writes its
    recording rules through vmagent, so they reach both stores the same
    way. The history keeps inputs rather than results: a reading defined
    later is computed over every period on record. VictoriaMetrics'
    open-source release has one retention per instance and no
    downsampling, so a subset kept for good needs an instance of its
    own. Grafana reads the history as a datasource of its own. The
    history store runs at the release the chart runs the recent store
    at, pinned beside its backup and restore images
    (`versions:image-metrics-*`). Its memory, a 150 Mi request, is a line
    of its own in the same table, beside the monitoring line (nodes.md
    §4.4), and a test holds it to that line's 150 Mi. Each store's volume
    is `local-path` on the worker, named rather than left to the default
    class. **Each store is backed up hourly to the backup bucket** by
    `vmbackup`, under `vm/monitoring/<store>/` and the monitoring
    namespace's writer key, which `k8s-base` holds sealed (§1.1): one
    CronJob per store, the two half an hour apart, mounting the store's
    claim read-only. `vmbackup` copies an instant snapshot of hard links
    to the store's immutable parts, where a file-by-file copy such as
    VolSync's would catch a store merging and deleting its parts
    mid-copy. **The history's backup refuses to shrink**: it reads the
    rows the store holds and the count its last successful backup
    recorded beside the backup, and backs up nothing where the store
    holds fewer, since a store that keeps every row and holds fewer
    started without its history and would overwrite it. **Each store
    restores itself before it starts**: a container its pod runs first restores the
    backup into an empty data directory, starts an empty store only
    where the bucket holds no backup, which is the first install, and
    fails where the backup cannot be read, so a store never starts empty
    over a history that exists. What `vmbackup` and `vmrestore` do not
    do themselves, whether a prefix holds a backup at all and the
    history's row count beside its backup, is B2's native API, called
    from containers of their own in curl's image: shell, since the
    images carry no other interpreter. **No credential is a process's
    argument**: `curl` reads the headers carrying the writer key and
    B2's tokens from files in the pod's memory, and `vmbackup` and
    `vmrestore` read the key, and `vmbackup` the snapshot key, from
    their environment. A process's arguments are readable by every
    process that sees its PID, the node exporter among them, which runs
    in the host's PID namespace
    ([threat-model.md](../threat-model.md) §2.6); its environment and
    the pod's files are its own user's. What the node exporter still
    sees of these pods is their commands: the scripts' paths, the store's
    address and the bucket's, and the variable `vmbackup` expands into
    the snapshot address, none of them secret.
    Until the key is sealed in, neither the backups nor the restores are
    declared, and the stores start empty (physical/bring-up.md §1). Each
    backup job is a row of the scheduled-job family below: a failed run
    raises `MetricsBackupFailed`, and a backup older than an hour and a
    half `MetricsBackupStale` ([operations.md](../operations.md) §6.2),
    and the whole-cluster rebuild is the history's restore drill
    (operations.md §3.5).
    **OCI's block-volume series are pulled in every hour**, since only
    OCI says when a volume refused I/O to hold its performance level:
    a CronJob runs a program of this repository, which the component
    renders into a `ConfigMap`, in the `oci-sdk` image, the interpreter
    and the `oci` SDK this repository builds (`versions:image-oci-sdk`).
    It signs with the metrics reader's key, sealed in the monitoring
    namespace (§1.1), whose one statement reads that metric namespace
    of `physical`'s compartment, and every request names the namespace,
    which the statement is conditioned on. OCI names a volume by its
    OCID alone, so the pull's targets are the volumes the census
    declares, every cloud node's boot volume and every node volume on
    its node, each looked up in `physical`'s `volume_ids` output; a
    declared volume the output lacks stops the run by name. A series
    is written to vmagent as `oci_blockstore_<metric>`, labeled with
    the node and the volume's name, so both stores receive it and the
    history keeps it. Each run reads from the newest sample the history
    holds of each volume, seven days back at most, a day at a time,
    writing each day before it asks for the next, so the first run
    after a gap fills it. A metric that meets an error stops alone,
    where it stopped, and the run goes on to the next. A run fails,
    after writing the rest, where a metric stopped, where OCI answered
    with a series that names no volume, or where a declared volume got
    no sample. The pull is a row of the family too: a failed run raises
    `OciMetricsPullFailed`, and a pull older than an hour and a half
    `OciMetricsPullStale` (operations.md §6.3). Until
    the key is sealed in, neither the key's SealedSecret nor the pull
    is declared, and `volume_ids` is not read.
    **The control planes are read by readings defined once**
    (`components/monitoring/readings.py`): each a name, a window, a
    PromQL expression over the history's series, a unit, and the budget
    nodes.md §1.1–1.2 and migration.md §0 rule 6 give it, with the
    canon's phrase and the section it stands in, which a test reads
    there. Every selector names its scrape job and `cluster=""`: the
    history holds the legacy cluster's series too, under
    `cluster="legacy"`, which a sum by node, pod or namespace would
    otherwise add to this cluster's. From that one table come a
    recording rule per reading, `kluster:<name>:<window>`, which the
    history keeps by its prefix; the SLO alerts, each comparing a
    reading's record with its budget, so its threshold is the budget,
    in a rule group per resource beside the records it reads, each with
    a tier and a playbook ([operations.md](../operations.md) §8); and
    the `Control planes` dashboard, a `ConfigMap` the chart's dashboard
    sidecar hands to Grafana, with a row per resource and a panel per
    reading. A panel computes its reading's expression over the history
    store, so it draws every period the history holds, series imported
    after their minute included, under one definition; beside it, the
    same query as of an earlier time the dashboard's variable sets. The
    two readings whose inputs the history does not keep are drawn from
    their records: the API server's request latency, whose histogram is
    the largest family the store holds, and the worker's scrapes of the
    control planes, which read `up`. The load classes are nodes.md
    §1.1's, read where its table reads them: the control-plane slice is
    etcd's own series and the API server's, the controller manager's
    and the scheduler's pods; the front and the workloads are Envoy's
    pods and every pod outside the namespaces `k8s-base` places objects
    in, which a test holds to the program; and the platform is the node
    less the two. A pod is read by its own control group, apart from
    its sandbox's, and a pod's limits count while it is pending or
    running. etcd's I/O is its WAL's bytes alone, since nothing counts
    its backend's, so the slice's I/O is a lower bound; the same
    sources bound the boot volume's writes other than etcd's from above,
    which nodes.md §1.2 rule 2 budgets. KubeSpan exports no peer's
    state, so its paths are read through what crosses them: each etcd
    member's connected peers between control planes, and the worker's
    scrapes of each control plane. The node exporter's series carry
    `node` from this change's apply on, so the per-node readings start
    there.
    **The stack relies on VictoriaMetrics' open-source release
    alone**: the single-node store, vmagent, vmalert, `vmbackup`,
    `vmrestore` and `vmctl`, and the operator and its chart. A need
    the enterprise release meets with a feature is met here by design:
    a history kept for good is a store of its own rather than a
    retention filter or downsampling, and the backups are `vmbackup`
    run by a CronJob rather than `vmbackupmanager`. **The way out**
    keeps every series: `vmctl`, which `mise.toml` pins, exports them;
    the operator's scrape objects are rewritten as the Prometheus
    operator's `ServiceMonitor`s, which they mirror; the rules are
    PromQL already; and vmagent's remote writes point at the new
    store.
    Grafana is reached by a port-forward until its name moves (§3). The namespace is `privileged`, for the node
    exporter (§0). **What the chart would add is off**: the sync job,
    which fetches rules and dashboards off upstream default branches at
    install time and is a hook; the default rules and dashboards it
    would fetch; the operator's pre-delete cleanup, a second hook
    (§1.2); and the scrape jobs for the controller manager and the
    scheduler, which Talos binds to `127.0.0.1`, and for etcd, which the
    chart finds as a pod behind a Service while Talos runs it as a
    service of the host (rfc-007 §7.1). vmagent keeps the chart's
    `selectAllByDefault` and reads every VictoriaMetrics scrape object
    in the cluster. The component declares the scrape objects for
    Cilium's metrics endpoints, which are instances of this chart's
    definitions and so cannot be Cilium's. **What vmagent scrapes is
    held to what the store keeps.** Every job is scraped every 30 s.
    The chart's kubelet scrapes, the per-container metrics among them,
    label a series with its node's name and none of the node's labels:
    a node carries tens of labels, node-feature-discovery's most of
    them, and vmsingle ignores the whole row of a series past 40
    labels, so a series carrying them all is never stored. The node
    exporter's scrape labels each series with its node's name too,
    which the operator gives a pod's target no other way, since a
    reading of a node groups by it. The API server's scrape drops
    the buckets of its largest histogram families, which no reading
    uses, and keeps their `_count` and `_sum`. **etcd is scraped** on
    its metrics listener (`ETCD_METRICS_PORT`) on every control plane,
    by a node scrape the component declares, which drops etcd's
    gRPC-server and debugging families. vmagent runs on the worker, so
    its request reaches a control plane over KubeSpan, which the node
    firewall accepts ahead of every rule, and the port takes no
    opening; that holds while vmagent never runs on a control plane,
    which a test holds beside alertmanager's placement. **A row the
    store ignores raises an alert**, `MetricsDropped`
    ([operations.md](../operations.md) §6.1). **Each store's mutating
    endpoints take keys**: deleting series, taking snapshots and
    forcing a merge each take a key generated into `k8s-base`'s state,
    which each vmsingle reads from a Secret it mounts, so no rendered
    object carries one; the backup jobs read the snapshot key from it. Without them, code in any pod that reaches the store's
    Service, which includes code the installation runs and did not
    write ([threat-model.md](../threat-model.md) §2.6), can delete the
    cluster's metrics. metrics-server reads each
    kubelet without verifying its certificate, which Talos' kubelet
    signs itself, as the chart's own kubelet scrapes do. **That is
    accepted** under threat-model.md §4, on its first clause: answering
    for a kubelet takes a node address on the kubelet's port, which
    none of the actors of threat-model.md §2.6, §2.9 and §2.10 holds (a
    workload, anyone on the internet, a device on the home network): a
    call to another node's kubelet rides KubeSpan between keyed peers,
    and a call to the worker's own never leaves the host. Signed serving certificates would take a rotation
    flag in every node's machine configuration, which is `physical`'s,
    and a certificate approver on the closed list. **Alert
    delivery**: vmalert needs an Alertmanager-compatible sink — one
    small alertmanager instance stays in the stack (a 28 Mi request with
    its config reloader, earns its rent as the alerting spine). This is
    the **in-cluster half of the unified alert channel** (architecture.md §4.3): the tiers and the
    playbook convention of the CI-origin alerts, every rule carrying a
    `tier` label from `conventions.alert.Tier` and `summary` and
    `playbook` annotations, which a test resolves for every rule the
    program declares (operations.md §5). **Every CronJob a stack program
    declares is watched by the scheduled-job rule family** (operations.md
    §4). Each is a row of `conventions.schedule.SCHEDULED_JOBS`, which
    gives its namespace, its name, its period, the deadline a run is cut
    off at and its playbook, and both the component that declares the
    CronJob and the rules read that row. From each row come two
    `actionable` alerts over kube-state-metrics' series of that CronJob,
    named after the job, as `MetricsBackupStale` and
    `MetricsBackupFailed` are: the stale one once its last success is
    older than one and a half periods, and the failed one once a run
    scheduled after that success has not succeeded within the deadline.
    A row refuses a deadline longer than half its period, less the
    minute a CronJob's status takes to reach the rules
    (`conventions.schedule.REPORT_LAG`, which a test holds the scrape and
    the evaluation interval below), so a run that succeeds is never
    reported stale. A test holds every CronJob `k8s-base` and `apps`
    declare to a row, and every row to a CronJob one of them declares.
    **Every scrape job an alert reads is watched by the scrape-target
    family** (operations.md §6.4): a job that reports nothing leaves
    each alert that reads it evaluating over no series, silent rather
    than firing. `ScrapeTargetDown` fires for the job when a target of
    it fails its scrape or no target of it is listed, and `ScraperDown`
    when no target of any job reports, which holds the per-job alerts
    back. The set of jobs is derived from the alerts: the metric names
    each expression selects, a recorded series read through the
    recording rule that writes it, each read from the job its selector
    names or from every scrape job that exports it, and a name no
    scrape job exports is refused, apart from OCI's imported names,
    which the pull's own rows watch. A test holds every job an alert
    reads to one alert, and every alert to a job some alert reads. **The
    kubeconfig's copies are watched by their expiry** (operations.md
    §2.5): `k8s-base` reads when the client certificate in its own copy
    of the cluster-admin kubeconfig expires, and
    `KubeconfigCopyExpiring`, in a rule object of its own, fires from
    `conventions.cluster.KUBECONFIG_ALERT_MARGIN` before that date,
    inside the renewal window in which a `physical` apply issues a new
    certificate. It compares the evaluation's time with the date and
    reads no series, so no scrape job stands behind it.
    Alertmanager's one receiver
    is a Home Assistant webhook of its own, an automation apart from
    the one the operations repository posts to, because alertmanager's
    body is fixed: it reads the tier, the summary and the playbook off
    each alert and pushes under the same title convention. Only the
    webhook's id is a sealed value (`conventions.sealed`'s
    alert-webhook row, §1.1); the SealedSecret renders the URL in the
    cluster from `conventions.HOME_ASSISTANT`'s address and the id, and
    the receiver reads it through the operator's `url_secret`, so the
    whole URL appears in no rendered configuration (rfc-007 §7.3); until
    the id is sealed in, the receiver is skipped and alerts end at the chart's
    `blackhole` receiver (physical/bring-up.md §1). **The cluster
    checks in**: a rule that always fires, in the `heartbeat` tier,
    which the receiver repeats to Home Assistant, whose timer for it is
    the dead-man of operations.md §4. Alertmanager holds **no GitHub
    credential**; the GitHub-issue leg is *pulled* by the ops repo's
    poller reading alertmanager's API through a **dedicated
    header-match route** at the internet gateway (2026-08-24): the
    HTTPRoute forwards only `method: GET` + path `/api/v2/alerts` + an
    exact `Authorization: Bearer <token>` header match — anything else
    404s. **Read-only by method match**, no auth middleware involved.
    Accepted and recorded: the token literal sits inside the HTTPRoute
    spec (a Pulumi config secret at render time, but readable through
    the k8s API) — tolerable for an alert-list read token; the recorded
    alternative, if that ever bothers, is Authelia OAuth2
    client_credentials + ExternalAuth bearer validation. The route is
    **not declared yet**: it lands with its reader, the poller, when
    the dashboard's name moves (§3, rfc-007 §7.4). HA-delivery failure
    surfaces as a meta-alert on the notification-failure metric.
7.  **NFD + Intel GPU device plugin** — inert until the GPU cutover
    flips vfio on the homelab worker (physical/homelab-host.md §3), present from
    day 0, so the cutover needs no k8s-base change.
8.  **The small standing set the legacy cluster already proved**
    (nodes.md §4.4 counted them as "kept as-is" but this list never
    named them — explicit now so the closed list is honest):
    **local-path-provisioner** (Talos ships no default StorageClass;
    its backing directory is a Talos user volume on every node,
    physical.md §2), declared as resources of this program after the
    release's own manifest rather than installed from a chart, since
    its upstream publishes no chart repository; its namespace is
    `privileged`, because the helper pods it starts mount the path they
    provision (§0),
    **metrics-server**, **reloader**. Monitoring internals
    (kube-state-metrics, the node-exporter DaemonSet) count as part of
    the VictoriaMetrics entry. None has an ordering constraint beyond
    Cilium.
9.  **The hourly etcd snapshot** — Sidero's `talos-backup` as the
    CronJob `etcd-snapshot` in a namespace of its own, `etcd-backup`, at
    `restricted`, on the control planes (`components.backup.etcd`,
    cluster/storage.md §5). It reaches the Talos API with a certificate
    Talos issues to its `talos.dev/v1alpha1` `ServiceAccount` for
    `os:etcd:backup` alone, the grant `physical` states
    (credentials.md §1, rule 3), and uploads with the `etcd` scope's
    writer key, sealed strict as `writer-etcd` (§1.1). It is behind
    Cilium and the sealing controller, whose chart defines the
    SealedSecret. Until `writer-etcd` is sealed in, the namespace and
    the `ServiceAccount` are all it declares: the CronJob waits for
    bring-up stage 6's seal (physical/bring-up.md).

Every chart this list installs is pinned in `Pulumi.yaml`'s `versions:`
block as `versions:chart-<name>`, the Gateway API definitions as
`versions:manifest-gateway-api`, and the images a component runs beside
a chart, local-path-provisioner's two among them, the provisioner's and
its helper pod's, and the etcd snapshot's, as `versions:image-<name>`
(framework/pulumi.md §3.2). One copy:
the stack program installs from those pins, and `update_crds` reads the
same file through the same parser to render the CRD bundle
`packages/crds/crds.yaml` from exactly this chart set and regenerate the
SDK the custom resources are declared through, `sdks/crds`, from that
bundle; the legacy chart list retires with kluster-code. The
VictoriaMetrics operator's kinds are the exception: the SDK generates
them and cannot import them, so the monitoring component declares them
untyped, in a layer of its own that goes once they import
([Aetf/kluster-ops#720](https://github.com/Aetf/kluster-ops/issues/720)).
A chart pin carries where the chart is served and its version — with the
digest of its manifest where the chart comes from an OCI registry, which
Helm pulls it by — and what the regeneration needs: whether the chart
renders definitions, the values that make it render them, and the floor
its operator version has to clear with the section that states it. The
floor is checked by `update_crds` against the `appVersion` the chart
declares. Renovate moves the pins (operations.md §1), and a bump of one
the script reads is finished by running `update_crds` on its branch:
the bundle records the pins it was rendered from, and a test holds that
record to the block (framework/pulumi.md §4).

Rendering is **offline**: a pinned Helm 3 binary renders each chart
and the CRDs are filtered out of the result, so the bundle describes
the pinned chart set rather than whatever some cluster happens to have
installed. Two consequences worth naming. Cilium's chart contains no
CRD at all — the agent registers its own at runtime — so its
definitions are read from the checked-in YAML at the release tag the
Cilium chart's pin names, and a chart bump moves both. And the
VictoriaMetrics stack installs only `operator.victoriametrics.com`:
this cluster has no `ServiceMonitor` and no `PodMonitor`, so a scrape
target is declared as the VictoriaMetrics object, never as a
prometheus-operator one. Because the render never contacts a cluster,
it cannot prove the set is *complete* — a chart that creates a
definition at runtime the way Cilium does would simply be missing. The
first live `up` is what proves it.

### 1.1 Secrets placement rules

Two channels, chosen by who consumes the secret:

-   **Consumed in-cluster as a k8s Secret → SealedSecret, first
    choice** — the kluster-code model carries over unchanged (including
    the `template.data` pattern: plaintext config stays reviewable in
    git, only the sensitive fields are sealed). Examples: cert-manager's
    DNS-01 token, app credentials, VolSync restic passwords, CNPG user
    secrets.
    -   **A sealed value is a plain value in the configuration of the
        stack that declares it**, under the one structured key
        `sealedSecrets`, keyed by the value's name and then by its data
        key. The ciphertext opens with the cluster's sealing key alone,
        so it is committed in the clear beside the stack's config
        secrets; the stack program reads it and hands each component
        the ciphertext of the values it declares, and the component
        declares the SealedSecret through `kluster.lib.k8s.sealed_secret`.
    -   **The sealed values are a census**, `conventions.sealed`,
        because two programs agree on each one: the `credentials`
        command seals and writes it, and a stack declares it. Each row
        names the Secret the value becomes, its namespace, the keys of
        its data, the scope it is sealed at and the stack that declares
        it, and the configuration path follows from the row. A value
        `credentials` seals is sealed strict — name and namespace bound
        into the ciphertext, `kubeseal`'s own default — since nothing
        about it will be renamed ([credentials.md](../credentials.md)
        §1, rule 6, and §3), but for the writer keys below.
    -   **The backup bucket's writer keys reach the cluster sealed.**
        `physical` mints one per family of each namespace
        `conventions.backup.BACKED_NAMESPACES` names and keeps it in its
        state, under its own passphrase, which no other stack holds;
        `credentials derived b2-writer seal` reads each there and seals it
        into the `sealedSecrets` of the stack that declares what reads it:
        `k8s-base` for a namespace the platform holds, the monitoring
        stack's, and `apps` for every application's
        (`conventions.sealed.WRITER_KEY_STACKS`). They are the one value
        `credentials` seals **namespace-wide**: every repository Secret of
        a namespace carries its key under that Secret's own name, beside
        the repository's password, so one ciphertext serves them all
        (workloads.md §3); the monitoring stack's backups and restores read
        theirs under the row's own name. A key never becomes a configuration secret,
        which would put it in every pull request's preview.
    -   **A ported ciphertext is not a row.** A legacy SealedSecret an
        application keeps is copied as it is into `Pulumi.apps.yaml`'s
        `sealedSecrets`, under its legacy name, and the component that
        declares it is its one reader. It is sealed namespace-wide against
        the legacy key, so it opens only in the namespace it was sealed
        to, and the application keeps that namespace (workloads.md §5).
-   **Consumed by a Pulumi provider itself → Pulumi config secret**
    (passphrase-encrypted in state) — the only cases where SealedSecret
    is impossible, because the consumer is not the cluster (or the
    cluster doesn't exist yet): OCI credentials, the pulumi-cloudflare
    provider token, B2 management keys, the UDM SSH key, and the
    ZeroTier Central token — which `credentials derived zerotier record`
    delivers here from Central's own web console, as broad as the account
    it belongs to, which every Central token is, the token API's included
    (credentials.md §3). CI's own ZeroTier *member identities* are not in
    this channel: `physical` generates them into
    state, and they reach a job as an Environment secret.
-   Where one external service serves several consumers (Cloudflare),
    issue **separately-scoped tokens per consumer**: one per channel
    above, plus a third, zone-limited token for the UDM caddy's own
    ACME issuance (dns.md §4) delivered as a device secret.

### 1.2 Installing a chart: `helm.v4.Chart`

The API every component installs through, wrapped as
`kluster.lib.k8s.helm_chart`:

-   **What it is.** `helm.v4.Chart` renders the chart in the provider
    and hands each rendered object to Pulumi as its own resource. It
    does not create a Helm release, which is what `helm.sh/v3.Release`
    does instead. The trade is per-object diffs, drift remediation and
    Pulumi transforms and policies, against losing `helm list`
    visibility, release history and `helm rollback`, and the ability to
    adopt an existing release. This cluster is built from empty and
    every object in it is Pulumi's, so the losing side is empty too.
-   **OCI registries.** A chart reference may be a full `oci://` URL in
    `chart` itself, with no repository options beside it; `helm_chart`
    writes a pin's digest into that reference, and Helm refuses the
    pull when the version's tag resolves to another manifest. This is the
    wall the legacy program hit: it used `helm.v3.Chart`, which cannot
    read an OCI URL, so the Bitnami catalog's move to OCI forced
    single charts over to `v3.Release` (kluster-code#100). Private
    registries need provider ≥4.27 for in-process login; nothing pinned
    here is private.
-   **Hooks are dropped.** Any object annotated `helm.sh/hook` is
    omitted from the rendered output, test hooks unconditionally, so a
    hook a pinned chart renders is switched off explicitly rather than
    left to vanish silently. These are the ones the installed set renders
    by default. One is cert-manager's `startupapicheck` post-install
    Job, which polls a dry-run create until the API server's call to the
    webhook succeeds; it is disabled with
    `startupapicheck.enabled: false`, and what it waited for is part of
    the chart's readiness, below. NFD's post-delete cleanup is a Job with
    its own account and permissions that strips NFD's labels from the
    nodes once the chart is uninstalled; it is disabled with
    `postDeleteCleanup: false`. The VictoriaMetrics stack's sync job, a
    post-install and post-upgrade Job that fetches rules and dashboards
    from upstream, is disabled with `syncJob.enabled: false` (item 6 of
    §1). The VictoriaMetrics operator's pre-delete cleanup, a Job with
    its own account and permissions that deletes the operator's objects
    before an uninstall, is turned on by the stack chart over the
    operator chart's default and is disabled with
    `victoria-metrics-operator.crds.cleanup.enabled: false`. The provider's `includeHooks` (≥4.33) is
    not a substitute: it only writes hooks into a rendered directory for
    some other tool to apply.
-   **A chart is ready when its admission path is.** A webhook entry
    that names a Service and is rendered without a CA bundle cannot be
    called until something writes one — cert-manager's CA injector, for
    cert-manager's webhook and the Intel operator's — and until then the
    API server refuses every object the entry gates, a refusal
    `pulumi-kubernetes` does not retry. So `helm_chart` gives
    every chart a transform that sets `pulumi.com/waitFor` on each
    webhook configuration the chart renders, one JSONPath per such
    entry, and the chart resolves once every entry carries a bundle.
    Whatever is declared behind a chart is therefore declared against a
    webhook the API server can verify, and a component adds no wait of
    its own. The wait observes the configuration, not an admission call:
    each API server's own view of it is the cluster's to converge. A
    create refused because one API server has not yet observed the
    bundle fails the run by name, and the next run, its preview
    included, admits it, because everything the webhook needs already
    exists: the chart's objects are created in class order (below), so
    a run that stops anywhere leaves no webhook configuration without
    the permissions of what fills its bundle.
-   **A chart's objects are created in three classes.** Inside a chart
    the provider orders nothing beyond a custom resource after its
    definition and an object after the Namespace it is in; the objects
    of one set are created concurrently, and a workload whose pods
    cannot start holds an engine slot through its ten-minute await,
    while the first failure stops every step not yet started. So
    `helm_chart` gives every chart a Helm post-renderer,
    `kluster.lib.install_order`, which writes the
    `config.kubernetes.io/depends-on` annotation the provider turns
    into dependencies between the chart's objects. Each object falls
    into one class, by what it is and what else the render holds:
    -   **runtime**: what runs pods, a Service that selects pods a pod
        template of the same render makes in its namespace, and what
        routes API calls to a Service — a webhook configuration or an
        `APIService`;
    -   **products**: objects of a kind a definition in the same render
        defines, and a Service that selects pods no template of the
        render makes, an operator's or another installation's;
    -   **prerequisites**: everything else — definitions, accounts,
        permissions, configuration, Secrets, and objects of a kind
        another installation defines.

    Every runtime object depends on every prerequisite, and every
    product on every runtime object. A workload therefore starts with
    its accounts, bindings and definitions in place, and an operator and
    the webhook that admits its objects are up before those objects are
    created; deleting runs the other way, products before their
    operator. A webhook configuration sits beside its workload rather
    than after it, because the CNPG operator writes its own bundle at
    start and exits while either configuration is absent, and not before
    it, because the bundle await holds the configuration until its
    injector writes the bundle. The one ordering inside a class left to
    the cluster is that one: the CNPG operator may start before its
    configurations exist, exit, and be restarted by the kubelet within
    its Deployment's await.

    Objects of another installation's kinds are prerequisites because a
    chart's workload can mount what they produce — the Intel operator
    and the Barman Cloud plugin each mount the Secret of a `Certificate`
    in their own chart. A Service goes with the pods it selects, because
    the provider completes the create of such a Service only once its
    endpoints are ready. The edges are per class, so a workload also
    waits for prerequisites it does not read, none of which waits on
    anything the chart runs.

    The post-renderer reads the render's text and writes the annotation
    into it, changing nothing else, so a value that the provider's YAML
    reader and another one read differently reaches the provider as Helm
    rendered it. A reference names an object where the provider
    registers it: an object of a built-in kind that lives in a namespace
    and names none is in the chart's. An object of another
    installation's kind that names no namespace and has to be referenced
    fails the render, naming it. The annotation stays on the live
    object, and nothing in this cluster reads it.
-   **CRDs.** Definitions in a chart's `crds/` directory are installed by
    default and become Pulumi's resources; `skip_crds` opts out. Helm
    never upgrades a CRD it installed that way, which is the reason
    Gateway API is a separate install-order entry rather than something
    a chart brings along. A chart that ships its definitions as ordinary
    templates behind a value instead (cert-manager) is unaffected by
    either switch and needs that value set.
-   **`lookup` reads the cluster.** The render is a server-side dry run,
    so a template's `lookup` sees what the cluster holds. A value a
    chart generates at render time, a certificate or a password, is
    stable across runs only where its template reuses what `lookup`
    finds, and every object carrying the value, or a digest of it,
    renders the same text from what it finds as from what it drew; a
    value that is not reused is a diff on every `up`, and one rendered
    differently once found is a diff on the first `up` after the
    install. The VictoriaMetrics operator's webhook certificate reuses
    its Secret while `admissionWebhooks.keepTLSSecret` holds, which the
    monitoring component states, so it is generated once, by the first
    install. Grafana's admin password is reused but not rendered the
    same: the Deployment's checksum of it hashes a second draw on the
    install, and the found password without its quotes afterward. So
    the monitoring component generates that password itself, into its
    state, and hands the chart a Secret of its own
    (`admin.existingSecret`), with which the chart renders neither its
    Secret nor the checksum.
-   **Values may be Outputs.** Because the render happens in the
    provider rather than in the language host, a chart value can be an
    unresolved Output — the limitation that made `v3.Chart` unusable for
    anything wired to another resource. A preview whose values are
    genuinely unknown still cannot enumerate what a template branches
    on.
-   **Transformations** are the generic `transforms` resource option,
    not the chart-specific `transformations` of `v3.Chart`; a transform
    cannot change an object's name or namespace.
-   **Dependency update** exists (`dependency_update`) and is unused:
    every pin here resolves to a packaged archive that already carries
    its dependencies, the VictoriaMetrics stack included — its own
    dependencies are OCI references, and they are inside the archive.
-   **A preview needs a reachable cluster**, because the render is
    server-side dry-run. A preview of this stack cannot succeed before
    `physical` has converged once (`pulumi-kubernetes` issue 3027).
-   **The fallback, recorded.** A chart that genuinely needs its hooks
    to run, or that has to adopt objects it did not create, is installed
    with `helm.sh/v3.Release` instead — one chart at a time, in the
    component that owns it, not by moving the stack.

## 2. Cilium: the load-bearing component

All decided behavior from architecture.md §3, expressed as config:

-   **Datapath**: the installation Cilium's and Talos' guides both
    document for Talos (rfc-007 §4.1) — Kubernetes IPAM, since Talos
    assigns every node its pod ranges; the agent's capabilities without
    `SYS_MODULE`, which Talos lets no workload use; Talos' own
    control-group mount; and kube-proxy replacement on, reaching the API
    server through KubePrism (`k8sServiceHost: localhost`,
    `k8sServicePort: 7445` — mandatory, there is no kube-proxy to fall
    back on). What the design adds: `NET_BIND_SERVICE` after the
    guides' capability list, which the BGP session's instance needs to
    listen on 179 (Cilium's manual, `bgp-control-plane-configuration.rst`
    L47–52 at v1.20.1); dual-stack with IPv4 primary; tunnel
    routing over `vxlan`, whose packets are addressed node to node and so
    ride KubeSpan; the MTU of KubeSpan's link, `conventions.KUBESPAN_MTU`,
    stated rather than detected, because the agent takes the tunnel's
    overhead off the MTU it is given and KubeSpan's link is selected by a
    firewall mark, which detection would miss; and policies that leave
    default-deny off, which the baseline below is. **BPF masquerading
    and the legacy host-routing switch stay unset**: BPF masquerading
    brings BPF host routing, which bypasses `netfilter` in the host's
    namespace, where the node firewall and KubeSpan's steering both
    live, and both upstreams answer that with the legacy switch; unset,
    masquerading is the iptables implementation and host routing takes
    the host's stack. Both families masquerade — pod v6 addresses are
    internal/unroutable, so outbound v6 is SNAT'd to the node's GUA; this
    *is* qbittorrent's outbound-v6 mechanism (architecture.md §3.5).
    Metrics: the agent's, the operator's and Envoy's endpoints, and
    Hubble's flow metrics. **A pod's MTU**: its device's is KubeSpan's
    1420, and its route MTU is that less the tunnel's overhead, 1370,
    which sets the segment size a pod advertises, so TCP inside the tunnel
    needs no discovery. A pod sending to a client behind a smaller link
    gets no packet-too-big through a balancer front, which relays no ICMP
    error to a backend, so the kernel's own detection of such a path is
    on in every pod: `pmtuDiscovery.packetizationLayerPMTUDMode:
    blackhole`, which after about three seconds of silence drops the
    segment size to 1024 bytes and probes upward. It is the chart's
    default, and the values state it (rfc-008 §4.1). **A change to the
    values rolls the agent, the operator and Envoy.** The agent and the
    operator read the config map `cilium-config` when they start and not
    after, and Envoy its bootstrap in the config map
    `cilium-envoy-config`, so the values turn on the chart's
    `rollOutCiliumPods`, `operator.rollOutPods` and `envoy.rollOutPods`,
    which put a checksum of the config map each one reads on its pod
    template: a change that rewrites one replaces the pods that read it,
    and an `up` that rewrites neither replaces none.
-   **No `NodePort`** (Aetf/kluster-ops#397): whatever the datapath
    answers on a node's own address is internet-facing on a cloud
    node, because the subnet narrows no TCP or UDP port in either
    family (its list narrows only IPv6's other protocols and ICMPv6,
    security-audit.md M3) and the datapath answers a raw Service's frontend in BPF before the Talos ingress
    firewall sees the packet (physical.md §1–2). That set must be the
    declared `internet`-pool
    frontends and nothing else. hostPort and `externalIPs` are already
    forbidden (workloads.md §1), so the only frontends outside it are
    node ports, and **no Service allocates one**. Kubernetes allocates
    them to every LoadBalancer Service unless the Service sets
    `allocateLoadBalancerNodePorts: false`, and the kube-proxy
    replacement serves them on every node by default; a `lan`-pool raw
    TCP/UDP Service, whose traffic policy is `Cluster` (architecture.md
    §3.1), would then answer at a cloud node's public address. The
    Gateways' Services opt out through their `CiliumGatewayClassConfig`
    (below), a raw port's Service sets the field
    (workloads.md §1), and `type: NodePort` is on workloads.md §1's
    list of what a component may not do. Bootstrap verification: no
    Service carries a `nodePort` (physical.md §6). A `Local` Service
    still carries a `healthCheckNodePort`, which Kubernetes allocates
    whatever `allocateLoadBalancerNodePorts` says. Cilium answers it
    from a listener in the host network namespace, which the Talos
    firewall filters, so **`enable-health-check-loadbalancer-ip` stays
    off**: turned on, it adds a BPF frontend for that port on the
    Service's load-balancer address, which for the `internet` pool is a
    node's own address, ahead of the firewall. The chart's values state
    it.
-   **LB IPAM**: three `CiliumLoadBalancerIPPool`s — `internet` (the
    on-the-wire node addresses: the three primary **private** IPv4s +
    the v6 GUAs + the dedicated-VIP node's secondary private IP — OCI
    1:1-NATs a node's public v4 to its private one, so a node's public
    v4 literal would never match (architecture.md §3.2) — and the
    balancer's public IPv4 and IPv6, a class of its own that no arriving
    traffic carries and that answers, on the caller's own node, a
    connection that starts in the cluster; all from physical outputs,
    one single-address block each) and `lan` (`192.168.71.0/24` + the
    ULA /64, outside every home network and the nodes' own VLAN 7
    alike), and `worker` (`conventions.cluster.POOL_WORKER`: the
    worker's own IPv4, a convention, and its GUA, a physical output — the
    same kind of member as the `internet` pool's node addresses, for a
    Service whose traffic arrives at the worker as the node it is: the
    site's forward and pinhole end there, so a `SiteForward` port's
    Service asks for them, and no advertisement selects it). Pool
    membership via the `serviceSelector` label from `conventions`. **Every Service on the `internet` pool's node
    addresses takes `Cluster`** (architecture.md §3.1), and **a Service
    whose raw port the balancer fronts asks for the balancer's two
    addresses beside the node addresses**, so a pod reaching one of the
    cluster's public names is answered on its own node and never dials
    the balancer, whose IPv6 front answers no cluster member
    (architecture.md §3.2). Bootstrap verification: pool-contains-node-IP.
-   **BGP**: Cilium **BGPv2** resources — `CiliumBGPClusterConfig`
    (node-selected to the homelab worker only) +
    `CiliumBGPPeerConfig` + `CiliumBGPAdvertisement`; the v1
    `CiliumBGPPeeringPolicy` is deprecated and **not used** (at the
    ≥1.20 floor it may be removed outright) — peering with the UDM
    (AS 65000) over both families, advertising `lan` VIPs as
    /32 + /128. The UDM side is physical's declaration of the site's
    BGP configuration on the controller (physical/gateway.md §1.3); the
    session only establishes once both stacks are up — acceptable,
    nothing LAN-facing exists before apps deploy. As declared
    (`components/cilium/bgp.py`): the cluster configuration selects
    every node without the `node-role.kubernetes.io/control-plane`
    label Talos puts on each control plane node, which is the homelab
    worker alone, since every other node is control plane; its one
    instance is `conventions.CLUSTER_ASN` and listens on the port the
    worker's firewall opens to the gateway, and its one peer is the
    gateway's leg on the cluster VLAN, the worker's default route, at
    `conventions.UDM_ASN` — the two ASNs the gateway's FRR
    configuration names from the same constants. Both families are
    negotiated on the one IPv4 session, as the gateway's configuration
    has it. The `/128`s therefore carry the session's IPv4 address as
    an IPv4-mapped next hop, which the UDM's kernel will not route
    through, and Cilium has no next-hop setting; the UDM's inbound
    route-map on the IPv6 family replaces it with the worker's
    link-local, so the UDM routes the `lan` pool in both families.
    The agent can bind the instance's port, which is below 1024,
    because the design grants it `NET_BIND_SERVICE` (the datapath
    bullet above). The advertisement selects the Services carrying the `lan` pool label,
    so the Services the `lan` pool hands addresses to are the ones
    announced. The session is a component of its own beside the rest
    of Cilium, installed behind the sealing controller, because its
    password is a sealed value.
    **Session hardening (2026-08-23, architecture.md §4.1)**: an MD5
    session password on both ends — BGPv2's `authSecretRef` on the
    Cilium side; **placement fact on top of §1.1**: the referenced
    Secret must live in the namespace named by
    `--bgp-secrets-namespace` (kube-system by default), so its
    SealedSecret is sealed for that namespace, which the chart's values
    state from the same convention rather than inherit — and, on the
    UDM side, a line of the site's BGP configuration, which is a secret
    in `physical`'s state and rests in the controller's site
    configuration. While the password is absent from the
    stack's configuration (rfc-007 §3.2), no SealedSecret is
    declared and the session is declared all the same: Cilium then
    opens it with an empty password, which the UDM refuses, so it stays
    down until the operator's sealing sitting adds the value. The UDM's
    FRR configuration applies an inbound **prefix-list** (`192.168.71.0/24
    le 32` + the ULA /64 `le 128`, deny the rest) plus a
    `maximum-prefix` cap, and on the IPv6 family the route-map that
    sets the next hop above — without the filter, a compromised worker
    VM (or anything claiming its static IP while it's down) could
    advertise arbitrary /32s — the DNS servers' addresses included —
    and MITM the whole LAN. Verified at bootstrap by advertising a
    bogus prefix (physical.md §6).
-   **Gateway API**: enabled; Envoy is the release's per-node
    DaemonSet, on every node, and Gateway traffic goes to the Envoy on
    the node it arrives at before any backend is chosen, so where a
    Gateway answers is decided by where its address is routed, and the
    client's address reaches Envoy under either traffic policy
    (rfc-007 §5.1). Three `Gateway`s in `conventions.GATEWAY_NAMESPACE`,
    each asking for its addresses in its own `spec.addresses`, which
    Cilium's operator turns into the generated Service's
    `lbipam.cilium.io/ips`, and carrying in `spec.infrastructure` the
    labels and annotations it copies onto that Service, the pool label
    among them — `internet-gw` (the `internet` pool's node members and
    the balancer's two addresses, never the dedicated VIP), `lan-gw` (the
    `lan` pool's default VIP, which the homelab worker announces), and
    `media-gw` (same shape as `lan-gw` on a **second,
    dedicated `lan`-pool VIP** — a `conventions` literal, because
    the UDM firewall's IoT→media allow names it,
    physical/gateway.md §4.2). **Each shares its addresses with the
    census's raw Services on its front**: `internet-gw` with the
    `Balancer` ports', under `lbipam.cilium.io/sharing-key` set to the
    pool's name, and `lan-gw` and `media-gw` with the home ports' at
    their VIP, each under its own name as the key, so a raw home port
    adds no address and is never handed the other VIP. Every side of a
    share carries `lbipam.cilium.io/sharing-cross-namespace` set to `*`,
    since the raw Services live in their applications' namespaces and
    more than one may share a front (rfc-008 §4.1). Attaching a route to `media-gw` *is*
    the decision "reachable from the IoT VLAN", and the row records it
    as its `media` side (`conventions.routes`, dns.md §5), so the
    choice is on the row a reviewer reads rather than an argument at a
    call site. Apps attach `HTTPRoute`s (architecture.md §3.6 matrix).
    The Gateways share one `GatewayClass`, `cilium`, which the stack
    declares in place of the chart's own so that it can carry a
    configuration: its `parametersRef` names a `CiliumGatewayClassConfig`
    (`cilium.io/v2alpha1`, in `kube-system`) setting
    `spec.service.externalTrafficPolicy: Cluster`, which a
    configuration that sets any field imposes anyway and so is stated,
    `allocateLoadBalancerNodePorts: false`, so the Gateways' Services
    allocate no `NodePort`, and both IP families required. The
    Gateways' secret sync, which hands Envoy the certificates, stays on
    and is stated.
-   **Listeners and certificates** (rfc-007 §5.2): every Gateway has an
    HTTPS listener per **served zone** — the primary zone, served by
    definition, and every zone a route or an apex of the exposure census
    names, held or not (`conventions.routes.served_zones`, which the
    DNS-01 token's scope reads too) — on the zone's wildcard, terminating
    with the zone's certificate, and one HTTP listener whose only route
    is a redirect to HTTPS. The ports are `conventions.GATEWAY_PORTS`,
    the same structure the node firewall opens for them (physical.md §2)
    and the balancer forwards. A route attaches to the HTTPS listeners from a
    namespace carrying `conventions.routes.ROUTE_LABEL`, which
    `allowedRoutes` selects on, and from no other; the HTTP listener
    admits only its own namespace, where the redirect is the one route.
    Two more kinds of listener are derived from the exposure census,
    each only where a row asks for it (rfc-008 §4.1): an **apex
    listener** on `internet-gw` for each zone an `Apex` names, HTTPS on
    the zone's own name with the zone's certificate; and a **bare
    listener** on `lan-gw` for each `Bare` row, plain HTTP on the label,
    where the row's route redirects the bare name to the full one over
    HTTPS. Each admits routes from a namespace carrying the route label,
    and a held row's listener stands while the legacy VPS holds its name,
    since its route attaches all the same. Cilium refuses two listeners
    of one protocol on one port only where their hostnames are
    identical, which neither kind's ever is. **A `Passthrough` row is
    refused by name, by the Gateways and by `expose` alike**: Cilium
    holds a TLS passthrough listener and an HTTPS listener on one port to
    conflict whenever their hostnames can match one name, and drops both
    (`listenerPairConflict`, `operator/pkg/gateway-api/gateway_reconcile.go`
    at v1.20.1, unchanged through `main`), so a passthrough for a name
    under a served zone would take that zone's wildcard listener, and
    every name of the zone, off the Gateway. How the discovery server,
    the one such row, is reached instead is
    [Aetf/kluster-ops#723](https://github.com/Aetf/kluster-ops/issues/723).
    **One certificate per served zone, for the apex and the wildcard
    together** (dns.md §4), in the Gateways' namespace so no listener
    refers across one, issued by **one `ClusterIssuer`, Let's Encrypt
    over DNS-01**, whose Cloudflare token is the sealed value
    `conventions.sealed.DNS01_TOKEN` in cert-manager's namespace (§1.1).
    A row in a new zone is a new certificate and a new listener in the
    preview that shows the row. A renewal reaches Envoy with nothing
    restarted: the operator copies a referenced TLS Secret into Cilium's
    secrets namespace whenever it changes, and the agent pushes it to
    Envoy over SDS. Until the token is sealed into the stack, which
    needs the running controller, no SealedSecret is declared for it,
    the run warns naming it, and the issuer and the certificates stay
    pending ([physical/bring-up.md](../physical/bring-up.md) §1).
-   **Egress Gateway**: not enabled. It refuses to start without BPF
    masquerading, which on this platform brings the legacy host-routing
    switch with it (Datapath, above). Only hath would use it, for the
    dedicated-VIP pattern's outbound half (architecture.md §3.2), and
    hath's migration wave decides between it and an address of the
    node's own (rfc-007 §15.3).
-   **Route-level auth (the Authelia gate)**: the Gateway API
    **ExternalAuth HTTPRoute filter** (GEP-1494; **Cilium ≥1.20.2 —
    this sets the Cilium version floor**: the filter arrives in 1.20, and
    only from 1.20.2 does it fail closed on a target that is not
    declared, below)
    pointing at Authelia's Envoy `ext_authz` endpoint. This is how
    apps without native auth (qbittorrent Web UI, spoolman, …) get
    SSO-gated from the internet — the legacy traefik forward-auth
    middleware's successor. The gate is declared on the row, as the
    `gate` of its `internet` side behind the proxy (dns.md §5), and it
    applies to that side alone: the home has no gate, so a gated name's
    home side is answered ungated. Each form of the gate asks the portal
    at an endpoint of its own (`kluster.lib.k8s.PORTAL`): the session
    form, which redirects a browser to sign in, and the Basic form, which
    answers a challenge for a client that cannot follow a redirect. What
    the filter names — Authelia's namespace, Service, port and each
    form's path — is served by Authelia's component in `apps`, with the
    `ReferenceGrant` that lets a route in an application's namespace name
    that Service. **Below Cilium 1.20.2 a target that is not declared
    drops the gate rather than closing it**: a filter whose Service is
    missing, or whose reference no grant allows, is left out of the
    route, which is programmed with its backend and serves
    unauthenticated; 1.20.2 marks such a route invalid instead
    (cilium/cilium#47929). So **the gate's floor is 1.20.2, held at the
    pin**: the Cilium chart's pin carries that floor, and `update_crds`
    refuses a pin below it
    ([Aetf/kluster#682](https://github.com/Aetf/kluster/pull/682)).
    Authelia's Service and grant stand before any gated row does all the
    same, since a route marked invalid serves nothing. Two
    things the gate does not guard, which the portal's configuration
    holds to: the identity headers (`Remote-User` and the rest) are
    copied from its answer only where the answer carries them, and
    Authelia sets them only for a signed-in subject, so an application
    that trusts them sits under no `bypass` rule, and every route without
    a gate removes them from the request; and the first element of the
    `X-Forwarded-For` the gate hands the portal is the client's own word,
    since Envoy appends the peer it saw to what the client sent, so the
    portal's access rules and its regulation key on no client network
    while the header is forwarded this way. Bootstrap verification:
    confirm the filter **fails closed** when Authelia is unreachable
    (fail-open was reported against early builds, cilium#47178), and
    when its target is not declared — no Service, and no grant — which
    takes a different path through the operator. Because every
    gated app (qbittorrent Web UI included — its "run external
    program" setting makes fail-open an RCE) rides this one mechanism,
    fail-closed is also **verified continuously, not only at
    bootstrap**: a standing **auth canary** — a synthetic
    unauthenticated probe against a protected route, with a vmalert
    rule firing on anything but a 401/302 — so a Cilium upgrade
    regressing to fail-open pages instead of silently exposing every
    gated app, and Cilium bumps merge only with the canary green.
    (Per-app fallback auth layers were considered and rejected: N app
    configs guarding against one mechanism's failure is the wrong
    layer — harden and monitor the mechanism.) Apps with
    native OIDC (immich, grafana, matrix, splitpro) are unaffected by
    this mechanism's availability.
-   **Hubble**: enabled with relay + UI off (its flow metrics — drops,
    TCP, flows, ICMP — into VictoriaMetrics; the UI is a port-forward
    away when needed — no standing dashboard, per the standing-rent
    rule).
-   **Network policy stance** (architecture.md §4.1): default-deny is a
    per-namespace app concern; this stack ships only the cluster-wide
    baseline, a `CiliumClusterwideNetworkPolicy` selecting every pod that
    **denies egress to `169.254.0.0/16` except `169.254.116.108/32`**:
    the OCI metadata service serves the machine config, and OCI's IMDSv2
    header is static, so this policy is the only thing between a
    compromised pod and the cluster PKI (architecture.md §4.1;
    bootstrap verification in physical.md §6); the one address excepted
    is where Talos' host DNS answers pods, which the cluster DNS forwards
    to. A deny rule alone switches every pod it selects to default-deny
    egress, so the baseline sets `enableDefaultDeny` false in both
    directions and leaves default-deny to the per-namespace policies
    (rfc-007 §4.5). The baseline holds the metadata range and nothing
    else. **Blocking an application's pods from the home and the
    management plane is the application base's**: `application_namespace`
    (`kluster.lib.k8s`; workloads.md §1) declares in every application
    namespace a second policy, `egress-block`, of deny rules alone and
    with default-deny off in both directions, so a pod's egress stays open
    except toward three things. **Private space outside the cluster** is
    every RFC 1918 address and every unique-local IPv6 address
    (`fc00::/7`) outside the pod, Service and `lan` pool ranges: the site
    networks, the cluster VLAN's residents that are not nodes, the
    overlay, the cloud network's residents that are not nodes, and the
    unique-local address KubeSpan gives each node, which no declaration
    holds, wherever Cilium does not hold it as the node's; where it does,
    the rule for a node's listeners below holds it instead. **A node's
    listeners** — Talos' `apid` and `trustd`, the kubelet, etcd, the node
    exporter, Cilium's own ports — are denied by identity, `host` and
    `remote-node`, since a CIDR rule does not match an address Cilium
    holds as a node's, on every port but the API's. **The
    kube-apiserver**, at that port on a node, is denied to every pod its
    namespace does not declare as a client. A namespace's declared
    reaches and API clients are carved out of the deny rather than
    allowed over it, because in Cilium a deny
    outranks every allow, whichever policy states it; and that is why the
    block is the namespace's and not the baseline's. A per-application
    exception needs a per-application rule, and the baseline also selects
    the platform's pods, whose work is the management plane: the scraper
    reads the kubelet and the host's metrics ports, Alertmanager posts to
    Home Assistant, the controllers call the API, the cluster DNS forwards
    to host DNS. **Outside the block** stand a namespace this stack
    declares with `namespace`, and a pod on the host network, which is
    the host's endpoint (security-audit.md H1); a call from either to
    another node's management port leaves its node to a node address,
    rides KubeSpan and enters on `kubespan`, which the ingress chain
    accepts ahead of every rule, so each service's own authentication is
    all that stands in its way. A home machine's global IPv6 address
    stands outside the block too, and a pod on the worker reaches it
    through the gateway's allow from the cluster zone (physical/gateway.md
    §4.2 rule 3): it is out of the delegated prefix, which the ISP assigns
    and rotates and which only the `physical` run reads, off the gateway,
    and the block names no part of it.

## 3. What this stack deliberately does not do

-   No ingress of its own — gateways are wiring; an application's
    routes arrive with `apps`, and the balancer's listeners for public
    ports are `physical`'s (physical.md §1). A component of this stack
    may declare routes of its own, through the same `route` helper
    every application calls: the monitoring component's dashboard name
    and its alert read route are its own, not an application's. Both
    land with the wave that moves the dashboard's name from the legacy
    `mon` block (rfc-007 §7.4, migration.md §2).
-   No app namespaces, quotas, or per-app policy.
-   No application's backup schedules — VolSync `ReplicationSource`s
    are declared beside their claims in `apps` by `claim` and the
    retention classes, each with its repository Secret (workloads.md
    §3); this stack installs the controller and the backup-freshness
    vmalert rule family. The platform's own backups are this stack's:
    the metrics stores' (§1 item 6) and etcd's (§1 item 9).
-   No DNS records (declarative/dns.md).
