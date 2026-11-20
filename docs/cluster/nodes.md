# Node & Provider Selection

Objective: Pick the concrete machines for the architecture fixed in
[architecture.md](architecture.md) — which cloud provider and which instances
for the internet-facing pool, how the Homelab VM is sized, and what "high
availability" means for a two-site, budget-capped cluster. Price-driven;
baseline to beat is the legacy Vultr VPS at $30/mo all-in.

> **Status**: as decided 2026-08-22 — **cloud site = OCI, 3× A1.Flex
> (2 OCPU / 8 GB) combined control-plane + ingress nodes** (§3.1–3.2);
> homelab VM is a pure worker (§4); Vultr is the scripted fallback. All
> prices are as-of August 2026, USD, verified against official price
> pages/APIs, and must be re-verified in the provider calculators before
> commit. Both pools are declared in code — the node names and
> OCPU/RAM/boot-volume numbers of §3.1 live in `kluster.conventions` and
> the `physical` stack builds instances from them, and the same stack
> declares the homelab VM of §4 (`HomelabHost`, `components/homelab`),
> and every node of both pools runs.
> (Decision history: AWS →
> GCP (2026-04) → tentatively Hetzner (2026-08-21, voided by its
> 2026-06-15 US price hike) → OCI.)

## 1. What the cloud pool actually does

Per architecture.md §1.1 the cloud site is **three combined
CP+ingress+worker nodes**: the etcd/apiserver quorum, the Envoy that
answers `internet-gw` on each of them, the shared-VIP raw TCP/UDP
services behind the NLB,
KubeSpan endpoints, hath, held to one node by its cache's volume, and
the pods the scheduler places there, which fit the allocatable the
control plane leaves (§1.1). This means the cloud pool needs:

