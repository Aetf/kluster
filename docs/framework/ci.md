# CI & State Backend

Objective: how the four deployed stacks
([declarative/README.md](../declarative/README.md) §1) are
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
    the backend must exist before Pulumi can act, so no stack declares
    it. The `state-backend provision` console script
    (`src/kluster/scripts/state_backend/`) creates it: through the OCI
    Python SDK the micro instance and the network, reserved address and
    boot image it needs, and the bucket that image is imported through,
    and through B2's API its dump bucket. What the
    box runs is the machine definition in `deploy/state-backend/`; the
    design is physical/state-backend.md.
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
-   **Exposure, as CI sees it**: public 5432, TLS with **mandatory
    client certificates** as the only authentication — no password
    method is offered — and the server verified `verify-full` by
    literal IP (no DNS in the hot path). CI holds the `ci` client cert as an
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
    its restore to be drilled with the rest of nodes.md §5 Tier 0 (the
    state-backend rebuild playbook, physical/state-backend.md §7.3). As
    of 2026-09-25 its scheduled drill has not run (operations.md §4).
    Its operator form, physical/state-backend.md §7.3.1, ran against a
    scratch box on 2026-09-18, on a workstation dump and without step
    1's fetch from B2, and `state-backend restore` ran against the
    appliance in its first replace-and-restore on 2026-09-25. RPO ≤ 24 h
    on state is fine — state is re-derivable from reality
    (`pulumi refresh`/import) at worst.
-   The legacy homelab Postgres backend keeps serving kluster-code
    untouched until that cluster retires.

## 2. Connectivity per job

| Target | Needed by | Path |
| --- | --- | --- |
| OCI / Cloudflare / B2 APIs | all layers (Cloudflare: `dns`, `apps`) | public |
| kube API, cloud Talos apid | `k8s-base`, `apps` | public (NLB 6443/50000, mTLS) |
| homelab worker's Talos apid | `physical` | public — the cluster endpoint (NLB 50000), naming `--nodes <homelab>`; the control plane that answers proxies over KubeSpan (**bootstrap verification item**) |
| libvirt + UDM SSH (the device files), UniFi Network API (firewall rules — `physical` only, API-key auth), AdGuard APIs | `physical`, `dns` (AdGuard rewrites) | **per-run ZeroTier join** (pre-authorized CI member identities, gateway.md §2.1/§2.6) — no standing runner, no home inbound ports |
| State backend | all layers | public TLS (§1) |

Only the `physical` and `dns` jobs touch ZeroTier; `apps` — the stack
that changes daily — reaches nothing but the cluster, because the
split-horizon rewrites its routes imply are applied by `dns` from the
same plain-data declaration (dns.md §3). A typical app image bump
stays entirely public-endpoint, and ZeroTier's availability is not a
dependency of it. The AdGuard rewrite resources tolerate an
unreachable UDM by failing only their own resources, not the whole
up. CI's overlay members are **tag-confined by
Central flow rules** (managed with the rest of the Central config,
architecture.md §5.3) to exactly the four targets in the table
(UDM SSH, UDM UniFi API, AdGuard APIs, homelab libvirt SSH) — a
leaked join credential does not buy general LAN access. Residual on
record (audit M6): the AdGuard credential is full-admin (AdGuard has
no scoped API), so LAN-DNS control rides the `dns` tier — the tier
that already holds the Cloudflare token, and therefore the whole of
this installation's naming rather than only its LAN half. `apps`, the
tier that changes daily, holds neither that credential nor a ZeroTier
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
a **job-level `concurrency` group named for the stack whose identity it
joins with** — `zt-dns` or `zt-physical` — and because a concurrency group is
repository-wide, that one name serializes previews, proofs, drift and
the merge chain against each other. The two physical Environments share
`zt-physical`: the plan and the apply are separate credential
partitions but are meant to carry the same member. A job-level group
may read the `matrix` context, which a job-level `if` cannot, so the
matrix jobs key on their own stack; their non-joining entries take a
per-run key that collides with nothing, because two entries of one run
sharing a key is the case GitHub does not serialize reliably.

What that buys, and what it costs:

-   At most one joining job per stack runs; a second waits.
-   A **third supersedes the second**: GitHub keeps one pending entry
    per group and cancels the older one. For a preview or a proof this
    is a re-run button rather than a correctness problem — pull
    requests are not cumulative, and `preview` is deliberately not a
    required check (github.md §3).
-   Residual, accepted: a *deploy* job that is waiting on a stack's
    group can be superseded the same way, and a canceled job is neither success
    nor failure, so that layer would silently not apply. The window is
    small — drift fires weekly, previews last minutes — and the next
    merge applies the same code again. Making it impossible would mean
    a second identity per stack, which is credential surface bought to
    close a rare and self-healing hole.

Rejected alternatives and the join-latency expectation:
physical/gateway.md §2.6. The member roster the two identities sit in,
with their addressing and role tag: physical/gateway.md §2.1.

## 3. Pipeline shape

Job names below are the ones the checks tab shows.

