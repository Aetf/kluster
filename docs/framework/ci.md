# CI & State Backend

Objective: how the four stacks ([pulumi.md](pulumi.md) §3) are driven —
where state lives, how CI reaches everything, and the pipeline shape.
Decided 2026-08-22 (interactive review); ports the proven kluster-code CI
mechanics (rebase-merge, zero-diff noop-automerge, HA failure push) onto
the layered layout.

## 1. State backend: Postgres on an OCI E2.1.Micro

The native `postgres://` DIY backend is kept — it was chosen over
object-storage backends for performance and that reasoning stands — but
the instance moves from the homelab host to an **OCI VM.Standard.E2.1.Micro**
(Always Free, x86, 1 GB; otherwise unused):

-   **Why it moves**: with the control plane cloud-side, cluster
    management must survive a home outage — a home-hosted state backend
    would re-couple every `pulumi up` (and all of CI) to the home uplink
    and to a per-run ZeroTier join. On the micro, the hot path needs no
    home connectivity at all.
-   **Bootstrap dependency, not Pulumi-managed**: like its predecessor,
    the backend must exist before Pulumi can act, so it is provisioned by
    the ported `deploy/state-backend/` (adapted to the OS below) — the
    micro instance itself is the one hand-created OCI resource,
    documented here.
-   **OS (decided 2026-08-23): an immutable, auto-updating container
    OS, fully provisioned at create time** — the micro is a
    zero-maintenance appliance, not a hand-patched pet, and the box is
    a standing brute-force target (below) that must never run stale.
    Preferred: **Fedora CoreOS** — Butane→Ignition as instance
    `user_data`; Postgres and the pg_dump timer as podman **quadlet**
    units (the idiom already proven on the homelab host); automatic OS
    updates with reboots accepted (a brief 5432 blip; CI retries).
    Any change re-provisions from the config — the instance carries no
    state that pg_dump + `pulumi refresh` can't rebuild.
    Implementation-time verification: Ignition's OCI platform support;
    fallback is openSUSE MicroOS or Ubuntu Minimal via cloud-init (the
    Oracle datasource is standard) + unattended-upgrades.
-   **OCI Container Instances rejected** as the runtime (checked
    2026-08-23): persistent storage is not supported (15 GB ephemeral
    only — disqualifying for Postgres), and A1-shaped container
    instances bill from the same tenancy A1 pool the three cluster
    nodes already budget to its conservative limit (nodes.md §3.2),
    while the E2.1.Micro is separately Always Free.
-   **Exposure**: public 5432 with TLS + scram-sha-256 (GitHub runners
    have no stable IPs to allowlist). **Client-certificate
    verification is mandatory**, not optional (`pg_hba` `cert` /
    `verify-full`; CI holds the client cert like any other secret) —
    the state contains `machine_secrets` behind one passphrase, so
    password-only auth on the open internet is not an acceptable outer
    wall. An OCI NSG additionally narrows 5432 to the published GitHub
    Actions ranges (`api.github.com/meta` — coarse, but it removes the
    internet-wide surface). State secrets remain passphrase-encrypted
    regardless (`PULUMI_CONFIG_PASSPHRASE` in CI secrets / local mise
    env).
-   **Tenancy co-fate, mitigated**: the backend now shares fate with the
    OCI tenancy (a named risk). Mitigation is the same posture as etcd:
    a **scheduled `pg_dump` to B2** (a timer on the micro),
    **age-encrypted before upload** — the dump holds every stack's
    ciphertext and salt, and B2 is where credentials concentrate —
    restore drilled
    with the rest of nodes.md §5 Tier 0. RPO ≤ 24 h on state is fine —
    state is re-derivable from reality (`pulumi refresh`/import) at
    worst.
-   The legacy homelab Postgres backend keeps serving kluster-code
    untouched until that cluster retires.

## 2. Connectivity per job

| Target | Needed by | Path |
| --- | --- | --- |
| OCI / Cloudflare / B2 APIs | all layers (Cloudflare: `dns`, `apps`) | public |
| kube API, cloud Talos apid | `k8s-base`, `apps` | public (NLB 6443/50000, mTLS) |
| homelab worker's Talos apid | `physical` | via cloud endpoints — talosctl proxies to `--nodes <homelab>` through apid over KubeSpan (**bootstrap verification item**) |
| libvirt + gw-config (UDM SSH), UniFi Network API (firewall rules — `physical` only, API-key auth), AdGuard APIs | `physical`, `apps` (AdGuard rewrites) | **per-run ZeroTier join** (ephemeral member, pre-authorized) — no standing runner, no home inbound ports |
| State backend | all layers | public TLS (§1) |

Only the `physical` jobs — and `apps` runs that change split-horizon
rewrites (AdGuard lives on the UDM) — touch ZeroTier; a typical app
image bump stays entirely public-endpoint. The AdGuard rewrite
resources tolerate an unreachable UDM by failing only their own
resources, not the whole up. CI's ZT members are **tag-confined by
Central flow rules** (managed with the rest of the ZT config,
architecture.md §5.3) to exactly the three targets in the table — a
leaked join credential does not buy general LAN access.

