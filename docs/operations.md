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
a run of the `state-backend` stack, `operator-stack state-backend up`. **The rest wait on the `k8s-base` stack**,
the dependency bumps included; the UDM firmware row waits on nothing,
being the vendor's, the row of renovate's `toolchain` group on nothing
either, its merge being its apply path, and the gateway's dash row on
nothing: its pin is an edit to `mise.toml`, and the declared base that
moves with it reaches the device through a deploy of the `physical`
stack.

For the rows whose apply path is the CI chain — the Cilium and
k8s-base charts, the cluster images, the blog image git-sync would
deliver — a merge deploys none of them: the chain stalls in front of
their stacks, and the deploy-failure alert the images row names as its
safety valve, which is built and is the interim Home Assistant webhook
(ci.md §3), fires on every merge alike, so it singles out nothing. The
dependency bumps wait one step further back. The noop-automerge
workflow is built, and it merges a bump only when every path the bump
changes is on its allow-list — `uv.lock`, and renovate's bump of
`Pulumi.yaml`'s `packages:` block with the `sdks/` it regenerates
(ci.md §3). A bump that also edits
`pyproject.toml`, a workflow file, `mise.toml` or an image's `.conf`
waits for a human outright. One that stays on the list proves itself
with a preview of `k8s-base` and `apps` among others, and both stacks
fail that preview until they hold a copy of the kubeconfig, which
waits for the second milestone's first step (§2.5) — so the proof
errors rather than passing. The whole account, the other facts
behind that error included, is ci.md §5. Every bump therefore merges
the way the waiting rows do: as a reviewed pull request.

**A release CI executes waits out a week; one the cluster only pulls
does not.** Renovate proposes a release on a route whose release CI
executes with credentials in reach only once it is seven days old, and
proposes one on a route whose artifact the cluster only pulls as soon
as it is published. Today the first class is the GitHub Actions and
their runner images, the Python dependencies, the tools `mise.toml`
pins other than the gateway's dash, which no route moves, the ZeroTier
release the CI member installs, the Pulumi providers and the bridged
providers with their bridge, and the packages a self-built image's
recipe fetches while CI builds it. The Talos row
and the state-backend age move with a tool CI runs, so they wait as
well. The second class is the container images and the Helm charts. The
Gateway API definitions reach the cluster the way a chart does, and wait
all the same: the exemption is one rule keyed by data source alone, a test
holds it to that shape, and the definitions' data source is also the CRD
generator's, which waits.
uv resolves under the same age whatever it picks itself: renovate's
lock file maintenance, and the libraries a bump pulls in. The criterion
and its reason are `renovate.json5`'s top-level `minimumReleaseAge`.
`pyproject.toml`'s `exclude-newer` repeats the age for uv, and a test
holds both. A data source that reports no publication time cannot be
aged, so renovate holds its releases on the dependency dashboard until
a person ticks them there. Two such sources feed waiting rows: the
ZeroTier `deb` index and the runner labels.

**A pin that records its bytes holds them; one that names a version
does not.** A release asset uploaded again under its version moves
under a pin written as a version alone, so wherever the tool can record
a hash and check a download against it, the pin carries one. Today that
is `uv.lock`'s hashes, the Actions pinned by commit, the container
images this repository names, written with their digest, the Helm charts
served from an OCI
registry, written with the digest of their manifest, which Helm pulls
them by, the Gateway API definitions, written with their asset's
sha256, which `kluster.lib.release_assets.fetch_manifest` checks the
download against, and `mise.lock`, which records the sha256 of each
`mise.toml` tool's artifact. `mise.toml` declares its
tools locked, so an install refuses a pin the lock has no entry for,
and a bump of any `mise.toml` pin, the Talos and age rows' included, is
finished when `mise lock` has moved its entry: renovate does that on
the branch where its host lets it run mise, and a person does it
otherwise. A lock diff that changes a sum and not its version is a
release uploaded again under its name, and is read as one. Every other
pin names a version alone — today the Pulumi provider plugins, the
bridge and the providers it fetches (`Pulumi.yaml` says why beside its
`packages:` block), the Talos factory images and the Helm charts served
from an HTTP repository, which offers nothing to check a download against
(`Pulumi.yaml` says so beside them).

