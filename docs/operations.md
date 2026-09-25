# Day-2 Operations

Who and what keeps the system current and its recovery paths proven:
the update-ownership matrix, the upgrade and node-replacement
runbooks, and the drill program with its trigger machinery. Sits at
the docs root like [credentials.md](credentials.md) — day-2 crosses
every layer boundary. Runbooks here follow the census discipline
(title, trigger, gist; executable form — the console scripts of
`src/kluster/scripts/` — ships with the implementation).

## 1. Update ownership matrix

**Rule: a pin without a PR-opener is drift.** Every pinned artifact
maps to a renovate (or equivalent) opener and a named apply path;
this matrix is the census. "Reviewed" means a human merges after
reading the preview; nothing on this table automerges unless the row
says so.

**Which rows can be applied today.** The `physical` stack carries the
Talos pin, the Image Factory schematic and the nspawn rootfs
references, so those rows apply through a deploy of that stack. The
node-by-node upgrade that follows is run by hand: no task wraps
`talosctl upgrade` or `upgrade-k8s`. The state-backend pins apply through
`state-backend provision`. **The rest wait on the `k8s-base` stack**,
the dependency bumps included; the UDM firmware row waits on nothing,
being the vendor's.

For the rows whose apply path is the CI chain — the Cilium and
k8s-base charts, the cluster images, the blog image git-sync would
deliver — a merge deploys none of them: the chain stalls in front of
their stacks, and the deploy-failure alert the images row names as its
safety valve, which is built and is the interim Home Assistant webhook
(ci.md §3), fires on every merge alike, so it singles out nothing. The
dependency bumps wait one step further back. The noop-automerge
workflow is built, but a bump touches `uv.lock`, `pyproject.toml` or a
workflow file, which puts it on the route that proves itself with a
preview of `k8s-base` and `apps` among others, and no stack of either
name exists in the state backend for that preview to resolve — so the
proof errors rather than passing. The whole account, the other facts
behind that error included, is ci.md §5. Every bump therefore merges
the way the waiting rows do: as a reviewed pull request.

| Surface | PR opened by | Applied by | Policy |
| --- | --- | --- | --- |
| Talos version (machine-config pin + Image Factory schematic) | renovate (GitHub-releases datasource) | `physical` stack for the pin and the schematic; `talosctl upgrade` by hand, serial, staged (declarative/physical.md §2) | Reviewed; §2.1 runbook |
| Kubernetes version | same PR family (Talos-coupled) | `talosctl upgrade-k8s` | Reviewed; after the Talos bump it belongs to |
| Cilium chart | renovate | CI chain (merge = deploy) | Reviewed, **never automerged** — §2.2 runbook; ≥1.20 floor (ExternalAuth) |
| k8s-base charts (cert-manager, CNPG, VolSync, sealed-secrets, VictoriaMetrics, …) | renovate | CI chain | Reviewed — chart bumps always produce a real diff; major behind dashboard approval |
| **Cluster images, infra and app alike** (CNPG operands, self-built, third-party app images) | renovate | CI chain (merge = deploy) | **Minor automerge** (patch stream folded into minor — the legacy `patch: enabled: false` precedent); **major behind dashboard approval + review**; CNPG operand major additionally gated on the self-built image line (workloads.md §4). The safety valve is the deploy-failure alert, not a per-bump eyeball — this deliberately reverses legacy's "applications get eyeballed" stance |
| blog image / built branch | blog repo CI | git-sync | Automatic — content, not code |
| nspawn rootfs (caddy, AdGuard, ZeroTier) | renovate here — docker datasource, reading whole references off the `versions:image-gateway-…` pins | `physical` stack: the device pulls the pinned manifest itself and unpacks it beside the tree it is running, then the boot chain's machine script restarts what changed | Reviewed; the run-number tag reads as a major bump, so every one waits on dashboard approval |
| State-backend pins: the FCOS stream; the Postgres major line (`settings.POSTGRES_IMAGE`, rendered into the Butane file); the age release (`settings.AGE_VERSION`) | Zincati (periodic window) for the OS; renovate for the two settings, one custom manager each on `state_backend/settings.py` | Zincati, and `podman-auto-update.timer` for the Postgres minor stream: auto. A settings bump: a manual replace-and-restore, `state-backend provision --force` and then `state-backend restore` of the dump that run names (state-backend.md §7.2) | Reviewed, grouped apart from everything else because each bump replaces the box. An age bump is one pull request moving `settings.AGE_VERSION` and the `age` pin in `mise.toml` together: renovate's mise manager reads that pin too, and a rule takes it out of the `toolchain` group into this one, because `tests/test_age.py` holds the two pins equal. The pull request is finished by hand: `AGE_SHA256` beside `settings.AGE_VERSION` is a digest renovate cannot recompute, and the `checks` workflow's `state-backend pins` refuses it stale. state-backend.md §1–2, and §7 for the age pin |
| Pulumi SDK + providers, Python deps, Actions versions | renovate | **noop-automerge workflow** — merges once the preview is proven empty (the zero-diff rule, ci.md); that proof previews stacks that do not exist yet, so nothing takes this path today (paragraph above) | Automerged when diff-free; a bump that produces a real diff falls out of the noop path to human review; major behind dashboard approval |
| UDM firmware | **vendor-controlled** (auto-update schedule; outage history on record) | — | Not ours to pin; the device's services self-heal via on_boot.d, overlay recovery runbook gateway.md §3 |

