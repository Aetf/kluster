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
    the ported `deploy/state-backend/` — the micro instance itself is
    the one hand-created OCI resource, designed in
    physical/state-backend.md.
-   **The box itself is a designed appliance, not a pet** — Fedora
    CoreOS provisioned entirely at create time from
    `deploy/state-backend/`, re-provision as the only apply path,
    auto-updating OS and Postgres, externally monitored, every alert
    backed by a playbook. The full design — OS & config management,
    Postgres lifecycle, PKI, network exposure, backup, monitoring,
    playbooks — is
    **[physical/state-backend.md](../physical/state-backend.md)**;
    the rest of this section keeps only what CI itself needs to know.
-   **OCI Container Instances rejected** as the runtime (checked
    2026-08-23): persistent storage is not supported (15 GB ephemeral
    only — disqualifying for Postgres), and A1-shaped container
    instances bill from the same tenancy A1 pool the three cluster
    nodes already budget to its conservative limit (nodes.md §3.2),
    while the E2.1.Micro is separately Always Free.
-   **Exposure, as CI sees it**: public 5432, TLS + scram +
    **mandatory client certificates** (`verify-full` by literal IP —
    no DNS in the hot path). CI holds the `ci` client cert as an
    Environment secret; local runs hold `operator` in the mise env.
    The NSG permits 5432 from anywhere — the client cert is
    deliberately the only wall (state-backend.md §4; the
    GitHub-ranges allowlist died on NSG rule-quota arithmetic).
    State secrets remain passphrase-encrypted regardless
    (`PULUMI_CONFIG_PASSPHRASE` in CI secrets / local mise env).
-   **Tenancy co-fate, mitigated**: the backend now shares fate with the
    OCI tenancy (a named risk). Mitigation is the same posture as etcd:
    a **scheduled `pg_dump` to B2** (a timer on the micro),
    **age-encrypted before upload** — the dump holds every stack's
    ciphertext and salt, and B2 is where credentials concentrate —
    restore drilled
    with the rest of nodes.md §5 Tier 0 (the state-backend rebuild
    playbook). RPO ≤ 24 h on state is fine — state is re-derivable
    from reality (`pulumi refresh`/import) at worst.
-   The legacy homelab Postgres backend keeps serving kluster-code
    untouched until that cluster retires.

## 2. Connectivity per job

| Target | Needed by | Path |
| --- | --- | --- |
| OCI / Cloudflare / B2 APIs | all layers (Cloudflare: `dns`, `apps`) | public |
| kube API, cloud Talos apid | `k8s-base`, `apps` | public (NLB 6443/50000, mTLS) |
| homelab worker's Talos apid | `physical` | via cloud endpoints — talosctl proxies to `--nodes <homelab>` through apid over KubeSpan (**bootstrap verification item**) |
| libvirt + gw-config (UDM SSH), UniFi Network API (firewall rules — `physical` only, API-key auth), AdGuard APIs | `physical`, `apps` (AdGuard rewrites) | **per-run ZeroTier join** (pre-authorized CI member identities, gateway.md §2.1/§2.6) — no standing runner, no home inbound ports |
| State backend | all layers | public TLS (§1) |

Only the `physical` jobs — and `apps` runs that change split-horizon
rewrites (AdGuard lives on the UDM) — touch ZeroTier; a typical app
image bump stays entirely public-endpoint. The AdGuard rewrite
resources tolerate an unreachable UDM by failing only their own
resources, not the whole up. CI's ZT members are **tag-confined by
Central flow rules** (managed with the rest of the ZT config,
architecture.md §5.3) to exactly the three targets in the table — a
leaked join credential does not buy general LAN access. Residual on
record (audit L11): the AdGuard credential in `apps` is full-admin
(AdGuard has no scoped API), so LAN-DNS control rides the apps tier —
accepted alongside the kubeconfig that tier already holds.

Join mechanics (2026-08-24): **two CI identities** — `ci-deploy` for
the merge chain, `ci-preview` for PR `preview-apps` — one per
concurrency domain, each domain serialized (the deploy workflow's
`concurrency` group, wanted for state-lock sanity anyway; a
`zt-preview` job group). A join cannot span jobs (per-job runner VMs)
and one identity must never be live twice (ZT maps a node ID to one
endpoint). Design, rejected alternatives, and the join-latency
expectation: physical/gateway.md §2.6.

## 3. Pipeline shape

```
PR:    detect-changes ─→ preview-{dns, k8s-base, apps} (parallel;
                          physical has no PR preview — its credentials
                          are main-only, see partitioning below)
                            └─ all zero-diff → noop-automerge (ported)
merge: plan-physical ──zero diff──→ (up-physical skipped)
            └────────── diff ────→ up-physical [approval gate]
       ──→ up-k8s-base ──needs──→ up-apps
       ──→ up-dns (parallel to k8s-base)
```