| Surface | PR opened by | Applied by | Policy |
| --- | --- | --- | --- |
| Talos version (machine-config pin + Image Factory schematic + `mise.toml`'s `talosctl`) | renovate (GitHub-releases datasource) | `physical` stack for the pin and the schematic; `talosctl upgrade` by hand, one node at a time in §2.1's order (declarative/physical.md §2) | Reviewed; one pull request moves `versions:talos` and the `talosctl` pin together, because `tests/test_talos_validate.py` holds the two equal; §2.1 runbook |
| Kubernetes version | same PR family (Talos-coupled) | `talosctl upgrade-k8s` | Reviewed; after the Talos bump it belongs to |
| Cilium chart | renovate — the custom manager for an OCI chart's `versions:chart-` pin in `Pulumi.yaml`, moving its version and digest together; alone in a `cilium` group, out of the `in-cluster` one, the way Talos travels alone | CI chain (merge = deploy) | Reviewed, **never automerged** — §2.2 runbook; ≥1.20 floor (ExternalAuth), checked by `update_crds`, which the bump is finished by running on its branch: the chart's version is the ref Cilium's definitions are rendered from |
| k8s-base charts (cert-manager, CNPG, VolSync, sealed-secrets, VictoriaMetrics, …) and the Gateway API definitions | renovate — custom managers on the `versions:chart-` pins (the Helm data source for an HTTP repository, the docker one for an OCI registry, which moves the digest with the version) and on the `versions:manifest-` pin (its release and sha256 together); one `in-cluster` group, Cilium excepted, with the cluster images the block pins | CI chain | Reviewed — chart bumps always produce a real diff; major behind dashboard approval. A bump of a pin `update_crds` reads is red until `update_crds` runs on its branch, because `packages/crds` records the pins it was generated from (framework/pulumi.md §4). `mise.toml`'s `kubeseal` moves in this group's pull request with the sealed-secrets chart: renovate's mise manager reads that pin too, and a rule takes it out of the `toolchain` group into this one, because a test holds it equal to the chart's `appVersion` (framework/pulumi.md §4). The tool's half waits out the release age, which the chart's does not, and is finished by `mise lock`, as a bump in the `toolchain` group is |
| **Cluster images, infra and app alike** (CNPG operands, self-built, third-party app images) | renovate | CI chain (merge = deploy) | **Minor automerge** (patch stream folded into minor — the legacy `patch: enabled: false` precedent — except for local-path-provisioner: each of its releases is a `v0.0.N` patch, so a package rule re-enables its patches alone); **major behind dashboard approval + review**; CNPG operand major additionally gated on the self-built image line (workloads.md §4). The safety valve is the deploy-failure alert, not a per-bump eyeball — this deliberately reverses legacy's "applications get eyeballed" stance. A self-built image (ci.md §4) is published under a fingerprinted tag, `<TAG>-<12 hex digits>`, and a published tag never changes, so a consumer takes a new build by moving to its new tag; renovate offers that move only under a versioning that matches the fingerprint without capturing it, such as `regex:^(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)-[0-9a-f]{12}$` for emailproxy. Today's consumer is kluster-code, which pins these images by plain tag. Of what a self-built image is built from, the commit golinks is built from moves by hand, because upstream publishes no releases; a `pgvecto.rs` release (in `vchord-cnpg.conf`) waits on dashboard approval, because it is an extension upgrade of immich's existing database; every other version and base digest arrives as an ordinary renovate pull request, the digest refreshes grouped once a month |
| blog image / built branch | blog repo CI | git-sync | Automatic — content, not code |
| nspawn rootfs (caddy, AdGuard, ZeroTier) | renovate here — docker datasource, reading whole references off the `versions:image-gateway-…` pins | `physical` stack: the device pulls the pinned manifest itself and unpacks it beside the tree it is running, then the boot chain's machine script restarts what changed | Reviewed; the run-number tag reads as a major bump, so every one waits on dashboard approval |
| State-backend pins: the Fedora CoreOS release the image is imported from, with its artifact's digest (`settings.FCOS_RELEASE`, `settings.FCOS_ARTIFACT_SHA256`); the Postgres image, its major line's tag with the digest the box runs (`settings.POSTGRES_IMAGE`, rendered into the Butane file); the age release (`settings.AGE_VERSION`) | Zincati (periodic window) for the running OS; renovate for the three settings, one custom manager each on `state_backend/settings.py` — the release and its digest captured together, read off the stream's own metadata through a custom data source; the Postgres tag and digest captured together, the digest's moves taken monthly | Zincati: auto. Nothing on the box moves Postgres. A release bump: `operator-stack state-backend up`, which imports a new image and replaces nothing. A Postgres digest or major, or an age bump: `operator-stack state-backend up --force`, which dumps the box, replaces it and restores into the new one (state-backend.md §7.2) | Reviewed, grouped apart from everything else because each bump changes the box, and all but the release replace it. An age bump is one pull request moving `settings.AGE_VERSION` and the `age` pin in `mise.toml` together: renovate's mise manager reads that pin too, and a rule takes it out of the `toolchain` group into this one, because `tests/test_age.py` holds the two pins equal. The pull request is finished by hand: `AGE_SHA256` beside `settings.AGE_VERSION` is a digest renovate cannot recompute, and the `checks` workflow's `state-backend pins` refuses it stale. state-backend.md §1–2, and §7 for the age pin |
| Pulumi SDK + providers, Python deps, Actions versions, `jdx/mise-action` excepted (the `toolchain` group's row) | renovate | **noop-automerge workflow** — merges a bump that changes nothing but paths on its allow-list (`uv.lock`, the `packages:` block with the `sdks/` it regenerates) once the preview is proven empty (the zero-diff rule, ci.md); that proof previews stacks that do not exist yet, so nothing takes this path today (paragraph above) | Automerged when on the list and diff-free; a bump that edits any other path — `pyproject.toml`, an action pin in a workflow — or that produces a real diff is human review; major behind dashboard approval |
| Every tool `mise.toml` pins under `[tools]`, with uv's second pin (`renovate.json5`'s `constraints.uv`) and mise itself (the `jdx/mise-action` pins in the workflows with the `version:` input they pass it, and `deploy/cloud-session/toolchain.sh`'s `mise_version`), but a pin a rule after the `toolchain` rule takes into another group, today `talosctl` (the Talos row), `age` (the state-backend row) and `kubeseal` (the k8s-base charts row), and `http:dash`, which renovate does not read (the gateway's dash row) | renovate — its `toolchain` group: the mise manager's dependencies, the `renovate-config` manager's `uv`, read from the releases the `mise.toml` pin is read from, and the two mise names, `jdx/mise-action` and `jdx/mise`. Rules after it take pins out of the group, today `talosctl`, `age` and `kubeseal`, and with them a major release of the local `postgres` client, which goes into the state-backend appliance group because a test holds its major equal to the appliance image's. `kubeseal` goes into the `in-cluster` group, the k8s-base charts row's, because a test holds it equal to the sealed-secrets chart's `appVersion` | the merge: every CI job that runs a `mise` command installs mise at the workflows' pin and the tools from `mise.toml` and `mise.lock`, and a cloud session does the same at its start, through `deploy/cloud-session/toolchain.sh`, whose `mise_version` a test holds equal to the workflows'; a workstation takes the tools at its next `mise install` | Reviewed — every file the group edits, `mise.toml` among them, is off noop-automerge's allow-list, so a person merges it; the group travels alone because a pulumi, uv or mise bump changes how every other check runs; major behind dashboard approval. A bump of a `mise.toml` pin is finished by the `mise lock` that moves its entry in `mise.lock` (the paragraph on recorded bytes above): renovate runs it on the branch where its host lets it run mise, and a person runs `mise lock <tool>` onto the branch otherwise. The pins of mise itself have no entry: they name a version alone (threat-model.md §2.6). A rebased `toolchain` pull request can arrive with the lock missing the bumped versions, which `checks` then refuses at the install (Aetf/kluster-ops#655). The `gawk` pin stays on the appliance's line, within which renovate offers nothing: the appliance's Fedora CoreOS major moves it, by the re-check its comment in `mise.toml` describes, which renovate's pull request for that major asks for in its body |
| The gateway's dash (`mise.toml`'s `http:dash`, the shell the suite runs the device's `#!/bin/sh` templates under) | `gateway-health`'s reading of the device's base, in place of a pull request: no dash release makes the pin stale; a UniFi firmware that changes the gateway's base or its `/bin/sh` does, and the vendor applies firmware on its own schedule (the UDM firmware row). The reading compares the device's Debian release and what its `/bin/sh` resolves to with the base `Gateway` declares, and the daily run of `check-gateway` mails a mismatch (physical/gateway.md §1.6), naming the playbook "The device's base moved" in physical/gateway.md §3, which is the trigger to move the pin | a person, from that playbook: for another Debian release, the pin moved to that release's dash, its URL and sha256 with it, then `mise lock`; for a `/bin/sh` other than dash, the suite's shell for the device's `#!/bin/sh` templates replaced (`tests/device_shell.py`), with the pin that installs it; then `Gateway`'s declared base moved to what the device reads, which a `physical` push delivers and which clears the finding | Reviewed, with no preview to read: no stack reads the pin, so what the reviewer reads is the suite's run of the device templates' cases under the new pin. The pin follows the device's base rather than dash's newest release (`mise.toml` says why beside it) |
| UDM firmware | **vendor-controlled** (auto-update schedule; outage history on record) | — | Not ours to pin; the device's services self-heal via on_boot.d, overlay recovery runbook gateway.md §3. A firmware that changes the device's base or its `/bin/sh` is the gateway's dash row's trigger |

## 2. Upgrade runbooks (census)

-   **§2.1 Talos node upgrade.** Trigger: version-pin PR merged.
    Gist: one cloud node first as canary (health + workload
    settle), then the remaining nodes serially — never two quorum
    members at once (the CI-serialization rule,
    declarative/physical.md §2);
    homelab worker last; `upgrade-k8s` afterward as its own step.

    **Each node is upgraded to the installer its own configuration
    names, never with a bare `talosctl upgrade`.** A bare one installs
    the stock `ghcr.io/siderolabs/installer`, which is built from no
    schematic, so the worker comes back without `siderolabs/i915` and
    `siderolabs/mei`. Talos never consults the configuration's
    installer for an upgrade: `talosctl` sends whatever its `--image`
    flag holds, whose default is the stock image
    (`cmd/talosctl/cmd/talos/upgrade.go` in Talos v1.13.9), and the
    node reads `machine.install.image` only in its install task, which
    runs on a disk with no META partition — never on a node booted from
    a factory disk image (`v1alpha1_sequencer_tasks.go`, `Install`).
    The configuration names the factory installer built from the
    schematic the node boots (declarative/physical.md §2), and the pin
    PR's `physical` apply has moved its tag to the new release.

    Each node, in the order above, takes two commands, run from the
    checkout, with `<node>` the address `talosctl` names that node by.
    The first reads the installer the node holds and prints it:

        installer="$( { talosctl -n <node> get machineconfig persistent -o jsonpath='{.spec}' 2>/dev/null || talosctl -n <node> get machineconfig v1alpha1 -o jsonpath='{.spec}'; } | mise x uv -- uv run python -c 'import sys, yaml; print(next(d for d in yaml.safe_load_all(sys.stdin) if d and "machine" in d)["machine"]["install"]["image"])')" && echo "$installer"

    It prints `factory.talos.dev/<platform>-installer/<schematic
    id>:<release>`. The tag is the release the upgrade installs, so the
    second command runs only once the tag is the new pin:

        talosctl -n <node> upgrade --image "$installer"

    The first command reads the `persistent` configuration where the
    node has one, and the active `v1alpha1` otherwise. An apply that
    needs a reboot is staged rather than applied (`apply_mode:
    staged_if_needing_reboot`, declarative/physical.md §2), and the node
    judges the whole document against its active one, so a pin bump
    stages too whenever any reboot-requiring change is pending on that
    node. A staged apply writes only `persistent` (`v1alpha1_server.go`,
    the `STAGED` case), so `v1alpha1` then still names the old tag; the
    reboot that ends the upgrade applies the staged document with it. A
    node that has rebooted since its last apply has no `persistent`:
    Talos does not write it again for a configuration loaded from STATE
    (`internal/app/machined/pkg/controllers/config/acquire.go`), and
    `v1alpha1` is then the whole answer. A failed read prints nothing
    and stops at the `&&`, so no upgrade starts with an empty image. The
    configuration stores itself as one YAML string, so no `jsonpath`
    reaches the field, and the YAML is parsed.

    After the worker's upgrade, `talosctl -n <node> get extensions`
    still lists both extensions.
-   **§2.2 Cilium upgrade.** Trigger: chart PR. Gist: the riskiest
    bump in the system — before merge, re-run the affected subset of
    the bootstrap verifications on the preview environment of one
    node (LB-IPAM node-IP pools, reserved-IP NAT, MTU over KubeSpan,
    ExternalAuth fail-closed); merge deploys; watch the
    Envoy/agent rollout complete before calling it done.
-   **§2.3 State-backend lifecycle** — owned by
    physical/state-backend.md §7 (pointer, not a copy).
-   **§2.4 CNPG major upgrade** — owned by workloads.md §4
    (pointer).
-   **§2.5 The cluster credential's copies.** Trigger: a `physical`
    apply that changes its `kubeconfig` output, which no operator sees
    happen. The Talos provider's `Kubeconfig` resource issues the
    cluster-admin client certificate again by itself in whichever
    `physical` apply runs within its renewal window of the certificate's
    expiry — 720 hours by default, and `physical` sets none — so a CI
    `up-physical` re-issues it; replacing the cluster's PKI changes it
    too. Cadence: the first copy is the first step of the second
    milestone (`kluster-ops#77`), and from then on after every
    `physical` `up`, and at least monthly, which no 30-day window can
    slip past. `physical`'s first `up`, in the first milestone, is
    followed by no copy: the commit of both stack files is an `up` of
    `k8s-base` and `apps`, whose Environments take no reviewer, and the
    bring-up's checks that need no network plugin run before Cilium
    exists (physical/gateway-cutover.md §8.1). Gist: `credentials derived sync
    --only kubeconfig` from the checkout that holds the kit, then commit
    `Pulumi.k8s-base.yaml` and `Pulumi.apps.yaml`, whose copies the two
    stacks open their Kubernetes provider with (credentials.md §3). A
    stack already holding the current value as a secret is left alone,
    so a run writes only what changed. A stale copy keeps opening the
    cluster only while the certificate it carries is valid and its
    authority still trusted. Past that, both stacks fail to authenticate
    to the API server — a provider error, not `UnusableKubeconfig`,
    which only an absent or blank copy raises — until the copy is made.

## 3. Node replacement runbooks (census)

-   **§3.1 Cloud CP node.** Trigger: hardware loss, A1 reclamation,
    or deliberate rebuild. Gist: drain → `talosctl etcd
    remove-member` → destroy in `physical` (unprotect if flagged) →
    re-create → rejoin quorum → health gate. Facts that shape it:
    A1 capacity at re-create is the known risk (the standing quorum
    holds capacity; rebuild promptly), NLB backends follow from
    `physical` automatically; a node carrying a **block volume** needs
    it reattached, through the program (declarative/physical.md §6),
    and the node holding the **dedicated VIP** needs the secondary
    private IP and the reserved-IP NAT re-pointed (architecture.md §3.2).
-   **§3.2 Homelab worker VM.** Trigger: VM/disk loss or rebuild.
    Gist: `mise x -- pulumi state unprotect --stack physical '<disk
    URN>'`, then `mise x -- pulumi up --stack physical --replace '<disk
    URN>'`, which
    replaces the disk, the domain and the seed, the seed from the
    current machine config (declarative/physical.md §3) — the way to a
    fresh worker, where a STATE wipe on the kept disk boots the seed as
    it stood at the last rebuild; local-path
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
`src/kluster/lib/state_backend/machine/drill-recipient.txt`; the
appliance encrypts to it from the converge that adopts the committed
file (state-backend.md §5), and a dump written before that converge
carries the escrowed generations alone. As of 2026-09-25 nothing reads the key: of the ops
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
state whose secrets sit under the Pulumi stack passphrase, which lives in the
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
| Gateway (ZeroTier container down, firmware-wiped device, UDM replacement; and every finding of the daily gateway check: a machine, the boot chain, a delivered unit, the routing daemon or the offline package cache, a check that did not run, a certificate not renewing, an autobackup not copied off) | physical/gateway.md §3 |
| Node replacement (CP node, worker VM, block volume and VIP extras) | §3 here |
| Upgrades (Talos serial, Cilium canary) | §2 here |
| Backup restores (CNPG, VolSync, etcd) | storage.md §5; the drills are §4 here |
| Alert-channel failure (HA push down → meta-alert; GitHub leg down; a scheduled ops-repo workflow that stopped checking in) | architecture.md §4.3; the contract and the dead-man are §4 here |
| CI alerts (a red `checks` or `images` run; a drift diff) | framework/ci.md §3.1 and §3.2 |

vmalert rule families adopt this index as they are ported: an alert
that cannot point at a row (or at its owner doc's census) does not
ship — the same-change rule.