```
PR      checks.yml:         checks
push                          (AGENTS.md's gate as CI runs it, without
                               the cloud; the gate is one job, so that
                               `checks` is the one required context it
                               reports (§5) — the step list is the
                               workflow's own; the `alert` job beside it
                               runs only on a push)
                            ──failed on a push──→ alert

PR      preview.yml:        changes ─→ preview (dns | k8s-base | apps)
                              (parallel, report-only; all three or none —
                               the three filters are one pattern list — and
                               none for a change that reaches no stack;
                               physical has no PR preview — its credentials
                               are main-only, see partitioning below)

        noop-automerge.yml: classify ─→ prove (dns | k8s-base | apps) ─→ merge
                                     │                                     ↑
                                     └──── unproven: nothing to prove ─────┘
                              (its own workflow, not a reader of
                               preview's verdict)

        sdk-regenerate.yml: regenerate ─→ gate ─→ push onto the branch
                              (renovate's branches touching Pulumi.yaml
                               only; the push is the dispatch App's, so
                               the pushed head's runs start on their own
                               — see the bridged-SDK bullet)

merge   deploy.yml:         plan-physical ──zero diff──→ (up-physical skipped)
                                   └───────── diff ────→ up-physical [gate]
                            ──→ up-k8s-base ──needs──→ up-apps
                            ──→ up-dns (parallel to k8s-base)
                            any of the five failed ──→ notify-failure

weekly  drift.yml:          drift (physical | dns | k8s-base | apps)
                              (workflow_dispatch only, fired from the ops repo)
                            any entry failed ──→ alert

        alert.yml:          dispatch
                              (called, never triggered: it is the `alert`
                               job above, and images.yml's, which §4
                               covers — see the alert-producer bullet)
```

-   **The prose gate is what makes `checks` long, and one file per
    invocation is a measured constraint rather than caution.**
    `ltex-cli-plus` checks the markdown files an event added or
    modified — a pull request's against the `main` it merges onto, a
    push's against the commit `main` moved from — one file per
    invocation under a 120-second bound. An event checks **every**
    markdown file in the tree instead when it has no base commit to
    diff against, or when it changes the checker or what it reads — the
    `mise.toml` pin, either `.vscode/` word list, `checks.yml` itself —
    so that a bumped checker or a dropped dictionary
    word is proven against the prose it judges rather than landing for
    the next change to an affected file to fail on. That whole-tree pass
    is where the cost sits: a file is five to eight seconds on a
    workstation and longer on a runner, nearly all of it the checker's
    startup rather than the document's length, which puts a tree of this
    size at minutes — and a checker that stalls adds its whole bound on
    top of that. **Batching is the obvious remedy, and it does not
    hold.** Four files per invocation runs the tree in a third of the
    time; eight at once is where the checker stops. Handed the eight
    that include this repository's two longest documents it makes no
    further progress and is killed at the bound, while those same eight
    as two invocations of four finish in under ten seconds each — and
    other eight-file batches of the same tree pass, so the cliff follows
    what is in a batch rather than how many files are in it, and no
    batch size stays safe as documents grow. The serial cost is
    accepted. What it buys is a bound that fires per file and a log that
    says so: the step tells `timeout`'s exit 124 from the checker's own
    exit 3 for findings, and prints the stalled file and the elapsed
    seconds as an annotation, so a kill names its file the way a finding
    does.

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
    correctness mechanism**, and **selection is all-or-nothing**: a
    change that reaches no stack skips the matrix entirely (saving
    checkout/deps/overlay-join), and every other change runs all three
    layers and lets the internal previews no-op. There is no
    single-layer case to select — the three filters are one YAML anchor
    and its two aliases, so `changes` answers with all three names or
    with none, never a subset. That is deliberate: shared code
    (`conventions/`, `putils/`, `packages/crds`) is most of what a
    change touches, and a per-layer list would have to be right about
    which layer reads it.
-   **The list is a deny-list, and only reads as one under
    `predicate-quantifier: every`.** A file is code unless it is under
    `docs/`, is a markdown file, is under `.vscode/` or is
    `.gitignore` — so a path nobody thought of defaults to running the
    job rather than to being missed. The action's default quantifier
    asks whether *any* pattern matches, and each negation matches every
    file the other negations are there to exclude, so an all-negation
    list under the default selects every file there has ever been.
-   **The unattended merge waits for the required checks rather than
    racing them.** `main` requires `checks` and `changes` on an
    up-to-date branch (§5), and the merge API refuses while either is
    outstanding — a race a skipped proof is fast enough to lose, since
    `classify` is finished while `checks` is still installing its
    tools. The merge job therefore blocks on
    `gh pr checks --required --watch` before it merges, and a required
    check that goes red takes that job down with it. It also stands
    down on a draft, which `pull_request` fires for and `gh pr merge`
    refuses — a red job where the point was a merge.