## 2. Upgrade runbooks (census)

-   **§2.1 Talos node upgrade.** Trigger: version-pin PR merged.
    Gist: one cloud node first as canary (health + workload
    settle), then the remaining nodes serially — never two quorum
    members at once (the CI-serialization rule,
    declarative/physical.md §2);
    homelab worker last; `upgrade-k8s` afterward as its own step.
-   **§2.2 Cilium upgrade.** Trigger: chart PR. Gist: the riskiest
    bump in the system — before merge, re-run the affected subset of
    the bootstrap verifications on the preview environment of one
    node (LB-IPAM node-IP pools, EGW + reserved-IP NAT, MTU over
    KubeSpan, ExternalAuth fail-closed); merge deploys; watch the
    Envoy/agent rollout complete before calling it done.
-   **§2.3 State-backend lifecycle** — owned by
    physical/state-backend.md §7 (pointer, not a copy).
-   **§2.4 CNPG major upgrade** — owned by workloads.md §4
    (pointer).

## 3. Node replacement runbooks (census)

-   **§3.1 Cloud CP node.** Trigger: hardware loss, A1 reclamation,
    or deliberate rebuild. Gist: drain → `talosctl etcd
    remove-member` → destroy in `physical` (unprotect if flagged) →
    re-create → rejoin quorum → health gate. Facts that shape it:
    A1 capacity at re-create is the known risk (the standing quorum
    holds capacity; rebuild promptly), NLB backends follow from
    `physical` automatically; a node carrying a **block volume** needs
    it reattached, and the node holding the **dedicated VIP** needs the
    secondary private IP and the reserved-IP NAT re-pointed
    (architecture.md §3.2).
-   **§3.2 Homelab worker VM.** Trigger: VM/disk loss or rebuild.
    Gist: re-create from machine config (nocloud seed); local-path
    data returns via VolSync restore (that's the designed move-path,
    storage.md §2 — no drill of it has run, §4); NAS-backed PVs
    re-point untouched; BGP session re-establishes from the static IP
    in the config.
-   **§3.3 Total-cloud-loss / total-home-loss** — owned by nodes.md
    §5 (cold-standby drill, both directions).

## 4. Drill program

Principle (standing): **an undrilled recovery path is assumed
broken.** Corollary for a one-operator system: **a drill that needs
the operator is a drill that will eventually be skipped** — so the
default form is a scheduled automation that alerts only on failure,
and the human appears exactly where an offline secret or physical
action is irreducible.