-   **Per node**: ~2–2.5 GB for etcd+apiserver+controllers, ~1.5 GB
    platform floor (Talos/kubelet/Cilium), leaving ~4 GB of an 8 GB
    node for Envoy, hath and the pods the scheduler places there, which
    fit the allocatable the control plane leaves (§1.1). **Two dedicated Ampere cores (2 OCPU) per
    node**: the fewest Talos documents for a control plane, two cores
    minimum and four recommended
    ([system requirements](https://docs.siderolabs.com/talos/v1.13/getting-started/system-requirements)),
    and the low end of etcd's guidance that "typical clusters need two
    to four cores to run smoothly"
    ([hardware recommendations](https://etcd.io/docs/v3.6/op-guide/hardware/)).
    The steady state needs a fraction of one core (load 0.1–0.3, CPU
    pressure 5–7%); a burst of creates, such as a first `k8s-base`
    apply, does not fit in one. On one core such a burst held CPU
    pressure at 21–45% (avg60) against 2–4% for IO, and etcd on every
    member logged `slow fdatasync` warnings, requests that took too
    long and leader changes, until requests timed out
    (Aetf/kluster-ops#780). The control plane's reservation keeps bursts
    away from etcd: the pods the scheduler places on the node fit the
    allocatable it leaves, and the kubelet weighs them below
    `/podruntime`, the group etcd runs in (§1.1; architecture.md §6.5
    for why a reservation, honestly kept, avoids the legacy
    CP-starvation failure).
-   **Stable public dual-stack IPs** per node (their on-the-wire forms —
    the v4 *private* primaries behind OCI's 1:1 NAT, plus the v6 GUAs —
    double as the `internet` pool VIPs, architecture.md §3.2) plus the
    NLB as the DNS-stable front.
-   Ability to boot **Talos Linux** — a hard filter: the provider must
    support custom images/ISOs (excludes AWS Lightsail and most managed
    VPS products).
-   **Disk**: ~50 GB boot per node, plus one attached block volume on
    the node holding the dedicated VIP (hath's cache). That overflows
    the 200 GB free block allowance by 55 GB, about $1.40 a month
    (§3.2). etcd lives on the boot volume, beside the images, the logs
    and every cloud-pool claim, so the boot volume carries a budget of
    its own (§1.2), and the bring-up reads etcd's fsync latency against
    it before any application moves (declarative/physical.md §6).

### 1.1 The control plane's reservation

**A control plane's resources are budgeted by its node allocatable.**
Its machine configuration reserves what the control plane runs on, and
the scheduler fits every pod into the rest; no class of pods carries a
budget of its own. The kubelet reserves 1000m of CPU and 0.5 GiB on a
control plane, the API server requests 500m and 1.25 GiB
(`components.talos`), and a control plane reports 1.0 core and about
6.2 GiB allocatable. The readings below support the reservation or
contradict it, and a reading that contradicts it goes to the operator
rather than moving it.

The classes below are how a control plane's consumption is read.

| Class | Members | Where its consumption is read |
| --- | --- | --- |
| Control-plane slice | etcd (a Talos service, not a pod), `kube-apiserver`, `kube-controller-manager`, `kube-scheduler` | etcd's own `process_*` series on its metrics listener, since `cAdvisor` reports no control group for it on Talos; the three pods by name |
| Platform | Talos (`machined`, `apid`, `trustd`, `udevd`), the kubelet, containerd, the Cilium agent, the node exporter, the node-feature-discovery worker, `coredns`, and any controller scheduled there | the node's totals less the other classes; each DaemonSet by namespace |
| Pods | `cilium-envoy` and every pod outside the platform's namespaces | `cAdvisor`, per pod |

**The peaks a control plane meets**: a bring-up or a rebuild, in which
every image is pulled and every object created at once; a Talos or
Kubernetes upgrade, one node at a time; a wave's land, with its images
and a claim streamed through the API server (rfc-008 §5); hath's
50 GiB warm copy; and VolSync's movers at 03:00.

**The node** is A1.Flex at 2 OCPU (two Ampere cores), 8 GB and a 50 GB
boot volume.

**CPU.**

-   **Rule: a control plane reserves the CPU that etcd, the kubelet,
    containerd and Talos run on**, outside the pods' control group
    (`systemReserved`), **and the control-plane static pods request
    what they run at.** The kubelet weighs `/kubepods` by the
    allocatable left, 39 against `/podruntime`'s 79, which keeps
    `/podruntime`, the group etcd runs in beside the kubelet and
    containerd, 1.34 cores against pods that saturate the node. Talos fixes
    its own groups' weights, so they are read after every Talos or
    Kubernetes upgrade (operations.md §2.1).
-   How the weights arise: on Talos, etcd runs in `/podruntime/etcd`,
    beside the kubelet and containerd, and the pods run in `/kubepods`,
    a sibling of `/podruntime` at the root, and the kernel divides a
    contended core between siblings by their `cpu.weight`. Talos writes
    its own groups' weights from constants, and the machine
    configuration has no field for them. The kubelet writes
    `/kubepods`' from the node's allocatable CPU, by a linear formula,
    and sets no CPU quota on it, so the reservation is enforced by
    weight alone. Weights give an idle group's share to the busy ones,
    so 1.34 cores is a floor under contention, not a cap on the pods.
-   The controller manager and the scheduler keep Talos' requests,
    50m and 10m. The API server's request is its share inside
    `/kubepods`, where a pod's weight follows its CPU request: 500m
    weighs ten times a pod that requests 50m.
-   Evidence: on all three control planes, read on 2026-11-19 and
    2026-11-20 with 200m reserved, every weight was the one the two
    formulas give: `init` 79, `system` 59, `podruntime` 79,
    `podruntime/etcd` 79, `podruntime/kubelet` and `podruntime/runtime`
    39, `kubepods` 71. At 71 the pods' group stood at parity with
    `/podruntime`, which kept 1.05 cores against saturating pods.
-   Evidence: over the seven days to 2026-11-20, the bring-up among
    them, etcd's own CPU peaked at 0.044–0.055 core and the API
    server's container at 0.107, 0.333 and 0.085 core on the three
    nodes. With 200m reserved, a control plane reported 1.8 cores
    allocatable, and the pods' requests summed to 565m, 465m and 365m;
    with the API server's request raised by 300m, each still fits the
    1.0 core left.
-   Evidence: the legacy counterparts (the table below) have 30-day
    peaks of 5-minute rates that sum to 0.49 core across the whole
    legacy pool.

**Memory.** The sizing above gives about 2–2.5 GiB to the
control-plane slice, about 1.5 GiB of platform floor, and the rest of
the 8 GB node to the pods.

-   **Rule: a control plane's kubelet evicts at 1 GiB available**
    (`evictionHard`), so the node's available memory never falls under
    1 GiB for long. It evicts workload pods by their usage over their
    requests, and never a static pod. The kubelet's other hard
    thresholds keep their defaults beside it, the space on the disk
    etcd writes to among them. The kubelet's `memory.max` on `/kubepods`
    keeps the pods as a whole off the 0.5 GiB it reserves.
-   Evidence: on the empty cluster, which does not count toward sizing,
    nodes' working sets read 1.9–2.1 GiB, kube-apiserver 1.05–1.2 GiB,
    etcd's resident set 0.13 GiB, the Cilium agent 0.33–0.6 GiB and
    Envoy 0.1 GiB (2026-11-17). Nothing contradicts the floors.
-   Evidence: the legacy counterparts have 30-day peaks that sum to
    2.1 GiB across the legacy pool, traefik included, and the pods'
    memory requests on a control plane read 0.9–1.06 GiB on
    2026-11-20.

**Disk.** §1.2.

**Network.** A1.Flex has a gigabit a second of network per OCPU, two a node (OCI's
[Block Volume performance](https://docs.oracle.com/en-us/iaas/Content/Block/Concepts/blockvolumeperformance.htm)
page, VM shapes).

-   **Rule: public and KubeSpan traffic together stay under a quarter
    of that, 62.5 MB/s, at the 99th percentile of 5-minute rates.** A
    request to a home backend crosses a cloud node twice, in from the
    internet and out over KubeSpan, so it counts twice.
-   Evidence: traefik's busiest 5 minutes in 30 days moved 7.3 MB/s,
    hath's 0.43 MB/s; doubled, that is under a tenth of the line.

**Planning.** A plan estimates by an assumed placement and says that
it assumes it. The pods a site holds (declarative/workloads.md §1) fit
that site's allocatable, and every pod fits the cluster's. The
assumption lives in the plan alone.

**The legacy counterparts of what a plan assumes on the cloud nodes**
(rfc-008 §5's assumed placement; the scheduler may place a free pod
elsewhere), from the legacy cluster's Prometheus over
the 30 days to 2026-11-17, `cAdvisor` rates over 5 minutes: CPU in cores,
memory as working set in GiB, network on the pod's interface.

| Workload (new home) | CPU mean / p99 / max | Memory mean / max | Sent / received in 30 days | Busiest 5 min sent | Disk |
| --- | --- | --- | --- | --- | --- |
| traefik (Envoy) | 0.005 / 0.020 / 0.091 | 0.07 / 0.16 | 91.2 / 94.5 GB | 7.3 MB/s | none |
| hath | 0.032 / 0.065 / 0.200 | 0.31 / 0.50 | 114.7 / 40.8 GB | 0.43 MB/s | its cache, on its own volume |
| splitpro and its database | 0.011 / 0.014 / 0.036 | 0.37 / 0.82 | 11.0 / 12.0 GB | 0.13 MB/s | 0.16 GB of writes |
| matrix | 0.000 / 0.000 / 0.003 | 0.13 / 0.19 | 0.26 / 0.23 GB | 0.01 MB/s | 0.04 GB of writes |
| legacy nginx (the blog; an upper bound, since it also serves other sites) | 0.000 / 0.001 / 0.050 | 0.01 / 0.01 | 3.4 / 0.4 GB | 0.45 MB/s | none |
| stdiscosrv | 0.001 / 0.001 / 0.003 | 0.02 / 0.03 | 1.4 / 1.1 GB | 0.01 MB/s | 0.7 GB of writes |
| authelia and its Redis | 0.031 / 0.050 / 0.108 | 0.07 / 0.40 | 1.9 / 1.5 GB | 0.02 MB/s | none |

-   **Every one of them writes a few kilobytes a second in the steady
    state**, so their claims, wherever they bind, are negligible
    beside the boot volume's 24 MB/s (§1.2). What the disk has to
    answer for is the peaks.
-   **hath's 30-day peak is 0.50 GiB**, against a mean of 0.31. Limits
    come from the 30-day peak (rfc-008 §6.1), so hath's memory limit is
    at least 0.5 GiB, which the allocatable a control plane leaves
    holds.

### 1.2 The boot volume's disk budget

**What OCI documents for the boot volume**
([Block Volume performance](https://docs.oracle.com/en-us/iaas/Content/Block/Concepts/blockvolumeperformance.htm)):

-   **Balanced, 10 VPU per GB, is a boot volume's default level**, and
    Lower Cost is not offered for one. `components.cloud.nodes`
    declares no level, so every control plane's boot volume is
    Balanced.
-   **Balanced gives 60 IOPS/GB, up to 25,000 a volume, and 480 KB/s
    per GB, up to 480 MB/s**; the formulas are IOPS/GB = 1.5 × VPU/GB +
    45 and KBPS/GB = 12 × VPU/GB + 360. **A 50 GB boot volume gives
    3,000 IOPS and 24 MB/s.**
-   **The volume binds, not the instance**: A1.Flex allows 20,000 IOPS
    and 120 MB/s of block volume throughput per gigabit a second of its
    network bandwidth, 40,000 IOPS and 240 MB/s at 2 OCPU.
-   OCI's IOPS SLA covers a Balanced volume on a `paravirtualized`
    attachment only on VMs of 8 cores or more, so on a 2-OCPU node the
    3,000 IOPS are a guarantee over iSCSI and a figure otherwise. Which
    attachment the boot volumes have is not established.
-   **hath's cache volume is Lower Cost** (`NODE_VOLUME_VPUS = 0`):
    2 IOPS and 240 KB/s per GB, 110 IOPS and 13.2 MB/s at 55 GB. Its I/O
    is outside the boot volume's budget, and the 50 GiB warm copy takes
    over an hour at that rate.

**What shares the boot volume**: one XFS filesystem, `sda4`, holds
etcd's write-ahead log and database, containerd's content and
snapshots, the kubelet's and Talos' logs, every cloud-pool claim
(`/var/mnt/storage`) and VolSync's caches.

**The budget:**

1.  **Rule: etcd's latency binds, at every moment, peaks included.**
    WAL fsync p99 under 10 ms and backend commit p99 under 25 ms over
    every 5 minutes, the targets of etcd's
    [FAQ](https://etcd.io/docs/v3.6/faq/), and no WAL fsync over 1 s.
2.  **Rule: steady writers other than etcd** — logs, claims, VolSync's
    movers — **stay under a tenth of the volume**: 300 IOPS and
    2.4 MB/s, averaged over an hour.
3.  **Rule: peaks run under the volume's throttle.** OCI's
    `VolumeThrottledIOs`, which OCI emits for every boot volume
    ([Block Volume metrics](https://docs.oracle.com/en-us/iaas/Content/Block/References/volumemetrics-reference.htm)),
    stays 0, and a peak is shaped to the write rate the reproduction
    below finds safe beside etcd, 12 MB/s. Three things set a peak's
    size: how many images the kubelet pulls at once, how many objects
    an apply creates at once, and the writes of the containers in the
    `blockio` class. The shaping below bounds the first two, and rule 5
    the third.
4.  **hath's cache I/O is on its own volume**, outside this budget.
5.  **Rule: an application's writes to the boot volume run in
    containerd's `blockio` class `etcd-disk-writer`**, which caps each
    container's write bandwidth and write operations on every whole disk
    of a control plane, the system disk among them (`components.talos`). The class is on every node, and a pod runs
    in it when its component declares the class. On 2026-11-20 no pod
    does: Aetf/kluster-ops#78's slices P2a and P2b put the pods with a
    `local-path` claim in it. The capped containers that could all be
    placed on one control plane, those held at home left out, times the
    class's rates stay within 12 MB/s and 300 write operations a second;
    planning alone holds that sum. The class fails open on a device that
    matches nothing and on a rate that does not parse, which a live read
    of a pod's `io.max` catches on the class's first apply and nothing
    catches after. VolSync's movers carry no class and are uncapped. The
    300 is rule 2's steady figure until a reading of
    Aetf/kluster-ops#785's rig beside random writes in the class
    replaces it.

**What the readings show** (2026-11-17, on the cluster a from-scratch
rebuild brought up):

-   **Steady state**: about 29 write IOPS, 33–37 in the busiest minute,
    and 0.13–0.22 MB/s per control plane, every writer included; a
    write completes in 0.84 ms on average.
-   **The bring-up**, from boot to the end of `k8s-base`'s first apply,
    86 minutes: 3.3–3.7 GB written per control plane in about 100,000
    writes, at 39–52 ms a write on average, with 60–72 requests in
    flight whenever the disk was busy. The images each control plane
    holds come to 0.58–0.71 GB.
-   **etcd's own histogram over the same run**: 4–8 WAL syncs over 1 s
    on each member, one of them over 8 s on each, and heartbeat send
    failures on two of the three. etcd's FAQ names a slow disk,
    "contention among etcd and other applications", as the usual cause
    of the latter.
-   **The disk read as busy for only 63–72 s of those 86 minutes**, so
    it moved 47–57 MB per busy second, about twice the 24 MB/s OCI
    documents for the volume. The volume does deliver that: one writer
    held 51.7 MB/s on it for two minutes (below).

**The cause is the disk path**, established by a controlled
reproduction on one control plane that was not the leader
(Aetf/kluster-ops#785, 2026-11-17). Each load ran for 120 s in a
privileged pod on `/var/mnt/storage`, the filesystem etcd's write-ahead
log lives on, beside a probe shaped like etcd's log: 2,300-byte writes,
each followed by `fdatasync`.

| Load beside the probe | Rate | Probe's `fdatasync`, p99 | etcd's WAL syncs on the member, over 1 s |
| --- | --- | --- | --- |
| None | — | 1.7 ms | 0 |
| One sequential writer | 51.7 MB/s | 325 ms | 0, and 114 over 128 ms |
| Random 4 KiB writes | 2,924 IOPS | 34.9 ms | 0 |
| An image unpack's shape: four writers, buffered, each file synced as it closes | 51.7 MB/s | 15.6 s | 8 |
| The sequential writer held to 12 MB/s | 12.6 MB/s | 1.6 ms | 0 |
| Two cores at 70% CPU, the disk idle | — | 2.4 ms | 0 |

-   **Writes at the volume's full rate stretch etcd's syncs into
    seconds, and a loaded CPU does not.** The unpack-shaped load stalled
    the probe for 15.6 s, the size of the syncs over 8 s that etcd took
    on every member during the bring-up, and the CPU load held the
    probe's p99 at 2.4 ms. The other two members, the controls, took no
    WAL sync over 128 ms in any phase, and no member saw a leader
    change.
-   **The volume's caps**: 51.7 MB/s from one writer, which four
    writers together did not exceed, twice the 24 MB/s OCI documents;
    and 2,924 IOPS, the documented 3,000.
-   **12 MB/s is the rate the volume takes beside etcd**: writes held
    there kept the probe's p99 at 1.6 ms, within rule 1's 10 ms.

**The shaping** keeps a peak's writes to that rate:

-   **Rule: a control plane's kubelet pulls one image at a time**
    (`maxParallelImagePulls`, `components.talos`). A pull unpacks
    onto the boot volume, and no reading bounds one pull's write rate
    below what one writer drives the volume to, 51.7 MB/s. So N pulls
    at once stay within 12 MB/s only where N × 51.7 ≤ 12, which no N of
    one or more meets (12 / 51.7 = 0.23); the kubelet refuses a bound
    under one, so the bound is one. `image_pull_bound` computes it from
    the two rates, `BOOT_VOLUME_SAFE_WRITE_MBPS` and
    `BOOT_VOLUME_ONE_WRITER_MBPS`, so a reading that moves either moves
    the bound. Unbounded, as Talos leaves the kubelet
    (`maxParallelImagePulls` unset), every image a burst of scheduling
    asks for unpacks at once, which is the load that stalled the probe
    for 15.6 s. What it costs is that a pod's first start waits behind
    the pulls queued ahead of it.
-   **Rule: the deploy chain applies `k8s-base` and `apps` four
    resource operations at a time** (`pulumi up --parallel`, from
    `CLUSTER_APPLY_PARALLEL` in the `env` of `deploy.yml`). In the
    pinned `pulumi` v3.274.1 the flag sets how many step workers the
    engine runs, each carrying out one resource operation at a time
    (`newStepExecutor`, `pkg/resource/deploy/step_executor.go`), and a
    workload's create holds its worker through the Kubernetes provider's
    await, until its pods are ready, their pulls included
    (declarative/cluster-infra.md §1.2). Unset, it is four times the
    CPUs Go sees, or `PULUMI_PARALLEL` where that is set
    (`defaultParallel`, `pkg/cmd/pulumi/operations/up.go`): 16 on the
    four CPUs of the
    [hosted runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
    the chain runs on (framework/ci.md §3), which is what the bring-up
    above was applied at.
-   **Four is chosen, not derived from 12 MB/s.** The pull bound's
    arithmetic counts writers, and an object in flight is not one. Of
    the 321 objects the bring-up's first `up` of `k8s-base` created, 21
    are workloads that run pods; the rest, 75 definitions among them,
    write little beyond their own object in etcd. And with the kubelet
    admitting one pull at a time on a control plane, the objects in
    flight no longer set how many images unpack there at once. What
    they still set is how many workloads start together, their pods
    queued behind that pull and their first writes beside it, and the
    reproduction rated neither. So four is chosen against that `up`:
    at 16, its workloads finished in groups of up to eight within half
    a minute of each other, and four lets fewer start together, while
    more than one keeps each workload's wait for readiness from holding
    every other object behind it. What it costs is a longer first `up`,
    that one having taken 12 minutes under the `Up` step's two-hour
    bound. A reading of what one workload's start writes is what would
    size it.
-   **One pull's own rate is beyond what either bound reaches.**
    Whether one pull at a time, with four operations in flight, keeps
    etcd within rule 1 is read at the next from-scratch rebuild
    (Aetf/kluster-ops#785); if it does not, buying disk performance is
    the operator's decision on that issue.
-   **A worker's pulls are not bounded**: its disk holds no etcd.

**The `blockio` class** holds the writes of each container in it to the
rate rule 5 sets. Kubernetes schedules no disk I/O, so the cap is the runtime's:

-   **What enforces it.** `physical` writes the class file
    `/var/lib/blockio/classes.yaml` on every node and a part of
    containerd's configuration that has containerd load it
    (`blockio_patch`, `components.talos`; declarative/physical.md §2).
    containerd puts a container in the class by its pod's
    `blockio.resources.beta.kubernetes.io/pod` annotation, and `runc`
    writes the class's caps to that container's own `io.max`. On a
    control plane the class caps every whole disk, `/dev/sd[a-z]`: the
    system disk is `sda` only while the kernel finds it first, and the
    control plane holding hath's cache volume has a second disk. Capping
    that one too stays within rule 5, since only a container in the
    class is capped and the cache volume's pod is in none. The pattern
    names no partition, such as `sda4`, which `io.max` refuses. On the
    worker it is empty, so a pod in it starts there uncapped: the
    worker's disk holds no etcd. The kernel throttles a buffered write
    when it is written back, so a pod's page cache does not take it past
    the cap.
-   **Rule: the plan bounds how many containers are capped.** Each
    container gets its own `io.max`, and nothing sets one on a pod's
    group or on `/kubepods`, so a node's figure cannot be divided at
    run time among the writers the scheduler happens to place there.
    The figure holds as the rate per container times every capped
    container that could be placed on one control plane: counted across
    the cluster, since the scheduler may stack them on one node; each
    container of a pod counted, since the pod's annotation caps each;
    containers held at home left out, since they never run on a control
    plane. A writer that would carry the product past the figures lowers
    every container's rate or keeps its data elsewhere.
-   **The rates are 0.8 MB/s and 20 write operations a second per
    container** (`conventions.ETCD_DISK_WRITER`), which 15 containers
    fit. Write operations are capped as well as bandwidth because the
    reproduction's random 4 KiB writes, 2,924 a second at only 11.4
    MB/s, under the bandwidth figure, took the probe's p99 to 34.9 ms,
    past rule 1's 10 ms.
-   **What it costs.** A capped writer is slow on a control plane: at 20
    write operations a second a database commits on the order of ten
    transactions a second, and 1 GiB written at 0.8 MB/s takes about 22
    minutes. Talos writes the class file at boot alone, so a change to a
    rate reboots every control plane, one at a time, and a change to the
    class's name or to its part of containerd's configuration reboots
    every node. And `io.max` bounds the container's own I/O alone: XFS's
    log, the container logs containerd writes, and the `emptyDir` and
    writable layer of a pod outside the class are not capped. A writer
    among them that breaks rule 1 fires etcd's fsync alerts,
    `EtcdWalFsyncSlow` and `EtcdDiskStall`
    (`components.monitoring.rules`).

**Unguarded:**

-   Nothing but the plan holds the capped containers' rates within the
    figures.
-   A class that fails open is caught by a live read of a pod's
    `io.max` on its first apply, and by nothing after.
-   VolSync's movers are uncapped.

## 2. The deciding factor is egress, not compute

Compute prices in this size class differ by ~$10/mo; egress prices differ by
~$100/mo. The legacy Vultr node quietly provided terabytes of included
transfer, and the legacy cluster leaned on that (hath via EgressGateway,
seeding). Hyperscaler pricing breaks that assumption:

| Provider path                  | Included/free egress | Overage        |
| ------------------------------ | -------------------- | -------------- |
| GCP Premium tier               | 1 GiB/mo             | $0.12/GB       |
| GCP Standard tier              | 200 GiB/mo (per billing account, ALL regions combined) | $0.085/GiB |
| AWS EC2                        | 100 GB/mo (all services/regions combined) | $0.09/GB |
| Hetzner Cloud (US)             | 1–3 TB/mo (CPX11/21/31) | $1/TB      |
| Oracle Cloud                   | 10 TB/mo (per tenancy) | $0.0085/GB   |
| Vultr (legacy baseline)        | 2–5 TB/plan, pooled account-wide, +2 TB free/account | $0.01/GB |

Provider-specific facts (verified 2026-08-22 against official price
pages/APIs; the deep-verification pass re-confirmed every number below
from primary sources — Hetzner's price-adjustment doc, Vultr's live
`api.vultr.com/v2/plans`, GCP's served pricing HTML, AWS's awsstatic
pricing feeds, Oracle's cetools price-list API):

1.  **GCP mixed network tiers are real.** Network tier is set **per access
    config**, not per VM: `--network-tier=STANDARD` for the IPv4 access
    config coexists with IPv6, whose tier "must be PREMIUM (currently only
    one value is supported)". So a dual-stack VM can put all its IPv4
    egress on Standard tier (**200 GiB/mo free per billing account across
    all regions — not per region**; $0.085/GiB after) while only the
    traffic that actually leaves over IPv6 bills at Premium ($0.12/GiB,
    ~no free tier). Since hath (~50–110 GB/mo) is v4-only and
    happy-eyeballs pulls an unpredictable share of HTTP onto v6, the GCP
    bill becomes ~$24 fixed + $0.12 × (v6 share of ~100 GB HTTP) ≈
    **$24–36/mo**. Both static v4 (Standard) and static v6 (Premium) are
    reservable; external IPv6 is free, in-use IPv4 is $3.65/mo. (Prices
    are us-central1; us-east4 runs ~10% higher, unverified.)
2.  **AWS Lightsail is disqualified** despite its bundled 1–3 TB transfer:
    it cannot boot custom images, so no Talos.
3.  **Hetzner US ≠ Hetzner EU.** The famous pricing is EU-only. US
    (Ashburn) has only the CPX/CCX lines, traffic was cut to 1–3 TB in
    Dec 2024, and 2026 brought **two** hikes (2026-04-01, then
    2026-06-15: CPX11 $6.99→$20.49, CPX21 $13.99→$37.49, CPX31
    $24.99→$73.49 — confirmed on Hetzner's own price-adjustment doc,
    ~4× vs early 2026 combined). Existing servers keep old pricing but
    **any rescale re-prices to the new list**.
4.  **Vultr's 4 GB tier is three product lines**: Regular `vc2-2c-4gb`
    $20 (80 GB SSD, 3 TB), High Frequency $24 (128 GB NVMe, 3 TB),
    High Performance `vhp` $24 (100 GB NVMe, **5 TB**). Transfer is
    pooled account-wide with an extra 2 TB/mo free per account; overage
    $0.01/GB. Reserved IPs are **$3/mo flat, attached or not** (the old
    "free while attached" rule is gone from current docs). Custom ISO
    upload free (≤10 GB, 2/account); snapshots $0.05/GB-mo. Compute
    pricing unchanged since the Jan 2023 restructure. One real demerit:
    the **Pulumi provider is a community bridge** (ediri/pulumiverse,
    lagging) — only the Terraform provider is Vultr-official.

### 2.1 Measured traffic (legacy cluster Prometheus, 30d to 2026-08-21)

The legacy VPS NIC moved 1276 GB TX/30d, but decomposed:

| Component | GB/30d | Fate in the new cluster |
| --- | --- | --- |
| ZeroTier tunnel encap (to homelab) | ~780 → **~150–250 steady** | becomes KubeSpan; free inside a bundled-TB plan (Vultr/Oracle), billed on GCP/AWS. Re-measured 2026-08-22 at daily granularity: quiet days are TX 2.5–10 GB / RX 5–14 GB; the 780 was migration one-offs (08-19 alone: 592 GB, the dav initial copy; 08-12: 90 GB; 08-21: 50 GB). Steady-state cross-site ≈ 150–250 GB/mo per direction — and the new design removes the shared-JuiceFS churn component (syncthing cross-site deltas remain, but they're document-sized). Re-measure over a quiet month before any hyperscaler commitment. |
| immich copytest + JuiceFS mount pods | ~500+ | one-off August migration traffic + JuiceFS churn; gone by design |
| traefik (real public HTTP serving) | **~96 TX / ~98 RX** | cloud node Envoy |
| hath (recent pod rate ~3.6 GB/day) | **~50–110 TX** | cloud node (stable IP) |

**Steady-state true public egress ≈ 150–220 GB/mo.** Design rules
(reflected in architecture.md §3.4):

-   **Bulk-egress workloads (qbittorrent/seeding) are pinned to the
    Homelab pool and egress directly via the home uplink**, never
    through a metered cloud path. **A roaming device's sync crosses
    one, by the operator's ruling**: no syncthing replica runs in
    the cloud, and the balancer's peer port reaches the NAS instance on
    the worker, so a device away from home syncs across the cloud's
    metered egress, inside the 10 TB allowance (migration.md §2, Wave
    A2).
-   **hath is the only workload requiring a stable public IP** (inbound port
    and outbound on the same IP; the dedicated-VIP pattern,
    architecture.md §3.2). Its ~50–110 GB/mo fits trivially inside the
    10 TB allowance, and its 50 GB cache rides a 55 GB block volume on
    the same node the VIP does (§3.2).

## 3. Provider comparison (cloud worker, dual-stack, Talos)

Monthly cost = instance + boot disk + IPv4 + egress at three traffic points.
Instance prices are 1-year committed/reserved where available (3-year rates
are ~30% lower still; defer committing until the design has run for a few
months).

| | GCP e2-medium (mixed tier) | AWS t4g.medium | Hetzner CPX21 (US) | Vultr 4 GB | Oracle A1.Flex (free) |
| --- | --- | --- | --- | --- | --- |
| Specs | 2 shared vCPU / 4 GB | 2 vCPU ARM / 4 GB | 3 vCPU / 4 GB | 2 vCPU / 4 GB | ARM, 2 OCPU / 12 GB, in every tenancy |
| Instance (1y commit) | $15.41 (3y: $11.01) | $15.40 (1y RI no-upfront) | $37.49 (no commit) | $20 vc2 / $24 vhp-5TB (no commit) | $0 |
| Boot disk 50 GB | $5.00 (pd-balanced $0.10/GiB) | $4.00 (gp3 $0.08/GB) | included (80 GB) | included (80–100 GB) | free (within 200 GB boot+block allowance, home region) |
| Public IPv4 | $3.65 ($0.005/hr) | $3.65 ($0.005/hr, idle same) | $0.60 | included | $0 (reserved & idle also $0; 50/region limit) |
| Fixed subtotal | **$24.06** | **$23.05** | **~$38.09** | **$20–24** | **$0** |
| @ 220 GB egress (steady state, §2.1) | $24.06 + $0.12 × v6-share ≈ **$24–36** | ~$33.85 | $38.09 | $20–24 | $0 (10 TB incl.) |
| KubeSpan inter-node TX (~150–250 GB/mo steady, 600 GB/day spikes) | metered on v4 Standard — competes with public traffic for the 200 GB allowance | metered $0.09/GB ⇒ +$15–25 steady, spikes billed | included | included (2–3 TB pool) | included |
| Talos support | image upload | AMI upload | no ISO — rescue-mode dd / packer snapshot | **native custom ISO** | custom image (.oci qcow2), official guide |
| Dual-stack, stable IPs | static v4 (Standard) + v6 (Premium) reservable | EIP + stable v6 | primary IPs detachable, v6 /64 free | reserved IPs (v4 + v6) | reserved v4; v6 on dual-stack VCN |
| Additional IP (VIP fallback) | static IP + alias/forwarding rules | EIP re-assignable | floating v4 $3.50 / v6 $1.50 | reserved IP | reserved public IP |
| Risk notes | v6-share billing unpredictable; CUD lock-in | KubeSpan metering kills it | 2 hikes in 18 mo; US traffic cut to 1–2 TB | legacy incumbent; pricing stable through 2024–2026 | **platform risk**: capacity hunts, 2026 free-tier halving (to 2 OCPU/12 GB, in every tenancy), reports of account purges; PAYG conversion mitigates |

The KubeSpan row matters more for its *variance* than its magnitude:
steady-state cross-site TX is ~150–250 GB/mo (re-measured 2026-08-22,
§2.1), which on AWS adds ~$15–25/mo and on GCP eats most of the
Standard-tier free allowance — but single migration/repair days have hit
600 GB, and on a metered provider every future data move, restore drill,
or resync becomes a billing decision. A bundled multi-TB pool (Vultr,
Oracle) makes cross-site traffic architecturally free, so the design
never has to think about it.

### 3.1 Decision (2026-08-22): OCI selected, Vultr as scripted fallback

Post-hike landscape at measured traffic (220 GB public + ~150–250 GB
KubeSpan TX steady state, with multi-hundred-GB spike days):

-   **Hetzner CPX21 US $38/mo**: no longer competitive; also no native
    ISO and a worrying pricing trend. Dropped.
-   **AWS**: ~$34 + ~$15–25 KubeSpan metering + billed spikes ≈ $50+.
    Dropped.
-   **GCP mixed-tier**: base $24, but public v4 + KubeSpan together
    overflow the 200 GB Standard allowance and v6 share bills at Premium
    — realistically $35–45 and unpredictable. Dropped as primary.
-   **Vultr 4 GB (~$24/mo)**: bundled 2–3 TB covers public + KubeSpan
    traffic with headroom; native ISO Talos install; mature
    pulumi/terraform provider; and it is the *legacy incumbent* — the
    original "$30 baseline to beat" is beaten by simply right-sizing the
    plan on the same provider, with zero provider-migration risk.
-   **Oracle A1.Flex under PAYG (free, 10 TB egress)**: the risk profile
    changes materially once the tenancy is converted to Pay-As-You-Go
    with a card on file (one-time $100 verification hold, refunded),
    *while still paying $0* for Always-Free-shaped usage:
    -   **The A1 allowance is the same in every tenancy, PAYG
        included.** Oracle's Always Free page reads: "All tenancies get
        the first 1,500 OCPU hours and 9,000 GB hours per month for free
        for VM instances using the VM.Standard.A1.Flex shape"
        ([Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)).
        That is about 2 OCPU / 12 GB always on, and the page draws no
        line between free and paid tenancies for A1. PAYG is billed (not
        killed) beyond it, and the fleet runs beyond it (§3.2, the
        node-shape math).
    -   PAYG tenancies are **exempt from the idle-reclaim policy** (the
        documented 7-day/95th-percentile-CPU rule targets Always Free
        accounts).
    -   The reported no-warning account purges cluster heavily on
        free-only tenancies; a paying account has normal customer
        standing.
    -   The failure mode inverts: on a limits change, a PAYG tenancy gets
        **billed instead of killed** — a survivable surprise, bounded by
        billing alerts, versus a terminated node.
    -   PAYG also opens *paid headroom on the same node*: extra A1 OCPUs/
        RAM and block storage beyond the free 200 GB are metered cheaply
        if ever needed.
    -   Residual risks that don't go away: A1 **capacity hunts apply at
        (re)creation time** (a running instance is safe; replacing it in
        a full region may take days), Oracle's demonstrated appetite for
        unilateral term changes, and ARM-only (fine for Talos and
        everything in the current workload set; hath is Java, syncthing
        Go — both multi-arch).

**Decided: OCI A1.Flex under PAYG, as the 3-node combined pool of §1.**
The deciding observations: (a) the §4.4 analysis shows cloud-node cost
is dictated by the fixed platform floor, which cheap 8 GB A1 shapes simply
delete as a concern; (b) A1 billing's fungibility turns the free/cheap
allowance into *three* nodes — buying CP HA and ingress HA that no paid
single-instance alternative offers at any similar price (§3.2); (c)
this cluster's Tier-0 posture (§5: everything declarative, a rebuild
the drill program covers, backups off-provider) is precisely the design
that makes OCI's platform risk survivable — the Vultr fallback (vhp
4 GB, $24, single node, ingress-only + homelab CP per architecture.md
§6.5's superseded layout) is a stack config flip plus a DNS diff; the
yearly offline day previews the flipped stack config to prove it still
computes (operations.md §4 — designed; no offline day has run).

### 3.2 OCI deep dive: commercial model, service landscape, gotchas

Verified 2026-08-22 against Oracle's price-list API (cetools, the cost
estimator's backend, data stamped 2026-08-14), the billing guide, the
July 2026 Pillar SLA document, and the Always Free docs. Uniform list
pricing across all commercial regions (no US exception found).

**Commercial model — PAYG is the only sensible one at this scale.** The
alternatives are commit models: Annual Universal Credits (12-month
prepaid pool, unused credits forfeited, discount schedule not public)
and Monthly Flex (subject to Oracle approval). The old startup-credit
program no longer exists on the official page. PAYG specifics:

-   Billed monthly in arrears; $100 one-time card authorization at
    upgrade; the $300/30-day trial credit survives conversion.
-   **Support is included** in service fees on PAYG (no 3–10%-of-spend
    surcharge like other clouds); Always-Free-only tenancies get no
    ticket support.
-   **Paid usage carries financially-backed SLAs** (compute single
    instance: 99.9% monthly uptime → 100% credit below it; Object
    Storage 99.9%); Always Free resources explicitly carry **no SLA**.
    One more concrete thing the $0-but-PAYG posture buys.
-   **There is no hard spending cap.** Budgets are alerts only,
    evaluated every ~24 h. The real guardrails are resource-side:
    **compartment quotas + service limits** pinning creatable shapes to
    the envelope the design is budgeted against, plus budget alerts.
    Accept this consciously — "billed, not killed" means the bill is the
    failure mode. The quotas
    bound A1 twice, per availability domain and per region, and both
    hold exactly the fleet: one node's worth per AD, the fleet putting
    one node in each (§5), and the fleet's worth across the region. A
    replacement is launched only once the node it replaces is
    terminated (operations.md §3.1), so the cloud refuses a node beyond
    the fleet, a replacement launched beside the node it replaces among
    them, whoever asks for it. The guardrails
    component (`components/cloud/guardrails.py`) holds the numbers and
    their reasons, and its tests hold them against the fleet's declaration.

**Free-envelope facts that matter to this architecture** (beyond the A1
shape itself):

-   Public IPv4: $0, including reserved *and idle* (50/region limit).
    IPv6: **/56 GUA per VCN**, $0. VCN/IGW/NAT/service gateway/S2S VPN:
    all $0. In-region transfer (node ↔ Object Storage): $0.
-   Block storage: **200 GB total (boot + block, home region)** free +
    5 volume backups. Beyond: $0.0255/GB-mo (+$0.017 at Balanced VPU).
    **The tenancy's footprint exceeds this by design**: OCI's minimum
    boot volume is 50 GB, so three A1 boots + the state-backend micro's
    boot + the one attached volume (55 GB, for hath's 50 GB cache) =
    **255 GB**, i.e. ~$1.40/mo at VPU 0. Accepted rather than designed
    around — folding the cache into a boot volume would cost it the
    independent-volume boundary storage.md §3.3 relies on.
-   **Bastion service: free** — an out-of-band SSH path to the node
    when the cluster/KubeSpan is down. Vault software keys + secrets:
    free. NLB ×1 + 10 Mbps flexible LB ×1: free (unused by the Cilium
    design, but there).
-   **OCIR** (container registry): billed as Object Storage (first
    10 GB free) — a natural same-region home for homelab-containers
    images consumed by the cloud node.
-   Monitoring/Notifications free tiers are huge (500M datapoints/mo,
    1M HTTPS deliveries) — good for an **out-of-band alert channel**
    (MQL, not PromQL; no substitute for VictoriaMetrics).
-   Email Delivery: 3,000 mails/mo free via port 587 (approved-sender
    setup required); **outbound port 25 is tenancy-blocked by default**.

**Paid rates around the free envelope** (for headroom math): A1
$0.01/OCPU-h + $0.0015/GB-h; preemptible capacity is a flat −50%;
E4 $0.025/OCPU-h. Egress beyond 10 TB: $0.0085/GB (NA). **Compute
capacity reservations exist** (unused capacity bills at 85%) — a priced
hedge against the A1 re-creation capacity hunt at worst-case ~$61/mo
for the fleet's 6 OCPU / 24 GB; not recommended (the Vultr fallback is
cheaper insurance), but it exists if OCI ever becomes load-bearing.

**The node-shape math (accepted posture: "we always pay something",
budget $50/mo).** A1 billing is *fungible*: OCPU-hours and GB-hours
accrue per tenancy across all A1 instances, the free 1,500 OCPU-hours
and 9,000 GB-hours subtracted from the totals — shapes are freely
splittable across nodes. At 744 h, the longest month, the marginal
prices are **$7.44 per always-on OCPU** and **$1.116 per always-on
GB**, and the allowance covers about 2 OCPU and 12 GB always on. Each
month below includes the block storage past 200 GB, $1.40:

| Shape | Total | OCPU past the allowance | Memory past the allowance | Month |
| --- | --- | --- | --- | --- |
| **3 × (2 OCPU / 8 GB)** — as designed | 6 OCPU / 24 GB | $29.64 | $13.28 | **≈ $44.3** |
| 3 × (2 OCPU / 6 GB) | 6 OCPU / 18 GB | $29.64 | $6.59 | ≈ $37.6 |
| 3 × (1 OCPU / 8 GB) | 3 OCPU / 24 GB | $7.32 | $13.28 | ≈ $22.0 |

One OCPU a node is below Talos's control-plane minimum and does not
carry a burst of creates (§1). Six GB a node would halve the ~4 GB that
§1 leaves each node for workloads. The budget alert
(`components/cloud/guardrails.py`) is set at $50: the designed month
rounded up, the remainder being room for what a month adds beyond the
fleet as declared, egress and request counts among them.

What the three-way split buys over one big node — each a standing
benefit, so the ~2× platform-floor overhead (3 × ~1.5 GiB instead of 1)
and two more Talos nodes to patch pass the §4.4 standing-rent test:

-   **Control-plane HA** (etcd quorum, architecture.md §1.1) and
    **public-ingress HA** (all nodes behind the free NLB) in one move.
-   **Maintenance without downtime of anything** — drain/upgrade one
    node while two serve; quorum holds at 2/3.
-   **A continuous capacity hold**: the practical mitigation for the A1
    re-creation hunt is never needing to create more than one node at
    once; losing one leaves quorum and ingress up while the replacement
    hunts for capacity.

**Gotchas registry**: new-tenancy service limits are low and free-tier
limit increases are often refused (PAYG requests go through); Always
Free resources are mostly home-region-locked and the home region is
immutable at signup; the A1 free allowance is measured in OCPU-hours
(1,500/mo in every tenancy, §3.1), so a
deleted-and-recreated instance burns the same pool; watch the
31/90-day minimum-retention rules if Object Storage IA/Archive tiers
ever get used.

## 4. Homelab node(s)

The Homelab side runs on the existing physical server under libvirt
(pulumi-libvirt; the VM's concrete disk/network/GPU shape is
physical/homelab-host.md). Sizing is
derived from a host inventory, not guessed.

### 4.1 Host inventory (measured 2026-08-22, Aetf-Arch-Homelab)

**Hardware**: i9-12900KS (16C/24T) with its UHD 770 iGPU, an Arc A380
discrete GPU (added 2026-10-18), **32 GB RAM** (the binding
constraint), and storage on two NVMe disks and the JBOD — this host
*is* the NAS.

**Storage** (read 2026-11-17), with what each is for in the new
cluster:

-   **The root btrfs**, on the main partition of a Samsung 980
    (500 GB, DRAM-less), beside the EFI partition and swap; 216 GiB
    free. It holds the host's own system, Home Assistant's disk image,
    and the **worker's disk**: a sparse raw file in the `nodatacow`
    subvolume `/var/lib/libvirt/kluster` (physical/homelab-host.md §1).
    For the new cluster it is the worker's whole local disk: every
    partition Talos makes is in that file, EPHEMERAL carrying the
    worker's `local-path` claims and container images, and the file
    grows into this filesystem's free space (§4.2). Every other
    writer on the host shares the disk (builds, image pulls, block-group
    relocation), and a DRAM-less drive's write latency climbs under that
    load, so the legacy cluster's latency-sensitive state sits on
    `fast` instead.
-   **`fast`**, a single-disk ZFS pool on an 860 GiB partition of a
    Samsung PM983 (960 GB, with DRAM and power-loss protection). The
    rest of that disk is a cache device (L2ARC) for `nas`. The pool's
    datasets `fast/etcd`, the legacy server's embedded etcd, and
    `fast/storage`, the legacy cluster's `local-path` volumes, serve the
    legacy cluster. **The new cluster has nothing on it.** A legacy
    claim bound for the worker is copied from it onto `nas` at its move
    (migration.md §3), and what the pool is for once Wave F has removed
    those two is not decided (Aetf/kluster-ops#803).
-   **`nas`**, a ZFS pool of eight HDDs in the JBOD, in two raidz2
    groups of four disks, one of 16 TB disks and one of 14 TB. For the
    new cluster it is the bulk tier: media, photo originals and the
    other fixed assets, served to the worker over NFS on the host leg
    (physical/homelab-host.md §5), and never a database
    (declarative/workloads.md §2).

**Stays on the host** (never enters the cluster):

| What | Why it stays | RAM budget |
| --- | --- | --- |
| ZFS + NFS + Samba serving | the NAS role; the cluster consumes it | ZFS ARC (in-RAM read cache) ≥4 GiB (read 2026-11-17: at its floor, about 1 GiB) |
| HAOS libvirt VM (2 vCPU / 4 GiB, PCIe USB3 + Wi-Fi/BT passthrough) | home automation must survive cluster outages; architecture.md §6.8 | 4 GiB |
| AdGuard alice/bob run as nspawn containers **on the UDM**, not here; the host's adguardhome-sync **retires** once Pulumi dual-writes both instances (declarative/dns.md §3) | LAN DNS lives on the gateway and must survive cluster (and this host's) outages | ~0 |
| The *legacy* Pulumi state-backend Postgres (podman) — serves kluster-code until decommission; the new cluster's backend lives on an OCI micro (framework/ci.md §1) | a state backend cannot live inside the cluster it manages | ~0.2 GiB (until Wave F) |
| zerotier, sshd, apcupsd, smartd, syslog-ng, small relays, Claude sessions | host plumbing / management path. The relays: `samsung-tv`, between Home Assistant and a television on the Default LAN, neither end the cluster's; and the **heard relay** of the `jellyfin-discovery` row, a unit whose `socat` hears discovery broadcasts on each home VLAN and sends each as a unicast to the row's port at the media VIP. The row, a `Heard` side of its `Port`, is its declaration, and the host belongs to no stack, so the unit copies the row's port, group and VIP by hand, and each window's verification holds the copy | ~1.5 GiB |
| thread-dashboard | operator ruling: its HomeKit names need multicast on the IoT bridge, which no pod has. Its name is a device row at `lan-gw`, served through a device relay on the worker that reaches the host's leg on the cluster VLAN | 35 MB |
| dmarc-check timer (claude-driven DMARC triage) | uses the host's claude credentials (an in-cluster CronJob would need the API key sealed); revisit after cluster stabilization — a different approach may supersede it | ~0 |

**Moves into the cluster**: everything currently in legacy k3s on this
host (requests 12.5 GiB, actual ~16 GiB including k3s overhead:
immich+CNPG, jellyfin+Shoko, prometheus+grafana, syncthing-nas, hath is
cloud-bound, …) **plus the natives that only stayed out for platform
reasons**: qbittorrent-nox (blocked on IPv6, solved by architecture.md
§3.5) with seedwatch alongside it (it drives the qbittorrent API and
reads NAS hardlink counts — both reachable in-cluster). distccd is
retired or containerized opportunistically.

**GPU** (2026-10-19): legacy workloads hold 3× `gpu.intel.com/i915` allocations
(immich ML/transcode, jellyfin), all on the host's Arc A380, to which
the legacy device plugin is limited. Plan of record: VFIO-passthrough
the A380 (`0000:03:00.0`, alone in its `iommu_group`; its HDMI audio
function stays with the host) into the Talos VM + Intel device plugin
in the guest, while the UHD 770 iGPU stays with the host as its BIOS
primary display; fallback is CPU transcode at reduced quality-of-life.
The bind is a scheduled migration cutover with no probe ahead of it,
since passthrough is known to work on this host
(physical/homelab-host.md §3 has both devices and the reasons,
migration.md Wave C the window).

### 4.2 VM sizing

-   **Worker VM** (pure worker — the control plane lives cloud-side,
    architecture.md §1.1): **12–16 vCPU**, its RAM grown to fit (below),
    disk on local NVMe. The host has 31 GiB. Read on 2026-11-05 before
    the worker started, 12.3 GiB of it was available, with Home
    Assistant's machine at 4.1, legacy k3s's server at 2.6 and its pods
    at 5.6; the worker **boots at 10 GiB**, leaving about 2.3. What it
    can grow by after that is what legacy releases, less a margin of
    1 GiB. **Two ceilings follow**:
    -   **Transitional, while the migration waves run: about 16 GiB**,
        about 14.5 of it allocatable to workloads, reached at Wave C's
        window with legacy's applications stopped. The legacy server and
        what stays on it — its system pods and the idle CI runners a
        neighboring project runs there, about 3.6 GiB together — stay
        resident. The runners are the assumption: one under load has
        taken up to 6 GiB, so the ceiling holds only while that
        project's dispatching stays paused, as the operator has ruled
        for the waves, or the runners are capped through Wave C's window
        and soak.
    -   **End state, after the decommission: about 19 GiB at most**,
        once legacy releases the rest. That is a ceiling and not a
        target: the end-state size is the decommission's decision
        (migration.md §4), against what the host keeps for the
        operator's own work beside the cluster.

    **The worker grows to fit, not to absorb**: a drained window grows
    it by what the next wave's requests and their headroom need, capped
    by what legacy has released, and whatever legacy releases beyond
    that stays with the host (migration.md §0 rule 4). One window does
    it, Wave C's, from 10 GiB to about 16. The workload set fits under
    the transitional ceiling because requests are sized from what each
    application runs at, and limits from its 30-day peak (rfc-008 §6.1):
    the requests take less than half of the allocatable, and every peak
    at once comes to about the allocatable itself. **A RAM upgrade
    remains the relief valve** (board verified: ROG Maximus Z690 Hero,
    DDR5, 4×DIMM up to 128 GB), and the choice between it and tighter
    limits is the operator's if Wave C's reading departs from that
    sizing.
-   **Addressing**: the worker sits on the **dedicated cluster VLAN**
    (id 7, `192.168.70.0/24`) rather than the host's untagged server
    LAN, at the static address **`192.168.70.10`** — the network shape
    and the reasons for it are physical/homelab-host.md §2.
-   **Disk**: target 100+ GB, a sparse file on the root btrfs
    (concrete shape — raw, on a nodatacow subvolume, virtio-blk —
    physical/homelab-host.md §1). It stays at the 60 GiB it is created
    at until Wave C's window grows it to the target. Its length is
    bounded by what the root filesystem can back in full: §4.1
    inventories that filesystem, and physical/homelab-host.md §1 states
    the reading a growth is checked against. (No etcd on this VM; its
    disk constraint is ordinary workload I/O.)
-   **Optional second worker VM** (2–4 vCPU / 8 GB): **not in the
    initial build**, deferred with criteria in §4.3. RAM is why it
    waits: the end-state ceiling above is already the whole of what
    this host can spare. The design must not assume it exists.

### 4.3 One big VM, and the deferred second worker

Decided 2026-08-22 (was an open question): a single large VM. On one
physical host, multiple VMs add no real fault isolation, but each costs a
fixed overhead (kubelet + Cilium + OS ≈ 1 GiB per node) from a 32 GB
budget that has none to spare. That argument settles the shape of the
initial build — one big VM, not N small ones — and it also sets the
terms on which a second one could ever join, which is the question a
reader arrives here with.

**The second worker VM is deferred as of 2026-08-22**, at the §4.2
sizing (2–4 vCPU / 8 GB):

-   **Trigger**: the RAM upgrade lands (§4.2's relief valve — ROG
    Maximus Z690 Hero, 4×DIMM DDR5, up to 128 GB) **and** one of the
    two use cases below is real. Both, not either: at 32 GB the end-state
    worker plus the host's own tenants spend the budget (§4.1, §4.2), so
    a second VM has no memory to run in no matter how good the reason.
-   **What it buys** — the two use cases that survive the
    no-fault-isolation argument: **same-site storage replicas**, which
    is the standing precondition for Longhorn replica=2
    (storage.md §3.2), and **draining the homelab worker** without
    evicting its workloads out of the site.
-   **What it does not buy**: survival of the physical host. Two VMs
    share one machine, one NVMe, one power feed — replicas across them
    are availability during planned work, not durability. Durability is
    Tier 0 either way: declarative rebuild plus backups (§5).
-   **Consequence for the design**: nothing may assume it exists. A
    feature that only works with two homelab workers is itself a
    deferred item with criteria, not a plan.

### 4.4 Infrastructure tax, measured, and the economy program

The "how much of the machine is control plane / infra?" question,
answered with `kubectl top` on the legacy cluster (2026-08-22), of
~16 GiB in use on the homelab node:

| Infra component | Measured | Fate in the new cluster |
| --- | --- | --- |
| k3s server + containerd (host procs) | ~1.7 GiB | CP moved cloud-side; the homelab VM keeps only kubelet+containerd (~0.5 GiB). The CP cost re-appears as ~2–2.5 GiB × 3 on the cloud nodes — paid out of OCI RAM, not out of the 32 GB host |
| Monitoring (prometheus 948 Mi, grafana 428 Mi, exporters/operator/alertmanager ~200 Mi) | ~1.6 GiB | **switch to VictoriaMetrics** (vmsingle + vmagent + vmalert): PromQL-compatible, typically ~1/5 the RAM at this scale; grafana stays. Target ≤0.7 GiB, held as requests on the worker: 0.65 GiB of them (`components/monitoring/memory.py`), against 0.9 GiB of working set measured 2026-11-17 under the charts' defaults. metrics-server, which the monitoring component installs beside it, is charged to the kube-system row below |
| Monitoring's history store (none on the legacy cluster) | — | **a line of its own**, the second vmsingle that keeps the comparison's series for good (declarative/cluster-infra.md §1 item 6): a 150 Mi request beside monitoring's ≤0.7 GiB, its own line of `components/monitoring/memory.py`. Derived from the recent store's 225 Mi at 171,229 series, read 2026-11-17: the history holds 5,000–6,000 of them, so what it takes is the process's floor rather than its series' |
| Shared-JuiceFS stack (CSI ×5, redis, 4 mount pods) | ~1.0 GiB | gone by design: no CSI driver, no shared metadata store, and no workload mounts JuiceFS (storage.md §6). A workload admitted later brings its own sidecar mount, **budgeted honestly at 0.5–1 GiB requests** — root cause (b) of the legacy instability was starving exactly this process |
| Everything else in kube-system (coredns, metrics-server, cert-manager, sealed-secrets, nfd, local-path, reloader) | ~0.45 GiB | kept as-is — **sealed-secrets stays** (the secret-management model is unchanged from kluster-code); only the JuiceFS dashboard is dropped |
| CNPG operator + traefik | ~0.3 GiB | CNPG stays; traefik → the two Envoy gateways (similar) |
| **Total** | **~5 GiB (~30%)** | **projected ~3.65 GiB** |

The projection *includes* what the new design adds (Cilium agents are
heavier than flannel+kube-proxy, two gateways, VolSync controller) and
excludes what it refuses: Longhorn's 2–4 GiB was the largest single new
infra item on the table and is deferred with criteria (storage.md §3.2).
Rule going forward: **standing infra must pay standing rent** — a
component that idles waiting for rare events (mobility layers,
dashboards nobody opens) is bought as a procedure (restore, redeploy)
instead of a daemon.

Cloud-node corollary — and the real point of this section: **cloud-node
size is set by the fixed floors, not by workloads**. Platform floor
(Talos + kubelet + Cilium) is ~1.5 GiB; the CP slice
(etcd+apiserver+controllers) another ~2–2.5 GiB — before the first app
pod. This is what made 2–4 GB paid instances structurally cramped and
what the 8 GB A1 nodes absorb, billed at A1's memory rate (§3.2): each still keeps ~4 GiB for
Envoy, hath (a 30-day peak of 0.5 GiB, §1.1, held to its node by its
cache's volume) and the pods the scheduler places there, which fit the
allocatable the control plane leaves (§1.1). Meanwhile, the homelab
side, though tighter on paper, is the flexible one: workloads can be
trimmed, ARC squeezed, and RAM added (§4.2) — memory pressure there is
real but manageable.

## 5. High availability, honestly

What HA means here, in tiers — the 3-node cloud pool delivers Tiers 2–3
by construction; Tier 0 remains the foundation everything else sits on:

-   **Tier 0 — declarative rebuild + backups (the foundation)**: the
    cluster *is* the Pulumi program plus Talos machine configs. Survival
    of permanent loss is designed to come from three legs, and the etcd
    snapshots are the one that runs. Their destination — the bucket,
    with the etcd leg's keys — is declared by the `physical` stack
    (declarative/physical.md §5, storage.md §4). The legs: hourly etcd
    snapshots shipped to B2 (off-provider by the storage.md §4
    placement rule) — a CronJob in the cluster, on the control planes
    (storage.md §5), which `k8s-base` declares; VolSync volume
    backups and CNPG barman to the same bucket (storage.md §5) —
    whose operators the `k8s-base` stack declares, with no backup
    declared against either until the first database and the first
    `claim` under a retention class, so neither runs; and periodically
    *drilled* restores — the drill program of operations.md §4, of
    which the etcd restore-verify runs, daily, restoring the newest
    snapshot into a scratch etcd on the worker, and none of the others
    has run.
    Target: RPO ≤ 1 h, RTO ~1–2 h hands-on. The **cold-standby drill**
    covers total-cloud-loss (tenancy termination included): bootstrap a
    temporary single-node CP on the homelab host (libvirt) from the
    latest etcd snapshot in ~30–60 min, then rebuild the cloud pool at
    leisure. (The scratch CP VM's ~4 GiB has no standing slack to come
    from on the 32 GB host — the drill script, unwritten today, is to
    squeeze ARC / the worker VM for the duration and restore them
    after.)
-   **Tier 1 — workload HA**: apps that support replication run
    multi-replica across the pools (CNPG multi-instance, stateless ×2).
    Even under full CP loss, running workloads keep serving — kubelet
    and the Cilium datapath don't need the API to keep forwarding.
-   **Tier 2 — public-ingress HA (delivered by design)**: the NLB
    health-checks the three nodes; Envoy runs on all of them
    (architecture.md §3.2–3.3). Only hath is single-instance by
    protocol necessity (its dedicated VIP survives node loss; only the
    cache volume ties it to a node at a time).
-   **Tier 3 — control-plane HA (delivered by design)**: etcd quorum
    across the three cloud nodes, same region, **one availability
    domain each** (fault domains are the tiebreak where a region offers
    fewer ADs than nodes). The AD spread is not only about independent
    failure domains: A1 capacity is per-AD, so a fleet packed into one
    AD draws its replacements from a single pool — and "replace at
    leisure" below assumes that pool has something in it. Degradation
    ladder: lose
    1 node → quorum holds, replace at leisure (the capacity hold, §3.2);
    lose 2 → etcd disaster recovery from the surviving member
    (re-bootstrap single-member, scale back to 3 — minutes-to-an-hour of
    API downtime, no data loss); lose all 3 → Tier 0: the cold standby,
    or, with the account intact, the fleet created again by the program
    with etcd empty and both cluster stacks applied again
    (operations.md §3.5).
    On the Vultr fallback this tier degrades to the superseded
    homelab-CP layout (architecture.md §6.5) with the drill direction
    flipped back.

## 6. Alternatives Considered

-   **AWS Lightsail (dual-stack $5–12/mo, 1–3 TB bundled)**: best sticker
    price of any hyperscaler product, rejected because custom images cannot
    be booted — no Talos. Revisit only if the Talos requirement ever falls.
-   **GCP t2a (ARM Tau)**: no CUD pricing at all ($56/month on-demand for
    2 vCPU/8 GB) and limited regions; strictly dominated by e2 + CUD.
-   **GCP free-tier e2-micro**: free but 1 GB RAM — cannot host Cilium +
    Envoy + workloads. Usable later as a free external probe/uptime checker,
    not as a cluster node.
-   **Spot/preemptible for the ingress node**: e2-medium spot ($14.68) barely
    beats the 1y CUD ($15.41) and brings preemption-induced public downtime.
    Rejected.
-   **IPv6-only cloud node** (drop the $3.65 IPv4): rejected for the same
    reasons as architecture.md §6.1 — public ingress must serve IPv4 users,
    and registries/apps still assume IPv4.
-   **Hetzner EU regions** (Falkenstein/Helsinki, CX32-class ~€7 + 20 TB):
    the pricing that made Hetzner attractive still exists — in Europe.
    Rejected for +90–100 ms RTT to the US home site on every KubeSpan
    packet and for US visitors; the WAN-latency budget is already the
    architecture's scarcest resource.
-   **netcup Manassas (~$6–8, generous traffic)**: attractive sticker,
    rejected on IaC immaturity — no credible terraform/pulumi provider,
    and this cluster's premise is that everything is declared.