-   **The installer that fetches every other tool is pinned too.**
    Every job that runs a `mise` command installs mise through
    `jdx/mise-action`'s `version` input rather than the action's
    floating default. mise is what resolves `mise.toml`'s templates, so
    the state backend's URL and the config passphrase reach `pulumi`
    through it, and a version that moves on its own moves that
    resolution with no pull request to read or to bisect. The pin lives
    in the workflows rather than in `mise.toml` because `[tools]`
    declares what mise installs, not what mise is — the action fetches
    the binary before any configuration is read. Renovate knows this
    action's `version` input by name and bumps it where it sits, at a
    cadence this repository's `patch: enabled: false` sets to roughly
    monthly: mise numbers releases by date, so `2026.9.1` to `2026.9.2`
    reads as a patch and is suppressed, a new month reads as a minor
    and opens a pull request, and a new year reads as a major and waits
    on the dependency dashboard.
-   **A bridged-SDK bump is finished on its branch by a workflow, and
    nobody clicks.** The three SDKs under `sdks/` are generated from the
    `packages:` block of `Pulumi.yaml`, and a test in `checks` holds
    each committed SDK to the block, so a renovate bump of a bridge or
    provider version is a red `checks` until `pulumi install` has
    regenerated the tree. `sdk-regenerate.yml` does that on renovate's
    branch: it regenerates `sdks/`, re-locks `uv.lock`, runs AGENTS.md's
    gate step for step on the regenerated tree, and pushes the result
    onto the branch — a tree that fails the gate is never pushed, and
    the run is red on the head renovate pushed. **The push is the
    dispatch App's act, not `GITHUB_TOKEN`'s.** A `pull_request` run
    that a `GITHUB_TOKEN` push causes is created in an approval-required
    state and starts only when someone with write access selects
    **Approve workflows to run** in the merge box; a push made with a
    GitHub App's installation token is a different actor, and the runs
    it causes start on their own. So the job mints an installation token
    of the dispatch App per run (`actions/create-github-app-token`, from
    the `DISPATCH_APP_PRIVATE_KEY` repository secret and the App's
    client id in the `DISPATCH_APP_CLIENT_ID` repository variable, which
    the `github` stack declares from `conventions/forge.py`
    (github.md §3), scoped to this repository alone — credentials.md
    §3) and hands it to
    the checkout, so the persisted credential and therefore the push are
    the App's; the workflow's own token keeps `contents: read`, which
    makes a push under it a refusal rather than a head that waits. The
    commit's author stays the Actions identity, because who pushed is
    the credential's business and the author is what `gitIgnoredAuthors`
    reads (below). A `push` made with `GITHUB_TOKEN` still starts
    nothing, which is the property the unattended merge relies on to
    start no deploy. From the push on, nobody is needed: `checks`,
    `changes` and `classify` start on the regenerated head, and
    `classify` admits `Pulumi.yaml` when the author is `renovate[bot]`
    and the document with `packages` removed is equal at base and head —
    compared **parsed**, not by hunk, so a comment and a reordering of
    keys do not count while an edit to any other key does, `versions:`
    above all. A person's edit to the recipe is a new provider, a design
    change, and keeps the human route. `classify` has no checkout and
    reads both revisions through the API. The admission rests on
    `checks` rather than on a second judgment of its own: `checks`
    already holds each `sdks/<name>` to the block and `uv.lock` to the
    tree, so "regenerated cleanly" is a required check. The bump then
    takes the proven route like any other — `checks`, `changes`,
    `classify`, `prove`, `merge` — because what it actually changes,
    `sdks/` and `uv.lock`, is code. What `prove` can say about such a
    bump is nothing in any case: the three SDKs render only in
    `physical`, which has no pull-request preview, so `prove` cannot see
    a bump's diff, and it surfaces where every provider-SDK bump does —
    as a diff in `plan-physical` on `main` (the residual accepted under
    H3 below).
    **A rebase heals itself.** Renovate's `rebaseWhen: auto` resolves
    to *behind-base-branch* here, because `main`'s protection requires
    an up-to-date branch, and `gitIgnoredAuthors` naming the workflow's
    address keeps the branch renovate's own — so on each advance of
    `main`, renovate rebases the branch from its own commit and the
    regeneration is dropped; the workflow regenerates on the new head,
    pushes as the App again, and that head's runs start as the first
    one's did. The alternative — a branch renovate would not touch once
    the workflow committed to it — is a bump that goes stale instead,
    and stays stale on a newer release too, which is why the loop is the
    accepted side.
    **What the App's key buys and what it costs.** No third App: the
    dispatch App's permission set is `contents: write` and nothing else,
    an installation adds repositories and no permissions, and
    `repositories:` scopes each run's token to the one repository that
    run pushes to — this one here, and the ops repository alone in the
    alert producer's mint. The trigger App keeps `actions: write` alone, so the
    partition stands: one App starts runs and never pushes code, the
    other pushes code and never starts a run by itself. The residual is
    the key's placement: the dispatch App's key is a repository secret
    readable by any same-repository job, and its token pushes to
    `kluster`'s unprotected branches and to any branch of
    `kluster-ops`, `main` included — a private repository has no branch
    protection on this plan (github.md §2) — but never to `kluster`'s
    `main` (protected, checks required) and to no workflow file in
    either (no `workflows` permission). On `kluster` that is the same
    "anyone who can push a branch" boundary this repository already
    accepts; on `kluster-ops` it is the fence of architecture.md §4.3. A merge route of the
    regeneration workflow's own is not part of it and never was: it
    would be a second copy of `prove`, for a proof it cannot improve on.
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
    `HAOS_DEPLOY_WEBHOOK_URL`: an empty URL logs a warning and the job
    passes, so a missing credential loses the alert instead of
    manufacturing a red run. A repository secret,
    rather than an Environment one, because the job belongs to no
    stack; every workflow here can read it, same-repo previews
    included, which is acceptable for a URL whose only power is to
    raise a phone notification.

    This is an interim shape, not the designed one. The design has CI
    hold no Home Assistant credential at all: the shared producer posts
    a `repository_dispatch` to the ops repo, which owns tier semantics,
    payload formatting, deduplication and the GitHub-issue leg
    (cluster/architecture.md §4.3). The producer is built and every
    other workflow that runs on `main` calls it (the alert-producer
    bullet below); `deploy.yml` is the one that does not, because as of
    2026-09-25 its alert is the one that reaches a phone and the ops
    repo's dispatch handler that would deliver the producer's is not
    built.
    When the handler is, this job becomes the `alert` job every other
    workflow ends in, and the webhook secret leaves the repository.
