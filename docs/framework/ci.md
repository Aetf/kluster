# CI & State Backend

Objective: how the four deployed stacks ([pulumi.md](pulumi.md) §3) are
driven — the forge they run on is itself declared, but applied by hand
([github.md](github.md)) —
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
| libvirt + gw-config (UDM SSH), UniFi Network API (firewall rules — `physical` only, API-key auth), AdGuard APIs | `physical`, `dns` (AdGuard rewrites) | **per-run ZeroTier join** (pre-authorized CI member identities, gateway.md §2.1/§2.6) — no standing runner, no home inbound ports |
| State backend | all layers | public TLS (§1) |

Only the `physical` and `dns` jobs touch ZeroTier; `apps` — the stack
that changes daily — reaches nothing but the cluster, because the
split-horizon rewrites its routes imply are applied by `dns` from the
same plain-data declaration (dns.md §3). A typical app image bump
stays entirely public-endpoint, and ZeroTier's availability is not a
dependency of it. The AdGuard rewrite resources tolerate an
unreachable UDM by failing only their own resources, not the whole
up. CI's ZT members are **tag-confined by
Central flow rules** (managed with the rest of the ZT config,
architecture.md §5.3) to exactly the four targets in the table
(UDM SSH, UDM UniFi API, AdGuard APIs, homelab libvirt SSH) — a
leaked join credential does not buy general LAN access. Residual on
record (audit M6): the AdGuard credential is full-admin (AdGuard has
no scoped API), so LAN-DNS control rides the `dns` tier — the tier
that already holds the Cloudflare token, and therefore the whole of
the estate's naming rather than only its LAN half. `apps`, the tier
that changes daily, holds neither that credential nor a ZeroTier
identity.

**Join mechanics, and why they need a lock of their own.** The
identity a job joins with is whatever its Environment's
`ZEROTIER_IDENTITY` holds, so the Environment is the identity's
carrier, and the jobs that join are exactly those whose Environment
has one: `plan-physical` and `up-physical` on the merge chain, `up-dns`
beside them, the `preview (dns)` and `prove (dns)` jobs a pull request
runs, and the `physical` and `dns` entries of the drift matrix. A join
cannot span jobs — each is its own runner VM — and ZeroTier maps a
member to one endpoint at a time, so **one identity must never be live
in two jobs at once**; two that share it flap.

No workflow-level setting arranges that. The deploy chain's
`concurrency: deploy` group serializes deploys against deploys, but a
drift run joins with the very same Environments' identities, and any
pull request's preview joins with the `dns` one. The lock therefore has
to name the identity rather than the workflow: every joining job takes
a **job-level `concurrency` group named for its identity domain** —
`zt-dns` or `zt-physical` — and because a concurrency group is
repository-wide, that one name serializes previews, proofs, drift and
the merge chain against each other. The two physical Environments share
`zt-physical`: the plan and the apply are separate credential
partitions but are meant to carry the same member. A job-level group
may read the `matrix` context, which a job-level `if` cannot, so the
matrix jobs key on their own stack; their non-joining entries take a
per-run key that collides with nothing, because two entries of one run
sharing a key is the case GitHub does not serialize reliably.

What that buys, and what it costs:

-   At most one joining job per domain runs; a second waits.
-   A **third supersedes the second**: GitHub keeps one pending entry
    per group and cancels the older one. For a preview or a proof this
    is a re-run button rather than a correctness problem — pull
    requests are not cumulative, and `preview` is deliberately not a
    required check (github.md §3).
-   Residual, accepted: a *deploy* job that is waiting on a domain can
    be superseded the same way, and a cancelled job is neither success
    nor failure, so that layer would silently not apply. The window is
    small — drift fires weekly, previews last minutes — and the next
    merge applies the same code again. Making it impossible would mean
    a second identity per domain, which is credential surface bought to
    close a rare and self-healing hole.

Rejected alternatives and the join-latency expectation:
physical/gateway.md §2.6. The member roster the two identities sit in,
with their addressing and role tag: its §2.1.