-   **Merge side runs `up` only — except `physical`, which gets a plan
    job** (2026-08-24, superseding the pure-up shape): for
    dns/k8s-base/apps, `pulumi up --yes` performs its own preview as
    phase one and a separate preview job would be a duplicate; the
    zero-diff "gate" is simply that an up with nothing to do is a fast
    no-op. `physical` differs because its up sits behind the approval
    gate: **`plan-physical`** runs `pulumi preview --expect-no-changes`
    in the ungated `physical-plan` environment first — zero diff (the
    common case: shared-code/lockfile merges) skips `up-physical`
    entirely and the chain proceeds with **zero clicks**; a real diff
    routes to `up-physical` in the gated `physical` environment, and
    the whole chain waits for the approval (correct: downstream layers
    may depend on the physical change). Workflow mechanics on record:
    downstream jobs need skip-tolerant conditions
    (`if: always() && needs.up-physical.result != 'failure'` shape) —
    a skipped gate job must not cascade-skip the chain.
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
-   **Weekly drift check (2026-08-24)**: a `workflow_dispatch`
    workflow in this repo runs `pulumi preview --expect-no-changes`
    on all four stacks (physical in the ungated `physical-plan`
    environment — the accepted posture above, zero clicks), fired
    weekly by the ops repo's scheduler through a fine-grained PAT
    scoped to **Actions: write only** — it can trigger runs, never
    push code (register row in credentials.md). Any diff raises an
    `actionable` alert through the standard producer step; playbook:
    human review, then reconcile reality or deploy — drift here
    means something changed behind Pulumi's back, the gw-config
    estate and the OCI console being the realistic sources. This
    closes the "hand edits never surface" gap that deleting the
    post-merge preview left open.
-   **Credential partitioning (2026-08-23, from the security audit;
    physical split amended 2026-08-24)**: secrets live in **per-stack
    GitHub Environments** — the `dns` jobs see only the Cloudflare
    token, `apps` never holds the UDM key or OCI admin credentials,
    and the physical credentials (UDM root SSH, OCI, ZT) are
    **main-only, split across two environments**: ungated
    `physical-plan` for the plan job, reviewer-gated `physical` for
    actual applies — the one approval door kept, guarding *apply*.
    kluster-code deleted its gate for the *apps* cadence, and apps
    stay frictionless here too — but a layer that can root the
    gateway is not that layer. **PRs get no physical preview at
    all**: a physical-path PR is reviewed as code, and its resource
    diff is read in `plan-physical`'s output on main — reading it is
    the approval moment before `up-physical`. Residual, accepted
    2026-08-24 (audit H3): merged main code — noop-automerged
    dependency bumps included — executes with physical credentials
    in the ungated plan job without per-run human approval; the
    compensation is that any bump which actually changes physical
    rendering surfaces as a plan diff and stalls at the gate for
    human eyes. Previews run only for same-repo branches
    (`pull_request`; fork PRs get no secrets, `pull_request_target` is
    never used): a preview **executes the PR's Python with provider
    credentials**, so who can trigger one is a security boundary, not
    a convenience setting. noop-automerge stays scoped to renovate
    lockfile/pin PRs; repo secret scanning + push protection on.
    A dedicated **`drill` Environment — in the ops repo, where the
    drill workflows run** — carries the unattended drills'
    credentials (drill-compartment OCI user, dump-read B2 key, drill
    age key) with **no reviewer gate — the scope is the gate**
    (credentials.md §4); Environment secrets are populated by
    the `deploy/credentials/` distribution scripts, not by hand.
-   **Everything on a clock lives in the ops repo; this repo is
    event-driven only** (2026-08-24, amending the 2026-08-23 "one
    scheduled workflow here" decision): once public, this repo's
    scheduled workflows would sit under GitHub's 60-day inactivity
    auto-disable — and the freshness checks silently dying with the
    thing they watch is exactly what the dead-man design exists to
    prevent. So the private **`kluster-ops`** repo (the notification
    hub, architecture.md §4.3) owns the complete scheduled census:
    the hourly **etcd snapshot** (`talosctl etcd snapshot` against
    the NLB endpoint → upload to B2 — no in-cluster CronJob, no
    talosconfig copied into the cluster), the **freshness checks
    for backups vmalert can't see** (object-age assertions on the
    B2 `etcd/` and state-backend `pg_dump` prefixes, the
    server-cert expiry probe ≥30 days —
    physical/state-backend.md §6), the issue-sync poller, the
    **slot-drift probe** (credentials.md §4), the **weekly drift
    trigger** (below), and the unattended
    **drill workflows** (state-backend rebuild, etcd restore-verify
    — operations.md §4) in the ops repo's `drill` Environment.
    Their failures need no dispatch hop — the delivery logic is
    local to that repo. Consequences carried consciously: the ops
    repo now holds real credentials (talosconfig, the B2 etcd
    write key, the drill set — register rows in credentials.md),
    which is what forced the dispatch-PAT fencing
    (architecture.md §4.3), and its Actions-minutes bill is
    accounted there too. This repo keeps only the event-driven
    set: previews, the merge chain, noop-automerge, images.yml —
    **zero `schedule:` triggers, by rule**.

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
    (cluster/security-audit.md). Sequencing fact (2026-08-24): the
    flip sits on **Wave A's critical path** (splitpro's pgcron
    operand needs the multi-arch build), so the scrub happens in
    repo-plumbing time, not "eventually" — and scrubbing is the
    realistic option of the two, because the ciphertext's secrets
    are the *legacy* cluster's, live until Wave F.
-   **The CNPG images join the CI** — the legacy manual `just docker-*`
    flow retires; heavy builds are exactly what should not depend on a
    workstation. kluster-code's `docker/` retires with the migration
    (old-tracker rule).

The blog is deliberately **not** an image (workloads.md §4: built
branch + git-sync).