-   **Every workflow that runs on `main` ends in the alert job, and
    the job is one reusable workflow, `alert.yml`.** The rule is a
    definition rather than a list: a workflow with any trigger other
    than `pull_request` and `workflow_call` — today `push` and
    `workflow_dispatch`, `schedule` being ruled out below; a called
    workflow runs as a job of its caller, whose `alert` sees its
    failure — ends in a job named `alert` that
    `needs:` every other job of the workflow, is conditioned
    `always() && github.event_name != 'pull_request' &&
    contains(needs.*.result, 'failure')`, and `uses:` the producer.
    The pull-request guard is load-bearing: a fork's run holds no
    secret to mint with, and a pull request's failure is already on the
    pull request. Today that is `checks.yml` on its `push` runs,
    `images.yml` on its `push` and dispatched runs, and `drift.yml`,
    with `deploy.yml` the one exception
    (the bullet above); a test in `checks` holds every workflow the
    definition reaches to it and names that exception, so a workflow
    added later that runs on `main` is red until it alerts. The
    producer mints an installation token of the dispatch App for the
    ops repository alone — `contents: write`, which is what a
    `repository_dispatch` costs and the whole of what the App carries —
    from the `DISPATCH_APP_PRIVATE_KEY` repository secret each caller
    hands it and the client id in the `DISPATCH_APP_CLIENT_ID`
    repository variable, and posts one dispatch of the payload
    `conventions/alert.py` spells (operations.md §4). A missing
    key fails the mint by name, and the alert job goes red rather
    than passing with a warning. **As of 2026-09-25 the receiver is not
    built**: until the ops repo's dispatch handler exists, a dispatch is
    accepted with a `204` and starts nothing, so an alert is a red run
    in this repository's Actions tab and a green `alert` job beside it.
    Every alert these callers raise is `actionable`, keyed by its
    workflow, and names its playbook: §3.1 for `checks` and `images`,
    §3.2 for `drift`.
-   **§3.1 A `checks` or `images` alert is fixed forward.** Read the
    run. A `checks` failure on `main` is a merged change the gate now
    refuses, or a step that failed on something outside the
    repository (a tool download, the registry); re-run it for the
    second, and open a pull request that fixes the tree for the first.
    An `images` failure leaves a tag missing or half stitched, and no
    later push notices it, since the selection reads the diff rather
    than the registry: once the cause is gone, re-run the failed jobs,
    or dispatch the workflow, which builds every image (§4).
-   **Version control is `jj`, and nothing here notices**: the forge
    sees git objects, so every workflow, check and merge behaves as it
    would under git — with the one accepted loss that rebase-merge does
    not carry a commit's `change-id` header onto `main`
    ([dispatch.md](dispatch.md) §1.2).
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
    (register rows in credentials.md). A diff fails the run, and a
    failed run ends in the alert job (the alert-producer bullet
    above): an `actionable` alert keyed `kluster/drift`, whose
    playbook is §3.2. Keyed by the workflow, a drift that persists is
    to be one open issue collecting a comment a week rather than a
    page a week. **As of 2026-09-25, how the human learns of it is
    half built**: the producer posts the alert, and the ops repo's
    dispatch handler that would turn it into a push and an issue does
    not exist, so a diff is a failed workflow run and nothing more.
    The workflow has also never run, because the ops repo's
    `drift-trigger.yml`, which would trigger it, is not written
    (`kluster-ops#57`).
    `--refresh` is load-bearing for the second source: a plain
    preview diffs code against *cached* state and never queries
    providers, so a console hand-edit leaves code == state and
    reports zero diff — only the device files would surface without it
    (`DeviceFile`, `DeviceDirectory` and `DeviceArtifact` all read the
    device in `diff`, architecture.md §5.2). A preview's refresh
    writes nothing: the refreshed state is compared and discarded, so
    the same diff fails every weekly run until it is reconciled
    (§3.2). This closes the "hand edits never surface" gap that
    deleting the post-merge preview left open.