**The enabler for the biggest drill**: pg_dumps gain a third age
recipient — an **ops-repo-held drill key** (credentials.md §3), living
in that repository's `drill` Environment. The key is drawn by
`credentials derived drill-age-identity generate`, which pushes the
private half into that Environment and writes the public half to
`deploy/state-backend/drill-recipient.txt`; the appliance encrypts to
it from the converge that adopts the committed file (state-backend.md
§5), and a dump written before that converge carries the escrowed
generations alone. As of 2026-09-25 nothing reads the key: of the ops
repository's workflows, `lint.yml` checks the workflow files
themselves and `probes.yml` runs `state-backend probe`, and neither
opens a dump, while the rebuild drill's workflow is not written
(`kluster-ops#57`). A rebuild is an operator opening the object with
the kit. The rest of this paragraph is the argument for holding it
there. It adds no new
*class* of exposure. The kluster CI already reads the live database
through its client cert, so a dump-decrypting key in a second GitHub
repository widens the reach of a forge compromise by one repository
rather than by a kind of access. Privacy is all the repository
setting can give — GitHub Free grants a private repository neither
branch protection nor rulesets (framework/github.md §2) — so the
containment is the one architecture.md §4.3 builds in their place:
an Environment secret is delivered only to a job whose workflow
names that Environment, and the sole credential aimed at the ops
repo (the dispatch App) cannot write a workflow file, holding no
`workflows` permission. Its accepted residual, writing non-workflow
files to the default branch, buys no path to this key. The key's own
scope is the remainder of the argument, and it has two halves that are
not the same sentence. Its *contract* is the newest dump alone — that
is what the drill relies on, and why it has no generational pair —
while the escrowed offline generations keep the retention role and
survive the loss of GitHub itself. Its *exposure* is every dump
written since it became a recipient and still in retention, at most
the retention window and none before; the payload underneath is Pulumi
state whose secrets sit under the state passphrase, which lives in the
*kluster* Environments and never in the ops repository, so what an
exposed key reads in the clear is the resource graph and the
non-secret configuration. With the key held there, the state-backend
rebuild drill can run unattended end to end once its workflow exists.

**As of 2026-09-25, nothing in the table below runs.** One row has its
workflow: of the credential expiry tripwires, the state-backend server
certificate's is scheduled daily in the ops repository's `probes.yml`
(state-backend.md §6), which has started no job, because no job in
that repository starts until its Actions billing is restored
(`kluster-ops#393`). The other ops-repo rows have no workflow written
(`kluster-ops#57`): not the state-backend rebuild, and not the etcd
restore-verify, nor the scheduled snapshot it would restore. The
in-cluster rows wait on the `k8s-base` stack that would install the
operators the two restores drive and the alerting every in-cluster row
reports into (§1), and the orphan-volume audit's recipe (storage.md
§3.3) is not written either. The last column is the designed form
rather than a census of schedulers: every row is an operator act, and
the only one written out as a procedure is the state-backend restore
(state-backend.md §7.3.1). By the standing principle above, that makes
every recovery path on this table an assumed-broken one.

| Drill | Cadence | Form (designed) |
| --- | --- | --- |
| CNPG restore (immich pattern, ported from legacy) | Monthly | In-cluster workflow, alert on failure |
| State-backend rebuild — scratch micro from Butane → restore latest dump (drill key) → verify → destroy (state-backend.md §7.3) | Quarterly | Ops-repo workflow, alert on failure |
| etcd snapshot restore-verify — latest B2 snapshot into a scratch etcd, health + key sanity | Monthly | Ops-repo workflow, alert on failure |
| VolSync spot-restore — rotating PVC into a scratch namespace, checksum, tear down | Monthly | In-cluster workflow, alert on failure |
| Orphan-volume audit, target zero (storage.md §3.3) | Quarterly | In-cluster workflow, alert only on findings |
| Credential expiry tripwires (credentials.md §4) | Continuous (scheduled probes) | Ops-repo probes; `actionable` alert when an expiry approaches |
| **Offline day**: age key rotation (proves offline custody, state-backend.md §7.4) + full cold-standby reverse bootstrap on homelab libvirt (nodes.md §5) + offline-kit verification against the register (credentials.md §2.1) + a `pulumi preview` against the Vultr-fallback stack config (nodes.md §3.1 — proves the scripted fallback still computes, creating nothing) + anything the probes can't reach | Yearly | One `actionable` issue, human-run |

**Destroy dates are not on that clock, and only one of them is a date.**
A retired **kit** owes nothing forward: `credentials kit rotate` re-wraps
every generation to the successor recovery key inside its own run, so
once `credentials derived check` passes and each generation is confirmed
to open under that key, the retired database is destroyable on a
property rather than after a wait (credentials.md §4.2). A retired **backup generation** keeps a real
clock — its escrow ciphertext is the only copy of the identity that
opens the dumps written under it, so it stays until the last of those
objects falls out of the B2 prefix's retention (state-backend.md §5).
Nothing records that date: `credentials kit rotate` leaves the retired kit
byte-for-byte as it was and a generation's ciphertext carries no expiry,
so a probe has no field to read. Honoring it is part of the yearly
offline day until the register carries it.