## 3. Pipeline shape

Job names below are the ones the checks tab shows.

```
PR      preview.yml:        changes ─→ preview (dns | k8s-base | apps)
                              (parallel, report-only; physical has no PR
                               preview — its credentials are main-only,
                               see partitioning below)

        noop-automerge.yml: classify ─→ prove (dns | k8s-base | apps)
                              ─→ merge          (its own workflow, not a
                                                 reader of preview's verdict)

merge   deploy.yml:         plan-physical ──zero diff──→ (up-physical skipped)
                                   └───────── diff ────→ up-physical [gate]
                            ──→ up-k8s-base ──needs──→ up-apps
                            ──→ up-dns (parallel to k8s-base)
                            any of the five failed ──→ notify-failure

weekly  drift.yml:          drift (physical | dns | k8s-base | apps)
                              (workflow_dispatch only, fired from the ops repo)
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
-   Ported from kluster-code: rebase-merge (not squash — committer
    identity), the zero-diff **noop-automerge**, and the **Home
    Assistant push on a failed deploy**. The push is the one that
    changed shape: there the workflow was a single job and the alert
    was an `if: failure()` step inside it, here the chain is five jobs
    and the alert is a sixth (`notify-failure`) that fires when any of
    them failed. Its payload is the legacy one — title, message naming
    the commit, link to the run. It reads a **repository** secret
    `HAOS_DEPLOY_WEBHOOK_URL`, empty for now (below): an empty URL logs
    a warning and the job passes, so a missing credential loses the
    alert instead of manufacturing a red run. A repository secret,
    rather than an Environment one, because the job belongs to no
    stack; every workflow here can read it, same-repo previews
    included, which is acceptable for a URL whose only power is to
    raise a phone notification.

    This is an interim shape, not the designed one. The design has CI
    hold no Home Assistant credential at all: one shared producer step
    posts a `repository_dispatch` to the ops repo, which owns tier
    semantics, payload formatting, deduplication and the GitHub-issue
    leg (cluster/architecture.md §4.3). That producer step, and the
    dispatch App whose token it would use, are **not built**. When they
    are, this job becomes the dispatch call and the webhook secret
    leaves the repository.
-   **Weekly drift check (2026-08-24)**: a `workflow_dispatch`
    workflow in this repo runs
    `pulumi preview --refresh --expect-no-changes`
    on all four stacks (physical in the ungated `physical-plan`
    environment — the accepted posture above, zero clicks), fired
    weekly by the ops repo's scheduler through an installation
    token from the **trigger App** — a second single-purpose GitHub
    App, installed on this repo alone and carrying **Actions: write
    only**, so it can start runs and never push code. Two Apps
    rather than one because GitHub scopes permissions per App
    (register rows in credentials.md). The playbook a diff calls for
    is human review, then reconcile reality or deploy — drift here
    means something changed behind Pulumi's back, the gw-config
    estate and the OCI console being the realistic sources.
    **How the human learns of it is not built**: the intended route is
    the same `actionable` alert the producer step raises
    (architecture.md §4.3), and neither exists, so today a diff is a
    failed workflow run and nothing more. The workflow has also never
    run: its trigger lives in the ops repo, behind the trigger App and
    an Environment layout that is still to be created.
    `--refresh` is load-bearing for the second source: a plain
    preview diffs code against *cached* state and never queries
    providers, so a console hand-edit leaves code == state and
    reports zero diff — only the gw-config estate would surface
    without it (GwFile/GwArtifact `diff` reads the device,
    architecture.md §5.2). Consequence carried consciously:
    `--refresh` rewrites state to match reality, so a drift run
    adopts the drift into state and the next deploy's diff is
    desired-vs-reality — acceptable, because the alert fires first
    and reconciling is exactly what its playbook demands. This
    closes the "hand edits never surface" gap that deleting the
    post-merge preview left open.
-   **Enforceable because the repo is public** (2026-08-25). Branch
    protection and rulesets return `403` on a private repository
    under this account's plan, and an Environment's reviewer gate is
    a public-repository feature too (framework/github.md §2), so the
    visibility flip was a security milestone rather than a packaging
    one: it is what lets the zero-diff proof be a required check and
    `up-physical`'s approval door exist. The gates below are declared
    by the `github` stack and applied 2026-08-25: `main` requires
    `checks` and `changes` and an up-to-date branch, with
    `enforce_admins` on, and `physical` is reviewer-gated.
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
    a convenience setting. **noop-automerge classifies by path, not by
    author**: any pull request touching neither `src/` nor `Pulumi.*`
    is a candidate, which is renovate's lockfile and pin traffic in
    practice but is not restricted to it — the gate that matters is the
    zero-diff proof, and an `expect-changes` label opts a pull request
    out of the whole path. A fork's pull request fails closed rather
    than merging: the proof needs Environment secrets, which a fork
    never receives. Repo secret scanning and push protection are on.
    A dedicated **`drill` Environment — in the ops repo, where the
    drill workflows run** — carries the unattended drills'
    credentials (drill-compartment OCI user, dump-read B2 key, drill
    age key) with **no reviewer gate — the scope is the gate**
    (credentials.md §4).
-   **Every secret this section names is an empty slot today.** The
    register's executable form is the `credentials` console script
    (`src/kluster/scripts/credentials/`), and the only slot kind it
    implements is a stack's Pulumi config: there is no
    GitHub-Environment sink, so nothing populates
    `ZEROTIER_IDENTITY`, `PULUMI_BACKEND_URL` or the rest, and the
    lone repository-level slot (`HAOS_DEPLOY_WEBHOOK_URL`) is
    unpopulated too. The partitioning above is therefore a design the
    workflows already obey while the forge does not yet enforce it, and
    no job in this repository has run against a real credential: a
    deploy on `main` today fails in its first ZeroTier join, on an
    empty identity.
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
    which is what forced the dispatch App's fencing
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
    require a public repository: **this repo is public since
    2026-08-25**, after the history scrub that removed the
    kluster-code-era `Pulumi.dev.yaml` ciphertext and encryption salt
    (cluster/security-audit.md L10). The same flip is what makes the
    branch protection and reviewer gates in §2 possible at all
    (framework/github.md §2).
-   **The CNPG images join the CI** — the legacy manual `just docker-*`
    flow retires; heavy builds are exactly what should not depend on a
    workstation. kluster-code's `docker/` retires with the migration
    (old-tracker rule).

As built, `images.yml` is three jobs. **`changes`** discovers the image
set by globbing `docker/*.Containerfile` and narrows it to the images
whose own files moved — safe in a way the preview path-filter is not,
because a published tag is a pure function of its `.conf`, so a tag that
does not exist yet implies a change in `docker/<image>.*`; a change to
the workflow or its action selects everything instead. **`build`** is a
matrix of image × architecture, each entry on its native runner
(`ubuntu-24.04`, `ubuntu-24.04-arm`), publishing `:<tag>-amd64` /
`:<tag>-arm64`. **`manifest`** stitches those two into the tag the
cluster actually pins. A PR builds both architectures and publishes
nothing, so the manifest job does not run there.

The per-image `.conf` is the contract: it is *sourced*, and beyond the
build args it declares **`IMAGE`** (the ghcr path) and **`TAG`**, the
latter written as an expression over the other keys
(`TAG="${PG_MAJOR}.${PG_MINOR}-${PG_REV}-${VECTORCHORD_SEMVER}"`) so
that the composite tags the CNPG operands need still reduce a bump to
the one line renovate edits. The two names are reserved and are not
passed on as build args, and what buildah is handed is the *resolved*
values rather than the file's lines — the confs carry renovate hints and
prose comments that are not build args at all. `TARGETARCH` is supplied
by the workflow, because under native builds the runner decides the
architecture.

The blog is deliberately **not** an image (workloads.md §4: built
branch + git-sync).