-   **§3.2 A drift alert.** The matrix entry that failed names the
    stack. Its log ends either in `--expect-no-changes` refusing the
    changes the refreshed preview found — a diff — or in an error
    before the comparison (the ZeroTier join, the state backend, a
    provider credential; §5 names those standing today), which is
    re-run once its cause is gone and is not drift. A diff means
    something changed behind Pulumi's back, the device files and the
    OCI console being the realistic sources. Review it by hand, then
    either change the code to what reality should be and merge it,
    which deploys it, or put reality back as the code says with
    `mise x -- pulumi up --refresh --stack <stack>` from the checkout
    that holds `.credentials/` (the workstation form README.md gives
    for a preview). `deploy.yml`'s plain `up` does not do the second:
    the drift run's refresh is a preview's and writes nothing, so
    state still holds what the last deploy wrote, and only the device
    resources, whose `diff` reads the device (architecture.md §5.2),
    are put back without it.
-   **Enforceable because the repo is public** (2026-08-25). Branch
    protection and rulesets return `403` on a private repository
    under this account's plan, and an Environment's reviewer gate is
    a public-repository feature too (framework/github.md §2), so the
    visibility flip was a security milestone rather than a packaging
    one: it is what lets `main` have required checks at all and
    `up-physical`'s approval door exist. The gates below are declared
    by the `github` stack and applied 2026-08-25: `main` requires
    `checks` and `changes` and an up-to-date branch, with
    `enforce_admins` on, and `physical` is reviewer-gated. **The
    zero-diff proof is not among them.** It is a condition
    `noop-automerge` enforces on itself before it calls the merge API,
    not a context branch protection knows about (§5) — what the flip
    buys the unattended merge is the two required contexts that merge
    waits on.