## 3. Pipeline shape

```
PR:    detect-changes ─→ preview-{physical, dns, k8s-base, apps} (parallel)
                            └─ all zero-diff → noop-automerge (ported)
merge: up-physical ──needs──→ up-k8s-base ──needs──→ up-apps
                └──needs──→ up-dns (parallel to k8s-base)
```

-   **Merge side runs `up` only.** `pulumi up --yes` performs its own
    preview as phase one; a separate post-merge preview job would be a
    duplicate. The zero-diff "gate" on merge is simply that an up with
    nothing to do is a fast no-op.
-   **PR previews run in parallel** (previews don't mutate state). When
    an upstream layer has a diff, downstream previews are computed
    against the *current* StackReference outputs and may be off — accepted
    and annotated on the PR; the serial `needs` order of the up jobs is
    what guarantees correctness at apply time.
-   **Path-filter skipping is a setup-cost optimization, not a
    correctness mechanism**: docs-only or clearly single-layer changes
    skip other jobs (saving checkout/deps/ZT-join); anything touching
    shared code (`conventions.py`, `putils/`, `packages/crds`) runs all
    layers and lets the internal previews no-op.
-   **Plan-pinning (`preview --save-plan` / `up --plan`) is deliberately
    not adopted** initially: it would guarantee merge applies exactly the
    reviewed plan, but adds plan-artifact plumbing and hard-fails on any
    benign drift between review and merge. Revisit if
    reviewed-vs-applied divergence ever actually bites.
-   Ported unchanged from kluster-code: rebase-merge (not squash —
    committer identity), zero-diff **noop-automerge** for renovate-class
    PRs, Home Assistant push notification on deploy failure.
-   **Credential partitioning (2026-08-23, from the security audit)**:
    secrets live in **per-stack GitHub Environments** — the `dns` jobs
    see only the Cloudflare token, `apps` never holds the UDM key or
    OCI admin credentials, and the `physical` environment (UDM root
    SSH, OCI, ZT) is restricted to main-branch deployments **with a
    required-reviewer gate**: the one approval door kept. kluster-code
    deleted its gate for the *apps* cadence, and apps stay
    frictionless here too — but a layer that can root the gateway is
    not that layer. Previews run only for same-repo branches
    (`pull_request`; fork PRs get no secrets, `pull_request_target` is
    never used): a preview **executes the PR's Python with provider
    credentials**, so who can trigger one is a security boundary, not
    a convenience setting. noop-automerge stays scoped to renovate
    lockfile/pin PRs; repo secret scanning + push protection on.
-   **One scheduled workflow owns the outside-cluster backups**
    (decided 2026-08-23 — storage.md §5 names the backups but not
    their owner): the hourly **etcd snapshot** runs here (`talosctl
    etcd snapshot` against the NLB endpoint → upload to B2), because
    CI already holds the talosconfig and B2 credentials and a
    scheduled job pays no standing rent — no in-cluster CronJob, no
    talosconfig copied into the cluster. The same workflow does the
    **freshness check for backups vmalert can't see**: object-age
    assertions on the B2 `etcd/` and state-backend `pg_dump` prefixes
    (the micro's cron is otherwise unmonitored), failing into the same
    HA push channel as deploy failures — the out-of-cluster mirror of
    the in-cluster backup-freshness alert family
    (cluster-infra.md §3).

## 4. Self-built images (decided 2026-08-22: they live in this repo)

The cluster-consumed custom images — the CNPG operands (pg_cron,
vchord/pgvecto-rs), emailproxy, golinks — stay **in the kluster repo**
(`docker/` + an `images.yml` workflow ported from kluster-code), not in
homelab-containers: ownership follows the consumer (the same
co-location principle as DNS records and firewall rules), and the
proven single-repo loop is kept intact — a `.conf` version file per
image, renovate's comment-driven regex managers bumping it,
noop-automerge merging on green, the workflow publishing the ghcr tag,
and renovate then opening the *deploy* PR against the image pin for
human eyes. homelab-containers keeps its host/UDM scope (nspawn
rootfs).

Upgrades over the legacy workflow, both mandatory now:

-   **Multi-arch is required** — the cloud pool is arm64 (splitpro's
    CNPG operand runs there). Builds use GitHub's free native arm64
    runners (public repos) + a manifest-stitch job; no qemu, which also
    keeps the Rust-heavy vchord build viable. Free arm64 runners
    require **flipping this repo public**, which is gated on history
    hygiene: the git history carries kluster-code-era stack config
    (`Pulumi.dev.yaml` ciphertext + encryption salt) — scrub the
    history or rotate the affected secrets before the flip
    (cluster/security-audit.md).
-   **The CNPG images join the CI** — the legacy manual `just docker-*`
    flow retires; heavy builds are exactly what should not depend on a
    workstation. kluster-code's `docker/` retires with the migration
    (old-tracker rule).

The blog is deliberately **not** an image (workloads.md §4: built
branch + git-sync).