Every scheduled drill above belongs in the **ops repo** (ci.md §3 —
the deployment repo carries no scheduled workflows; the in-cluster
drills are the exceptions: VolSync spot-restore and the CNPG restore,
kube-native scratch-namespace operations driven by the cluster itself,
so the ops repo never needs a kubeconfig; and the orphan-volume audit,
which compares each node's on-disk volume directories against the live
volumes and so needs what only a node can read). Each automated drill
is to be covered against stopping silently — an ops-repo drill by the
dead-man below, an in-cluster one by a **freshness alert** (the
backup-freshness family, cluster-infra.md §3) — because a drill that
silently stops running is indistinguishable from a failing one. Until
the workflows, the dead-man and that alert family exist, a drill that
never started is indistinguishable from both. The only calendar ritual
left is the yearly offline-day issue.

**The alert contract.** Every ops-repo drill and probe above, and
every workflow here that fails on `main` (framework/ci.md §3 names
any it still excuses), reports through the dispatch intake
(architecture.md §4.3); the in-cluster drills report through
alertmanager, outside this contract. What every side of the intake
agrees on is fixed here, and it is the contract the Home Assistant
side is built against:

-   **The event.** One `repository_dispatch` into the ops repo, whose
    event type, tiers and payload fields are the census
    `kluster.conventions.alert`, which says what each field holds.
    A source is `<repo>/<workflow>`, the workflow named by its file
    name without the suffix. CI sends the event through its producer,
    `alert.yml` (ci.md §3); the ops repo's own workflows are to send
    the same event with their own token, so there is one intake and
    one payload.
-   **The handler's post to Home Assistant.** The ops repo's dispatch
    handler opens or comments the `actionable` issue — one open issue
    per `key`, labeled `alert`, assigned to the operator — and then
    POSTs `{tier, source, summary, playbook, run, issue, push}` to the
    webhook in its `HA_WEBHOOK_URL` secret. `push` is true for
    `notify`, true for `actionable` when the issue is new or the issue
    step failed, and false for a repeat and for `heartbeat`. A failed
    POST is escalated to an issue of its own.
-   **Home Assistant's side.** One intake automation, a webhook
    trigger that accepts `POST` from the internet, with two branches
    on the body: restart the source's timer when the source has one,
    and, when `push` is true, a phone notification titled
    `[<tier>] <source>: <summary>` whose message is the playbook and
    whose tap target is the issue, or the run where there is none. The
    webhook id is the whole of Home Assistant's credential; it holds
    no GitHub token. The legacy deploy-failure automation beside it
    is kluster-code's and keeps its own id until that repository's
    cutover; this repository's `deploy.yml` posts to it too until it
    calls the producer.
-   **The dead-man.** Every scheduled ops-repo workflow is watched by a
    Home Assistant timer, `timer.kluster_ops_<workflow>` (the
    workflow's file name without the suffix, hyphens as underscores),
    whose duration is `conventions.backup.max_age` of
    the workflow's cadence and which survives a restart; every post
    from `kluster-ops/<workflow>` restarts it, whatever its tier, and
    its expiry pushes "no check-in from kluster-ops/<workflow>". Its
    playbook is that workflow's Actions page: disabled
    (`gh workflow enable`), a run that never started (fire it by
    hand), or a green run whose post landed on a webhook id nothing
    is registered under (`credentials derived sync --only
    haos-webhook`).

What is built of it is the census and CI's producer. The handler, the
ops repo's own intake, and the Home Assistant automation and timers
are not: until the handler exists, a dispatch is accepted and starts
nothing, and a CI alert is a red run and nothing more.

## 5. Playbook index

The unified alert channel requires every alert to name its playbook
(architecture.md §4.3); this index is where the names resolve.
Owning docs keep the content — the index only locates it.

| Playbook family | Lives in |
| --- | --- |
| State backend (cert/CA, PG major, rebuild, age rotation) | physical/state-backend.md §7 |
| Gateway (ZeroTier container down, firmware-wiped device, UDM replacement) | physical/gateway.md §3 |
| Node replacement (CP node, worker VM, block volume and VIP extras) | §3 here |
| Upgrades (Talos serial, Cilium canary) | §2 here |
| Backup restores (CNPG, VolSync, etcd) | storage.md §5; the drills are §4 here |
| Alert-channel failure (HA push down → meta-alert; GitHub leg down; a scheduled ops-repo workflow that stopped checking in) | architecture.md §4.3; the contract and the dead-man are §4 here |
| CI alerts (a red `checks` or `images` run; a drift diff) | framework/ci.md §3.1 and §3.2 |

vmalert rule families adopt this index as they are ported: an alert
that cannot point at a row (or at its owner doc's census) does not
ship — the same-change rule.