-   **Credential partitioning (2026-08-23, from the security audit;
    physical split amended 2026-08-24)**: secrets live in **per-stack
    GitHub Environments** — the `dns` jobs see only the Cloudflare
    token, `apps` never holds the UDM key or OCI admin credentials,
    and the physical credentials (UDM root SSH, OCI, the overlay identity) are
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
    a convenience setting. **noop-automerge's *candidacy* is by path,
    with one admission that asks who as well**: any pull request
    touching neither `src/` nor `Pulumi.*` is a candidate, which is
    renovate's lockfile and pin traffic in practice but is not
    restricted to it — and so is a renovate bump of `Pulumi.yaml`'s
    `packages:` block, which is the generator's recipe rather than
    stack configuration (the bridged-SDK bullet above). The gate that
    matters is the zero-diff proof either way, and an `expect-changes`
    label opts a pull request out of the whole path. A candidate that touches
    nothing but documentation, `.vscode/` or `.gitignore` — the same
    deny-list the preview filter uses — **may skip the proof and merge
    on the required checks alone**: no stack program reads those paths,
    so an empty preview of them is a ceremony rather than evidence. That
    reasoning does not retire, because which files a stack program
    reads is not a phase. **That route tests the author for a
    reason of its own**: skipping the proof skips the last thing
    holding a
    documentation change until somebody read it, and dispatch.md §3
    says no pull request merges reviewed by nobody but its author — a
    rule aimed at `AGENTS.md` and `docs/` above all. So it is open to
    renovate's own pull requests and to nothing else, and every other
    documentation change takes the proven route. **An operator opt-in
    by label is ruled out rather than pending** (kluster-ops#244,
    #247): a label carries no record of who applied it, and on a later
    `synchronize` the actor is the author regardless, so a label that
    *removes* a gate is one an author can put on their own pull request
    and merge behind — the case dispatch.md §3 forbids by name.
    `expect-changes` is safe against the same hole only because it runs
    the other way: it can stand the merge down and can never let one
    through. The switches a workflow branches on live in the census
    `conventions/forge.py` carries — the labels it reads, and the
    identities it tests a pull request's author against — because one
    the census does not name is silently dead: the comparison is never
    true, and the route it guards is never taken. A fork's pull request
    is refused by name in `classify`, and by name rather than by
    consequence because the proof-skipping route reaches the merge
    without running any job that a missing Environment secret would
    fail. Repo secret scanning and push protection are on.
    A dedicated **`drill` Environment — in the ops repo, where the
    drill workflows are to run** — is where the unattended drills'
    credentials (drill-compartment OCI user, dump-read B2 key, drill
    age key) are to live, with **no reviewer gate — the scope is the
    gate** (credentials.md §4). The Environment exists; its three
    credentials are filled by `credentials derived drill-age-identity
    generate` and `credentials derived drill-credentials mint`
    (credentials.md §3); as of 2026-09-25 the ops repo carries no
    workflow that would read any of them (operations.md §4).
-   **What fills these Environments is a workstation, not a stack and
    not a job.** The register's executable form is the `credentials`
    console script (`src/kluster/scripts/credentials/`), whose slot map
    is the machine-readable half of credentials.md §3: one row per
    credential, naming where its value comes from and every slot it
    lands in. A GitHub Actions secret — in an Environment of this
    repository, in the ops repository, or repository-wide — is one of
    that map's channels, pushed through `gh secret set` as the GitHub
    admin token, which the pusher reads out of the `github` stack's
    committed configuration (credentials.md §3). Neither of the other
    two candidates can hold the job:
    the `github` stack declares the *structure* the secrets sit in and
    runs a few times a year, while some of these values are generated
    in Pulumi state after it last ran; and a workflow that could write
    its own Environment's secrets could rewrite the partition
    confining it, which is the one property the partition exists to
    have. That is why the credential which writes them — the GitHub
    admin token — sits in the one stack whose configuration is **not**
    encrypted under the passphrase these Environments carry
    (credentials.md §1 rule 6): every Environment holding that
    passphrase means every Environment could otherwise read that
    stack's config, and the ungated pull-request Environments make
    "every Environment" reach as far as "anybody who can push a
    branch" (github.md §1).
    The partition above is therefore also the map's shape —
    the estate `PULUMI_CONFIG_PASSPHRASE` and the state-backend bundle in every
    Environment because every job runs a `pulumi` command,
    `ZEROTIER_IDENTITY` only in the Environments of the stack it
    belongs to (physical/gateway.md §2.6).
-   **Two ways a row fills, and which one applies is a property of the
    credential.** *Synced* rows are copies of a value whose truth lives
    elsewhere, and `credentials derived sync` re-reads and re-pushes
    them: the state passphrase out of the escrow, `ZEROTIER_IDENTITY`
    out of the `physical` stack's state, `ZEROTIER_NETWORK_ID` out of
    the constant `conventions.overlay.NETWORK_ID`, and the Home
    Assistant webhook URL from whoever types it — `HA_WEBHOOK_URL` in
    `kluster-ops`, pushed by `credentials derived sync --only
    haos-webhook`, while `deploy.yml` keeps reading the undeclared
    `HAOS_DEPLOY_WEBHOOK_URL` until the dispatch handler exists
    (credentials.md §3). Re-running such a
    push is a refill, never a rotation. The four `PULUMI_BACKEND_*`
    carriers are the exception: the leaf key of a client certificate is
    stored nowhere, so a push *issues* a fresh `ci` bundle under the
    escrowed CA rather than copying the one CI already holds — which
    costs nothing, because the appliance authenticates the CA and this
    PKI revokes nothing, so the predecessor keeps working until it
    expires. A *minted* credential is pushed from nowhere at all: it is
    disclosed once, to the run that creates it, so its own `credentials
    derived <row> mint` fills every slot it has in the same run, and
    naming such a row to `sync` is refused rather than quietly doing
    nothing. No provider credential is a GitHub secret today for that
    reason — each reaches its job through the stack's committed
    configuration, which the program reads for itself.
-   **A push is verified as far as the channel allows.** The API never
    discloses a secret again — not to a later run, not to the token
    that wrote it — so what a push checks is that the name is in the
    Environment's listing and its timestamp moved. That distinguishes a
    delivered secret from a refused one and nothing more: no channel
    here can tell a correct value from a corrupted one. A row whose
    source is not yet reachable — a stack that has not run, a
    credential nobody has typed in — is reported by name and the walk
    continues, so the exit status of a full `sync` is the answer to
    "is the map filled".
-   **Every scheduled workflow lives in the ops repo, but for the
    in-cluster drills the cluster runs itself (operations.md §4); this
    repo is event-driven only** (2026-08-24, amending the 2026-08-23 "one
    scheduled workflow here" decision): once public, this repo's
    scheduled workflows would sit under GitHub's 60-day inactivity
    auto-disable — and the freshness checks silently dying with the
    thing they watch is exactly what the dead-man design exists to
    prevent. So the private **`kluster-ops`** repo (the notification
    hub, architecture.md §4.3) owns the rest of the scheduled census:
    the hourly **etcd snapshot** (designed as `talosctl etcd
    snapshot` against the NLB endpoint → upload to B2 — no in-cluster
    CronJob, no talosconfig copied into the cluster; as of 2026-09-25
    that workflow is unwritten, so none is taken — nodes.md §5 Tier 0),
    the
    **freshness checks
    for backups vmalert can't see** (object-age assertions on the
    B2 `etcd/` and state-backend `pg_dump` prefixes, the
    server-cert expiry probe ≥30 days —
    physical/state-backend.md §6), the issue-sync poller, the
    **slot-drift probe** (credentials.md §4), the **weekly drift
    trigger** (below), and the unattended
    **drill workflows** (state-backend rebuild, etcd restore-verify
    — operations.md §4) in the ops repo's `drill` Environment.
    Every run of them is to post to the same intake as CI's — the
    same event to the same dispatch handler, with that repository's
    own token since no repository boundary is crossed, an alert when
    it failed and a `heartbeat` when it passed — and each is to be
    watched by a dead-man timer on the Home Assistant side, because a
    scheduled workflow that stops running cannot report its own
    absence (operations.md §4, which says what of it is built).
    Consequences carried consciously: the ops
    repo is to hold real credentials (talosconfig, the B2 etcd
    write key, the drill set — register rows in credentials.md,
    pending but for the drill set, whose generator and mint fill
    the `drill` Environment; as of 2026-09-25 nothing there reads any
    of them),
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
    branch protection and reviewer gates in §3 possible at all
    (framework/github.md §2).
-   **The CNPG images join the CI** — the legacy manual `just docker-*`
    flow retires; heavy builds are exactly what should not depend on a
    workstation. kluster-code's `docker/` retires with the migration
    (old-tracker rule).

As built, `images.yml` is three jobs and the `alert` job (§3).
**`select-images`** discovers the
image set by globbing `docker/*.Containerfile` and narrows it to the
images whose own files moved — safe in a way the preview path-filter is
not, because a published tag is a pure function of its `.conf`, so a tag
that does not exist yet implies a change in `docker/<image>.*`; a change
to the workflow or its action selects everything instead. Its name is
distinct from `preview.yml`'s `changes` on purpose: a job name is a
check-run name, and `changes` is a required context (§5) that this
workflow's filtered trigger would otherwise fill a second time on the
pull requests that touch `docker/**`. **`build`** is a matrix of
image × architecture, each entry on its native runner (`ubuntu-24.04`,
`ubuntu-24.04-arm`), publishing `:<tag>-amd64` / `:<tag>-arm64`.
**`manifest`** stitches those two into the tag the cluster actually
pins. A PR builds both architectures and publishes nothing, so the
manifest job does not run there.

The per-image `.conf` is the contract: it is *sourced*, and beyond the
build args it declares **`IMAGE`** (the image's name, which the
workflow places under the repository owner's ghcr namespace) and
**`TAG`**. A tag may be written as an expression over the other keys —
`TAG="${PG_TAG%-*}-${VECTORCHORD_SEMVER}"` in `vchord-cnpg.conf`,
`TAG="${PG_TAG}-${PGCRON_REV}"` in `pgcron-cnpg.conf` — so that the
composite tags the CNPG operands need still reduce a bump to the one
line renovate edits, or as a literal where the tag holds what no key
does, as in `golinks.conf` (its commit's date and short hash). The two names are reserved and are not
passed on as build args, and what buildah is handed is the *resolved*
values rather than the file's lines — the confs carry renovate hints and
prose comments that are not build args at all. `TARGETARCH` is supplied
by the workflow, because under native builds the runner decides the
architecture.

The blog is deliberately **not** an image (workloads.md §4: built
branch + git-sync).

## 5. Red before the installation exists

The pipeline above is complete; the installation it drives is not.
`physical` holds no resources and has never been updated, and
`k8s-base` and `apps` are not written at all — of the stacks this
repository declares, only `dns` (whose resources were imported rather
than created) and `github` have state behind them. The checks that
reach past the repository therefore fail, on `main` and on every pull
request that touches code, and that red is a property of the phase
rather than a fault to report. What follows is how to tell it apart
from a real one. The milestones named below are the ops repository's
roadmap phases (dispatch.md §4); the `M1`/`M2` of
cluster/security-audit.md are audit findings and unrelated.

`drift` is left out of the job lists below. Its matrix names all four
stacks and every entry would fail, for whichever of the two reasons
below applies to its stack, but as of 2026-09-25 the workflow has
never run at all (§3), so nothing about it here is an observation.

**A merge is gated on `checks` and `changes`, and on nothing else.**
`main` requires exactly those two contexts on an up-to-date branch,
with `enforce_admins` on and no reviewer requirement (§3); `preview`,
`noop-automerge`'s `prove`, `deploy` and `drift` are unrequired, so a
pull request whose `checks` and `changes` are green is mergeable while
the rest of the matrix is red. That much is standing and does not
retire. What is phase-bound is the reading: today a red one of those
says something about the installation and nothing about the change, so
it is not a review finding. Once the stacks exist, a red `preview` on
a code change is exactly a review finding again. Each of the three
findings below names the condition that deletes it, and the section
goes with the last of them.

**No Environment holds a `ZEROTIER_IDENTITY`, so every job that joins
the overlay fails.** The `dns`, `physical` and `physical-plan`
Environments carry `ZEROTIER_NETWORK_ID` and not the identity beside
it, because the identity is generated in the `physical` stack's state
and `credentials derived sync` (§3) can only copy it out once that
stack has been applied. The join fails in the *Install the member
identity* step of the ZeroTier action, where `zerotier-idtool` is
handed an empty secret:

```
Identity argument invalid or file unreadable: /var/lib/zerotier-one/identity.secret
```

Only `preview.yml` gives that action a name of its own, *Join
ZeroTier*. In `deploy`, `noop-automerge` and `drift` it is unnamed, and
the log shows `Run ./.github/actions/zerotier` with the same sub-step
inside it. The jobs are `preview (dns)`, `prove (dns)` and `deploy`'s
`plan-physical`. `plan-physical` is the head of the merge chain, so a
push to `main` fails there and skips all four `up` jobs — which is
also why `notify-failure`, the Home Assistant alert of §3, runs on
every merge. On a pull request the two `dns` jobs share the `zt-dns`
concurrency group (§2), and one of the pair is routinely canceled
before it runs a single step rather than reaching the failing one. A
`preview (dns)` that is canceled with no steps at all was superseded
by the lock; it is not a separate problem. **Retires with the M1 first
`physical` up**: the identity is minted by the run of ceremony step 1
that follows the cutover window (physical/gateway.md §2.5) — the one
with no targets, which creates the overlay — and not by the targeted
apply inside the window. So the `credentials derived sync` that fills
the row waits for that later run; run earlier it would succeed and
push a placeholder, because on a stack with no prior state a targeted
apply writes every export that comes from a resource it did not create
as Pulumi's unknown sentinel, which `pulumi stack output` returns as
an ordinary value (physical/gateway-cutover.md §5). These jobs then
join like any other.

**`k8s-base` and `apps` are not stacks yet, so previewing them is an
error.** Each entrypoint raises `NotImplementedError`, neither name
has a `Pulumi.<stack>.yaml`, and no stack of either name exists in the
state backend — while the `preview` and `prove` matrices already name
all three stacks. Pulumi resolves the stack before it loads the
program, so what CI reports is the backend's answer rather than the
entrypoint's refusal:

```
error: no stack named 'k8s-base' found
```

Every pull request that touches code runs both entries. `prove` is
`fail-fast: true`, so an entry that fails first can cancel the other
two: which names are reported failed and which canceled varies
between runs and carries no information. A pull request that touches
only documentation, `.vscode/` or `.gitignore` runs no `preview` at
all — `changes` selects an empty set and the matrix job stands down.
Whether it also skips `prove` turns on who opened it: renovate's own
pull requests merge unattended, and every other documentation change
still proves against the missing stacks and is merged by hand (§3).
**Retires with the M2 stacks** (`kluster-ops#77`), which create both
and make the matrices honest.

**Neither of those is a secret-availability problem, and the tell is
which step fails, and then which name the message carries.** All five
Environments hold the four `PULUMI_BACKEND_*` secrets and the
passphrase, and the `state-backend` action checks the four before
anything else runs: an empty one aborts the job in *Write the bundle
into the checkout's slot* with `the ci client bundle is incomplete`,
naming the variables that came through blank. The passphrase has no
such guard — the `pulumi` step reads it directly, and a blank one
surfaces only once a stack holding encrypted secrets is reached, which
in this phase never happens.

A job that reached a `pulumi` command therefore had a complete bundle,
so `no stack named` at that step is not a blank secret. It is not
proof that the backend answered either: whether `pulumi` was pointed
at the appliance turns on `mise.toml` resolving the slot under
`config_root`, which no line of the log settles either way. What
`pulumi` does with no URL at all is left out here on purpose — it
depends on the version and on whether the session is judged
interactive, and nothing below rests on it. In this phase no job ever
reaches a stack that exists, so no run here says anything about the
box being up.

**The names are the test.** The only ones ever expected in that
message are `k8s-base` and `apps`. `no stack named 'dns'` or
`no stack named 'physical'` — both exist in the backend, `dns` holding
the records imported into it — means `pulumi` was pointed at a backend
that is not the appliance, or that the state is gone, and is a
regression on either reading. A bundle that resolves but cannot reach
the appliance fails differently again, with
`unable to open bucket ...: failed to ping PostgreSQL`.

One artifact of the logs invites a blank secret where there is none.
The runner prints each step's inherited environment, and in it
`PULUMI_BACKEND_URL`, `PULUMI_CONFIG_PASSPHRASE` and the three
`PGSSL*` variables are all blank — in every job, including
the ones that go on to reach the backend. That listing is a snapshot
`mise-action` took before the bundle was materialized, frozen into the
job's environment; `mise x` re-resolves them from the checkout's
`.credentials/state-backend/` slot at the moment the command runs
(physical/state-backend.md §3), and a step that sets one explicitly
overrides it, which is why `PULUMI_CONFIG_PASSPHRASE` reads `***` in
the `pulumi` steps and blank in every other.

The pair that actually misleads is in the ZeroTier step, where that
blank passphrase is the line directly above `IDENTITY:`. Those two
look identical and mean opposite things: the passphrase is the
snapshot artifact, while `IDENTITY` is
`${{ secrets.ZEROTIER_IDENTITY }}` interpolated by the workflow at
that moment, so blank there is the genuine absence this section opened
with. **Retires with the two paragraphs above**, since a green matrix
leaves nothing to misread — but the parts of it that are not phase
state should survive the deletion beside the credential partition they
belong to in §3: the `state-backend` action rejects an incomplete
bundle by name, the passphrase is not covered by that check, and the
step headers go on showing blanks after every stack exists.
