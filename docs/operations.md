# Day-2 Operations

Who and what keeps the system current and its recovery paths proven:
the update-ownership matrix, the upgrade, node-replacement, rebuild and
resize runbooks, and the drill program with its trigger machinery. Sits at
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
a run of the `state-backend` stack, `operator-stack state-backend up`.
The rows whose apply path is the CI chain apply through a merge, as far
as a stack's program reads their pin: the Cilium and k8s-base charts,
the Gateway API definitions and the images of the platform through
`up-k8s-base`. **A pin no wired component reads waits for its
application to land in `apps`** — an application's image, the blog
image git-sync would deliver (cluster/migration.md §2). The UDM
firmware row waits on nothing, being the vendor's, the row of
renovate's `toolchain` and `mise release` groups on nothing either, its
merge being its apply path, and the gateway's dash row on nothing: its
pin is an edit to `mise.toml`, and the declared base that moves with it
reaches the device through a deploy of the `physical` stack.

The deploy-failure alert the images row names as its safety valve is
built, and is the interim Home Assistant webhook (ci.md §3): it fires on
the merge whose chain fails. The noop-automerge workflow is built, and
it merges a bump only when every path the bump changes is on its
allow-list — `uv.lock`, and renovate's bump of `Pulumi.yaml`'s
`packages:` block with the `sdks/` it regenerates (ci.md §3) — and its
preview of `dns`, `k8s-base` and `apps` is proven empty. A bump that
also edits `pyproject.toml`, a workflow file, `mise.toml` or an image's
`.conf` waits for a human outright, and one whose preview finds a diff
is reviewed like any other pull request.

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
finished when `mise lock` has moved its entry: renovate runs it on
the branch, with the mise CI checks the branch with, and a person runs
it there when renovate's run fails or is skipped. A lock diff that changes a sum and not its version is a
release uploaded again under its name, and is read as one. Every other
pin names a version alone — today the Pulumi provider plugins, the
bridge and the providers it fetches (`Pulumi.yaml` says why beside its
`packages:` block), the Talos factory images and the Helm charts served
from an HTTP repository, which offers nothing to check a download against
(`Pulumi.yaml` says so beside them).

**A chart bump's review reads the chart's changelog for every key the
program sets on it, unless the chart's schema refuses a key it does not
declare.** The values a component hands its chart, the `values` of its
`helm_chart` call, are held to the chart a bump brings in one respect:
`tests/test_chart_values.py` holds every path they set to the paths the
chart has at its pinned version (`packages/crds/chart-values.json`,
[framework/pulumi.md](framework/pulumi.md) §4), so a bump that renames
or removes a key the program sets is red until the component follows
it. A key the new version keeps and reads differently, or keeps and no
longer reads, passes that test. Cilium's `rollOutCiliumPods` is one
such key: it is what makes a change to the values replace the agent's
pods (declarative/cluster-infra.md §2), and a release that kept it in
`values.yaml` and dropped its templating would pass. A chart whose
`values.schema.json` sets `additionalProperties: false` at every map the
program sets keys in declares its interface in that schema, and refuses
a key it does not declare; today that is the cert-manager chart alone.
For every other chart, one that ships no schema or whose schema is open
at those maps, the bump's review reads the chart's changelog between the
two versions for each key the program sets, and says on the pull
request what it found for each.

**Beside the keys, the same review reads the changelog for every CronJob
and Job the new version renders or stops rendering.** The scheduled-job
rule family watches a CronJob through its row in `conventions.schedule`
(§4), and `tests/test_monitoring.py` holds every CronJob the `k8s-base`
and `apps` programs declare to a row, and every row to one of them. A
CronJob a chart renders is none of those: it has no row, a run of it
that fails or stops raises nothing, and no check turns red when a bump
brings one in. For each one the new version renders, the pull request
names it, says what it runs, and says whether a run of it failing or
stopping would go unnoticed in a way that matters. One that needs
watching is filed as an issue of its own in `kluster-ops` and linked
from the pull request; the bump adds no row. A Job is outside the
family, which reads a CronJob's series alone, so for each Job the pull
request says what it runs. One the new version stops rendering is named
as well. The pull request says what the review found, including that the
chart renders none.

**A `pulumi` bump's review reads the changelog for every resource option
and hook a declaration under `src/` sets**, beyond `parent`, `provider`
and `providers`, a regression in which every preview shows: a URN that
moves, or a resource sent to a disabled default provider. What the
engine does with such an option is held by no test of its own: each
option carries its reason at its declaration, and the release that
changes what the engine does with it passes every check. Today those are
`protect`, `ignore_changes` (a nested path included),
`replace_on_changes`, `replace_with`, `delete_before_replace`,
`depends_on` (one naming a component, a chart included, orders after
that component's children: `after_the_chart` in
`components/certificates`, `components/cilium` and
`components/monitoring`), `additional_secret_outputs`, `aliases`,
`import_`, `transforms` and the resource hooks (`before_create`, `after_create`, `before_delete`), a
delete running the binding the state recorded, by name. For each, the
review reads what the engine does under a targeted run (`--replace`,
`--target-replace`), a refresh (`pulumi refresh`, `--refresh`) and a
`destroy`. The review of the CLI's pin, which travels in the `toolchain`
group and waits for a person, reads the changelog between the two
versions for each of them and says on the pull request what it found
for each; so does the review of an SDK bump that waits for a person. An
SDK relock inside its bounds merges through noop-automerge, read by no
one. The SDK carries the options to the engine, which decides what to do
with them. A hook is the one the SDK runs itself: it calls the function
with the arguments it builds from the engine's request. On a relock,
`tests/test_homelab_engine.py`'s rebuild cases run a `before_delete`
hook through the locked SDK in `checks`. No check runs a `before_create`
or an `after_create` hook, so a relock that breaks one surfaces at the
next replacement of the `state-backend` box, whose `permit` and
`restore` they are. Which provider the SDK sends a `crds:` resource to
is the SDK's own, and the zero-diff rule's preview of `k8s-base` is what
answers for it on that route: the stack turns the default `kubernetes`
provider off, so a resource sent to the default provider fails that
preview with `denydefaultprovider not registered`.

| Surface | PR opened by | Applied by | Policy |
| --- | --- | --- | --- |
| Talos version (machine-config pin + Image Factory schematic + `mise.toml`'s `talosctl`) | renovate (GitHub-releases datasource) | `physical` stack for the pin and the schematic; `talosctl upgrade` by hand, one node at a time in §2.1's order (declarative/physical.md §2) | Reviewed; one pull request moves `versions:talos` and the `talosctl` pin together, because `tests/test_talos_validate.py` holds the two equal; §2.1 runbook |
| Kubernetes version | same PR family (Talos-coupled) | `talosctl upgrade-k8s` | Reviewed; after the Talos bump it belongs to. The `kubectl` pin in `mise.toml` and the `allowedVersions` bound on it in `renovate.json5` move in that Talos pull request, by hand, when the release defaults to another Kubernetes minor: `tests/test_cluster_clients.py` holds the pin's minor equal to that default |
| Cilium chart | renovate — the custom manager for an OCI chart's `versions:chart-` pin in `Pulumi.yaml`, moving its version and digest together; alone in a `cilium` group, out of the `in-cluster` one, the way Talos travels alone | CI chain (merge = deploy) | Reviewed, **never automerged** — §2.2 runbook; ≥1.20.2 floor (ExternalAuth, which from that release fails closed on a backend it cannot use, [cilium/cilium#47929](https://github.com/cilium/cilium/pull/47929)), checked by `update_crds`, which the bump is finished by running on its branch: the chart's version is the ref Cilium's definitions are rendered from |
| k8s-base charts (cert-manager, CNPG, VolSync, sealed-secrets, VictoriaMetrics, …) and the Gateway API definitions | renovate — custom managers on the `versions:chart-` pins (the Helm data source for an HTTP repository, the docker one for an OCI registry, which moves the digest with the version) and on the `versions:manifest-` pin (its release and sha256 together); one `in-cluster` group, Cilium excepted, with the cluster images the block pins | CI chain | Reviewed — chart bumps always produce a real diff; major behind dashboard approval. A bump of a pin `update_crds` reads is red until `update_crds` runs on its branch, because `packages/crds` records the pins it was generated from (framework/pulumi.md §4). `mise.toml`'s `kubeseal` moves in this group's pull request with the sealed-secrets chart: renovate's mise manager reads that pin too, and a rule takes it out of the `toolchain` group into this one, because a test holds it equal to the chart's `appVersion` (framework/pulumi.md §4). The tool's half waits out the release age, which the chart's does not, and is finished by `mise lock`, as a bump in the `toolchain` group is |
| **Cluster images, infra and app alike** (CNPG operands, self-built, third-party app images) | renovate | CI chain (merge = deploy) | **Minor automerge** (patch stream folded into minor — the legacy `patch: enabled: false` precedent — except where a pin's release line moves by patches alone, as local-path-provisioner's does, each of its releases being a `v0.0.N` patch: a package rule in `renovate.json5` names each such dependency and re-enables its patches); **major behind dashboard approval + review**; CNPG operand major additionally gated on the self-built image line (workloads.md §4). The safety valve is the deploy-failure alert, not a per-bump eyeball — this deliberately reverses legacy's "applications get eyeballed" stance. A self-built image (ci.md §4) is published under a fingerprinted tag, `<TAG>-<12 hex digits>`, and a published tag never changes, so a consumer takes a new build by moving to its new tag; renovate offers that move only under a versioning that matches the fingerprint without capturing it, such as `regex:^(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)-[0-9a-f]{12}$` for emailproxy. Two consumers pin these images today: kluster-code, by plain tag, and this repository's `Pulumi.yaml`, by fingerprinted tag and digest, under a versioning `renovate.json5` gives each image. Of what a self-built image is built from, the commit golinks is built from moves by hand, because upstream publishes no releases; a `pgvecto.rs` or `vchord` release (both in `vchord-cnpg.conf`) waits on dashboard approval, because each is an extension upgrade of immich's existing database; `oci-sdk`'s `oci` release arrives once a month, in the window of the digest refreshes, since the SDK releases about weekly; the `python` base of `oci-sdk` and of `postgres-client` is held to its line by an `allowedVersions` bound in `renovate.json5`, because each image runs this repository's Python and carries the line `.python-version` names (ci.md §4), so a person moves the bases and the bound in the pull request that moves `.python-version`, which `tests/test_images.py` holds red until all of them have moved; `postgres-client`'s Postgres major moves by hand in the pull request that moves the state backend's, which the same test holds red until it has; `etcd-age`'s etcd follows the minor the cluster's etcd runs, so its minors and majors wait on dashboard approval, given with the Talos release that moves that minor, whose pull request says so, and its patches are not offered; every other version and base digest arrives as an ordinary renovate pull request, the digest refreshes grouped once a month |
| blog image / built branch | blog repo CI | git-sync | Automatic — content, not code |
| nspawn rootfs (caddy, AdGuard, ZeroTier) | renovate here — docker datasource, reading whole references off the `versions:image-gateway-…` pins | `physical` stack: the device pulls the pinned manifest itself and unpacks it beside the tree it is running, then the boot chain's machine script restarts what changed | Reviewed; the run-number tag reads as a major bump, so every one waits on dashboard approval |
| State-backend pins: the Fedora CoreOS release the image is imported from, with its artifact's digest (`settings.FCOS_RELEASE`, `settings.FCOS_ARTIFACT_SHA256`); the Postgres image, its major line's tag with the digest the box runs (`settings.POSTGRES_IMAGE`, rendered into the Butane file); the pooler's image, a release's tag with the digest the box runs (`settings.PGBOUNCER_IMAGE`); the age release (`settings.AGE_VERSION`) | Zincati (periodic window) for the running OS; renovate for the four settings, one custom manager each on `state_backend/settings.py` — the release and its digest captured together, read off the stream's own metadata through a custom data source; each image's tag and digest captured together | Zincati: auto. Nothing on the box moves either image. A Fedora CoreOS release bump: `operator-stack state-backend up`, which imports a new image and replaces nothing. A move of either image's digest or tag, or an age bump: `operator-stack state-backend up --force`, which dumps the box, replaces it and restores into the new one (state-backend.md §7.2) | Reviewed. The releases are grouped apart from everything else, in the `state-backend appliance` group, because each changes the box, and all but the Fedora CoreOS release replace it; no window holds them. The two images' digest moves under an unchanged tag — the tag rebuilt for a new base, or Postgres's for a minor release — are a group of their own, `state-backend image refreshes`, whose pull request opens on the first of the month only: renovate opens a grouped branch on the schedule of the update it sorts first, so a window holds only for a group in which every update carries it. A month that brings a release of either image or of age and a rebuild opens two pull requests, each of them a replacement of the box. An age bump is one pull request moving `settings.AGE_VERSION` and the `age` pin in `mise.toml` together: renovate's mise manager reads that pin too, and a rule takes it out of the `toolchain` group into the `state-backend appliance` one, because `tests/test_age.py` holds the two pins equal. The pull request is finished by hand: `AGE_SHA256` beside `settings.AGE_VERSION` is a digest renovate cannot recompute, and the `checks` workflow's `state-backend pins` refuses it stale. state-backend.md §1–2, and §7 for the age pin |
| Pulumi SDK + providers, Python deps, Actions versions, `jdx/mise-action` excepted (the row of the `toolchain` and `mise release` groups) | renovate | **noop-automerge workflow** — merges a bump that changes nothing but paths on its allow-list (`uv.lock`, the `packages:` block with the `sdks/` it regenerates) once the preview is proven empty (the zero-diff rule, ci.md) | Automerged when on the list and diff-free; a bump that edits any other path — `pyproject.toml`, an action pin in a workflow — or that produces a real diff is human review; major behind dashboard approval. A `pulumi-kubernetes` release that embeds another Helm SDK than the last moves `mise.toml`'s `helm` and its `mise.lock` entry, by hand, in its own pull request: `update_crds` renders the chart set with that Helm, `tests/test_update_crds.py` holds the pin equal to the release the provider's plugin embeds, and renovate offers the pin nothing (the tools row) |
| Every tool `mise.toml` pins under `[tools]`, with uv's second pin (`renovate.json5`'s `constraints.uv`) and mise itself (the `jdx/mise-action` pins in the workflows with the `version:` input they pass it, `deploy/cloud-session/toolchain.sh`'s `mise_version`, and `renovate.json5`'s `constraints.mise`), but a pin a rule after the `toolchain` rule takes into another group, today `talosctl` (the Talos row), `age` (the state-backend row), `kubeseal` (the k8s-base charts row) and the `vmutils` archive, `github:VictoriaMetrics/VictoriaMetrics` (below); `helm`, which renovate offers nothing because the Kubernetes provider's pull request moves it (the Pulumi SDK and providers row); and `http:dash`, which renovate does not read (the gateway's dash row) | renovate — its `toolchain` group: the mise manager's dependencies and the `renovate-config` manager's `uv`, read from the releases the `mise.toml` pin is read from. mise under its three names, `jdx/mise-action`, `jdx/mise` and the `renovate-config` manager's `mise`, is a group of its own, `mise release`. Rules after the `toolchain` rule take pins out of the group, today `talosctl`, `age`, `kubeseal` and the `vmutils` archive, and with them a major release of the local `postgres` client, which goes into the state-backend appliance group because a test holds its major equal to the appliance image's. `kubeseal` goes into the `in-cluster` group, the k8s-base charts row's, because a test holds it equal to the sealed-secrets chart's `appVersion`. The `vmutils` archive, which puts `vmctl` and `vmalert-tool` on the path, goes into the `victoriametrics` group with the VictoriaMetrics stack chart and the images the monitoring component runs at its release, because a test holds it equal to that chart's `appVersion`. A rule after the `toolchain` rule also takes `helm` out, offering it nothing: the pin is the Helm SDK the Kubernetes provider's plugin embeds, and moves by hand in that provider's pull request (the Pulumi SDK and providers row) | the merge: every CI job that runs a `mise` command installs mise at the workflows' pin and the tools from `mise.toml` and `mise.lock`, and a cloud session does the same at its start, through `deploy/cloud-session/toolchain.sh`, whose `mise_version` a test holds equal to the workflows'; a workstation takes the tools at its next `mise install` | Reviewed — every file either group edits, `mise.toml` and the workflows among them, is off noop-automerge's allow-list, so a person merges it; each group travels alone because a pulumi, uv or mise bump changes how every other check runs; major behind dashboard approval. A bump of a `mise.toml` pin is finished by the `mise lock` that moves its entry in `mise.lock` (the paragraph on recorded bytes above): renovate runs it on the branch with the mise `constraints.mise` names, which a test holds equal to the workflows' pin. Renovate reads that constraint from `main`, so it is the mise a branch's install runs on every branch but the one moving mise, and that is why `mise release` travels apart from the `toolchain` group: it moves no `mise.toml` pin, so it writes no lock, and its pins of mise need none, since they have no entry there and name a version alone (threat-model.md §2.6). Every `mise.lock` change renovate writes is therefore written by the mise that installs it. The `mise release` pull request is where a new mise first reads entries earlier ones wrote, `main`'s lock under its `checks`, and a refusal there is answered by `mise lock` with the new mise onto that branch. A run that fails marks `renovate/artifacts` red and leaves the entry unmoved. Without the host's allow-list, a run whose `mise version` probe fails is skipped: the pull request arrives without the entry and without that mark, and `checks` refuses it at the install. Either way a person runs `mise lock <tool>` onto the branch. The `gawk` pin stays on the appliance's line, within which renovate offers nothing: the appliance's Fedora CoreOS major moves it, by the re-check its comment in `mise.toml` describes, which renovate's pull request for that major asks for in its body. The `kubectl` and `cmctl` pins stay on lines the same way, each under an `allowedVersions` bound in `renovate.json5`: `kubectl` on the cluster's Kubernetes minor, which the Talos pull request moves (the Kubernetes version row), and `cmctl` on the line built against the cert-manager chart's minor, which a person moves beside a chart bump onto another minor |
| The gateway's dash (`mise.toml`'s `http:dash`, the shell the suite runs the device's `#!/bin/sh` templates under) | `gateway-health`'s reading of the device's base, in place of a pull request: no dash release makes the pin stale; a UniFi firmware that changes the gateway's base, its `dash` package or its `/bin/sh` does, and the vendor applies firmware on its own schedule (the UDM firmware row). The reading compares the device's Debian release, the version of its `dash` package, Debian revision included, and what its `/bin/sh` resolves to with the base `Gateway` declares, and the daily run of `check-gateway` mails a mismatch (physical/gateway.md §1.6), naming the playbook "The device's base moved" in physical/gateway.md §3, which is the trigger to move the pin. The pin's version is the package's version, which `tests/test_dash_pin.py` holds equal to the declared base's | a person, from that playbook: for another `dash` package, a revision within the release or another release's, the pin moved to that package, its version, snapshot URL and sha256 with it, then `mise lock`; for a `/bin/sh` other than dash, the suite's shell for the device's `#!/bin/sh` templates replaced (`tests/device_shell.py`), with the pin that installs it; then `Gateway`'s declared base moved to what the device reads, which a `physical` push delivers and which clears the finding | Reviewed, with no preview to read: no stack reads the pin, so what the reviewer reads is the suite's run of the device templates' cases under the new pin. The pin follows the device's `dash` package rather than dash's newest release (`mise.toml` says why beside it) |
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

        installer="$( { mise x -- talosctl -n <node> get machineconfig persistent -o jsonpath='{.spec}' 2>/dev/null || mise x -- talosctl -n <node> get machineconfig v1alpha1 -o jsonpath='{.spec}'; } | mise x uv -- uv run python -c 'import sys, yaml; print(next(d for d in yaml.safe_load_all(sys.stdin) if d and "machine" in d)["machine"]["install"]["image"])')" && echo "$installer"

    It prints `factory.talos.dev/<platform>-installer/<schematic
    id>:<release>`. The tag is the release the upgrade installs, so the
    second command runs only once the tag is the new pin:

        mise x -- talosctl -n <node> upgrade --image "$installer"

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

    After the worker's upgrade, `mise x -- talosctl -n <node> get extensions`
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
-   **§2.5 The cluster credential's copies.** Trigger:
    `KubeconfigCopyExpiring`, an `actionable` alert that fires
    `conventions.cluster.KUBECONFIG_ALERT_MARGIN`, fourteen days, before
    the client certificate in `k8s-base`'s copy of the cluster-admin
    kubeconfig expires (`components.monitoring.rules.kubeconfig_copy`).
    The certificate lives a year. The Talos provider's `Kubeconfig`
    resource issues it again by itself in whichever `physical` apply
    runs within its renewal window of that expiry,
    `conventions.cluster.KUBECONFIG_RENEWAL`, 720 hours, which `physical`
    sets on the resource; a refresh never does. So the copies in
    `k8s-base`'s and `apps`'s configuration go stale about once a year,
    and the alert fires inside the window, where the apply below issues
    a new certificate.

    Earlier signs: from the moment the window opens, every
    `plan-physical` plans an update of `<cluster>-kubeconfig`, so a chain
    run waits on it at `up-physical`'s reviewer gate, and the weekly
    drift run's `physical` entry fails on it. Both are the renewal
    window, not drift.

    Act, from the checkout that holds the kit: `credentials derived sync
    --only kubeconfig`. If it writes `Pulumi.k8s-base.yaml` and
    `Pulumi.apps.yaml`, merge a pull request that commits both. The merge
    is the `up` of both stacks, whose Environments take no reviewer, and
    `up-k8s-base` moves the rule's threshold a year on, which resolves
    the alert. If it writes neither, `physical` has not issued a new
    certificate: approve the `up-physical` a chain run is waiting on, or
    start one with `gh workflow run deploy.yml --repo Aetf/kluster` and
    approve it, then run the sync. The sync may follow any approval of
    that step at once; the alert is for the time it did not.

    A stack already holding the current value as a secret is left alone,
    so a run writes only what changed. A stale copy keeps opening the
    cluster only while the certificate it carries is valid and its
    authority still trusted. Past that, both stacks fail to authenticate
    to the API server until the copy is made. That is a provider error,
    not `UnusableKubeconfig`, which refuses a copy by its form and never
    by its certificate's date: one that is absent, blank or the unknown
    sentinel, and, in `k8s-base`, one that is no YAML or carries no
    embedded client certificate.
    The copy in `apps`'s configuration is not watched apart from
    `k8s-base`'s: the same run writes both, into files one pull request
    commits, and a file left out of it shows at `apps`'s first apply
    after the old expiry, as a failed chain run. Replacing the cluster's
    PKI breaks both copies at once, which no expiry rule sees, so a PKI
    replacement runs the sync as a step of its own, as step 8 of the
    whole-cluster rebuild (§3.5) does for the `Kubeconfig` it issues
    again. In a bring-up the first copy waits for stage 4 of
    physical/bring-up.md §2 rather than following `physical`'s first
    `up`: the commit of both stack files is an `up` of `k8s-base` and
    `apps`, and the bring-up's checks that need no network plugin run
    before Cilium exists (physical/gateway-cutover.md §8.1).

## 3. Node replacement and resize runbooks (census)

-   **§3.1 Cloud CP node.** Trigger: hardware loss, A1 reclamation,
    or deliberate rebuild. **Rule: no instance is destroyed through the
    program.** Each node's instance is protected, so the engine refuses
    to delete it and refuses its replacement, planned or targeted,
    before it creates anything, while a refresh still drops an instance
    that reads back gone (declarative/physical.md §1). A lost node is
    recovered by a targeted `refresh --run-program` and an `up`. A
    deliberate replacement makes the node a lost one on purpose, one
    node per sitting, and takes the same recovery.

    **The peak recorder runs around either path** (§8.5), in a terminal
    of its own at the root of the checkout, from before the refresh, or
    before the termination of a deliberate replacement:

        mise x uv -- uv run perf record .claude/peaks

    Once the `up` has written the new node's address, it is stopped with
    Ctrl-C and started again with the same command, which appends to the
    file and reads that address. After the closing readings it is
    stopped, `mise x uv -- uv run perf import .claude/peaks` writes the
    file into the history store, and the file is deleted.

    The program has no delete-first path for the node holding the
    **dedicated VIP**. A targeted destroy of that instance reaches its
    dependents: the secondary private IP and, through it, the reserved
    public IP. That address is protected because it is
    identity-bearing, and lifting that protection to let the destroy
    through deletes it. OCI keeps a reserved IP whose private IP is
    deleted, unassigned, so both paths below keep the address and
    re-point it with one update of its `private_ip_id`
    (`components/cloud/nodes.py`).

    Facts that shape both paths:

    -   **The re-created node joins by itself** from its `user_data`,
        the cluster being live, and one whose etcd service is `Failed`
        on the join timeout joins after its next boot
        (declarative/physical.md §6). A control plane joining etcd with
        an empty data directory first removes any member that carries
        its own hostname (physical/gateway-cutover.md §5), and every
        node's hostname is its declared name (declarative/physical.md
        §2), so the member a lost node leaves behind goes with that
        join. `tal -n "$CPS" etcd members` after the `up`, with the
        setup taken again from the new outputs, lists one member per
        control plane, and a member still listed for the
        lost node is removed with `talosctl etcd remove-member`.
    -   **A1 capacity at the create is the known risk.** The standing
        quorum holds the capacity of the other two while one create
        hunts, so a lost node is re-created promptly; a create that is
        refused fails that instance alone, and the same `up` again
        retries it.
    -   **What follows the node is the program's.** The balancer's
        backends name the instance or its GUA. A node carrying a
        **block volume** has it attached again by the program
        (declarative/physical.md §6). On the dedicated VIP's node the
        secondary private IP lives on the instance's VNIC, and the
        reserved IP's `private_ip_id` names it. The day-1 chain names
        each node by its private address. A changed `node` updates the
        `Bootstrap` in state and bootstraps nothing: the pinned
        provider's update writes the plan into state and calls no node
        (`talos_machine_bootstrap_resource.go` in
        `terraform-provider-talos` v0.11.0, which `pulumiverse-talos`
        0.8.1 bridges).

    **A lost node.** OCI terminated it: the instance reads
    `TERMINATED`. A node that is up but given up is terminated first,
    by the deliberate replacement's termination step below, with the
    etcd steps it can no longer take left out. From the root of the checkout that holds `.credentials/`,
    with `physical` the function of declarative/physical.md §6, `tal`
    and `CPS` the setup of physical/gateway-cutover.md §8.1, `kub`
    the cluster's `kubectl` under the kubeconfig that setup writes, and
    `<node>` the lost node's name (`cp2`):

        kub() { mise x -- kubectl --kubeconfig ~/.kube/kluster "$@"; }
        ocid() { physical stack export | mise x -- jq -r --arg t "$1" --arg n "$2" '.deployment.resources[] | select(.type == $t and .outputs.displayName == $n) | .id'; }
        credentials derived oci-physical read "$(ocid oci:Core/instance:Instance kluster-<node>)"
        kub delete node <node>
        urns() { physical stack --show-urns | sed -n 's|.*URN: \(urn:.*\)$|\1|p' | grep -E "$1"; }
        S=$(urns '\$oci:NetworkLoadBalancer/backendSet:BackendSet::' | wc -l); echo "$S"
        T=($(urns '::kluster:cloud:CloudNodes\$oci:(Core/instance:Instance::kluster-<node>|NetworkLoadBalancer/backend:Backend::kluster-nlb-.*-<node>)$'))
        printf '%s\n' "${T[@]}"                  # the instance, and its backend in each backend set
        [ "$S" -gt 0 ] && [ ${#T[@]} -eq $((1 + S)) ] && physical refresh --run-program "${T[@]/#/--target=}"
        physical preview --diff
        physical up

    `ocid` prints the identifier that `physical`'s state records for
    the resource of that type and display name. `read` prints the
    instance's state, `TERMINATED` for a node OCI terminated, as OCI
    reports it under `physical`'s own OCI credential, read out of the
    stack's configuration with the passphrase in its workstation slot
    (credentials.md §4.4). `kub delete node` removes the Node object
    the lost machine registered, so that the new machine registers one
    of its own rather than inheriting the old one's cordon and status.
    A URN carries `$`, which the array keeps as
    it is, since the output of a command substitution is not expanded
    again. A component of
    `physical` hangs off the stack itself, so its type opens the URN's
    type chain, after the project's `::`, and a resource it holds
    follows it after a `$`:
    `urn:pulumi:physical::kluster-py::kluster:cloud:CloudNodes$oci:Core/instance:Instance::kluster-cp1`.
    A pattern anchors on the component as `::kluster:cloud:CloudNodes`,
    never as `$kluster:cloud:CloudNodes`, which no URN of the stack
    contains.

    **A list goes to a command only at the count the step knows.** `S`
    counts the balancer's backend sets, one per forwarded port and
    family, and every node has one backend in each. The `[ … ] &&` runs
    the refresh only when the list holds that count, because a list
    that matched nothing leaves the refresh with no `--target`, and a
    `refresh`, `destroy` or `preview` with no target acts on the whole
    stack.

    On the dedicated VIP's node the targets add the
    `oci:Core/privateIp:PrivateIp` and the `oci:Core/publicIp:PublicIp`
    under `kluster:cloud:CloudNodes`, and the
    `oci:Core/volumeAttachment:VolumeAttachment` under
    `kluster:cloud:NodeVolume`: the pattern gains
    `|::kluster:cloud:CloudNodes\$oci:Core/(privateIp:PrivateIp|publicIp:PublicIp)::|::kluster:cloud:NodeVolume\$oci:Core/volumeAttachment:VolumeAttachment::`
    and the count is `$((4 + S))`. The refresh drops what reads back
    without an ID: the instance and, on that node, the secondary private
    IP and the attachment, the attachment whatever its `protect`
    (declarative/physical.md §6, whose fallback applies if it stays).
    The reserved IP stays, its `private_ip_id` reading empty. The
    backends stay as well, because OCI keeps the backends of a
    terminated instance. The preview plans the creates of what the
    refresh dropped, the replacement of each of the node's backends, the
    reserved IP's one update on that node, and changes to the day-1
    resources that name the node's address. Any other plan, the volume
    itself above all, is a diff nobody expected, and nothing is applied
    until it has been read. The `up` ends on the Talos health gate,
    every node named. **The deploy chain follows it, dispatched as at
    the end of §3.4**: `plan-physical` finds no diff, `up-physical` is
    skipped, and `up-k8s-base` and `up-apps` run. OCI picks a
    re-created instance's addresses again, so the node's entries in the
    `node_private_ips` and `node_guas` outputs move, and on the
    dedicated VIP's node the `vip1_private` output besides (§3.5).
    `k8s-base` reads them for the `internet` pool's blocks and the
    Gateways' addresses (`components/cilium`), and `apps` for the
    addresses of the Services the balancer fronts (`components/apps`),
    so until both have run the new node takes no traffic the balancer
    hands it.

    **A deliberate replacement.** The node is up and is to be replaced
    by a new instance. The order is delete-first: the old instance is
    terminated, and the recovery is the lost node's. The A1 quotas hold
    the fleet and no node more (cluster/nodes.md §3.2), so the cloud
    refuses a replacement launched beside the node it replaces. From the
    termination until the new member is promoted, etcd runs on two
    members and tolerates no failure. If the AD refuses the create, that
    lasts as long as the hunt, which may be days (cluster/nodes.md
    §3.1), and a second loss inside it is the rung of cluster/nodes.md
    §5 that loses two nodes, for which this document holds no runbook
    but §3.5. One node per sitting, and no second node until the closing
    readings pass. The first deliberate replacement runs on a node that
    holds neither the dedicated VIP nor a claim reading 3 lists, and the
    dedicated VIP's node is replaced this way only once one has passed.

    The setup is the lost node's above, the `kub` and `ocid` lines of
    its block included, with `ip_of` from the same setup of
    physical/gateway-cutover.md §8.1 and `<node>` the node to be
    replaced.

    **Readings, all of which pass before anything is written.**

    1.  *Quorum is whole.* `tal -n "$CPS" etcd members` lists one
        member per control plane from every node, none a learner.
        `tal -n "$CPS" etcd status` shows one leader, the same on every
        row, and no error. `tal -n "$CPS" etcd alarm list` prints no
        alarm. `kub get nodes` shows every node `Ready`.
    2.  *The AD has capacity.* §3.4's capacity reading, at the declared
        size, passes for the AD the node's instance names, its
        `availabilityDomain` in `physical stack export`, taken in the
        hour before the termination. It is a reading and no reservation:
        the node's place in the AD is given up at the termination.
    3.  *What the node holds is given up knowingly.* This lists the
        class and the claim of every volume whose node affinity names
        `<node>`:

            pv=$(kub get pv -o json) && mise x -- jq -r --arg n <node> '[.items[] | select(any(.spec.nodeAffinity.required.nodeSelectorTerms[]?.matchExpressions[]? | select(.key == "kubernetes.io/hostname") | .values[]; . == $n))] | (.[] | "\(.spec.storageClassName) \(.spec.claimRef.namespace)/\(.spec.claimRef.name)"), "\(length) volumes"' <<<"$pv"

        Its last line counts the volumes listed, `0 volumes` on a node
        that holds none. A read that does not end on that line is no
        reading, and the sitting does not go on: `jq` reads only what
        `kub` returned, so a failed read prints `kub`'s error and no
        count, rather than the empty list of a node that holds nothing.

        A `local-path` volume dies with the instance's system disk
        (cluster/storage.md §2, "What local-path data outlives"), so each
        such claim is replicated, backed up, or moved by its owner's
        procedure before the sitting goes on. A node volume's claim is
        listed too and loses nothing: the block volume outlives the
        instance.
    4.  *On the dedicated VIP's node*, the outage is accepted for the
        sitting: the reserved address answers nothing and hath is
        offline until the chain's `up-apps`, where hath's Service asks
        for the address.

    **The node is emptied and stepped down.**

        kub drain <node> --ignore-daemonsets --delete-emptydir-data
        tal -n "$(ip_of <node>)" etcd forfeit-leadership
        (umask 077; tal -n "$(ip_of <another node>)" etcd snapshot ~/.talos/kluster-etcd-before-<node>.snapshot)

    `tal -n "$CPS" etcd status` then shows a leader that is not
    `<node>`. The drain is undone by `kub uncordon <node>`, and a
    forfeited leadership needs no undoing. The snapshot holds all of
    etcd's data, the cluster's Secrets among it, so it is written beside
    the talosconfig and readable by the operator alone. It is what a
    recovery from the loss of quorum would start from, and that
    recovery is not written (cluster/nodes.md §5). The node does not
    leave etcd: its member is removed by the new instance's join, as a
    lost node's is, so the one step that cannot be taken back is the
    next.

    **The instance is terminated**, at a count of one:

        I=($(ocid oci:Core/instance:Instance kluster-<node>)); printf '%s\n' "${I[@]}"
        [ ${#I[@]} -eq 1 ] && credentials derived oci-physical terminate "${I[@]}"

    `terminate` deletes the instance with its boot volume and prints
    both once they read `TERMINATED`, as in §3.5's step 3. On the
    dedicated VIP's node,
    `credentials derived oci-physical read "$(ocid oci:Core/publicIp:PublicIp kluster-vip1)" "$(ocid oci:Core/volume:Volume kluster-hath-cache)"`
    then prints `lifetime=RESERVED assigned=-` and
    `AVAILABLE attached-to=-`, and anything else stops the sitting
    before the refresh. `tal -n "$CPS" etcd status` answers from the
    other two nodes with one leader, and errs on the terminated one.

    **The recovery is the lost node's block, from its `read` line on**:
    the `read` printing `TERMINATED`, the delete of the Node object, the
    count-gated targeted refresh, the preview, and the `up`. A create
    the AD refuses fails that instance alone and leaves the cluster on two
    members; the same `up` again retries it, and nothing else is tried
    in between.

    **Closing readings**, with the setup taken again from the new
    outputs: reading 1 passes again, three members and no learner, and
    `<node>` is `Ready` and not cordoned. The etcd fsync item of
    declarative/physical.md §6 passes on the new boot volume. The
    deploy chain of the lost node's path has run to the end of
    `up-apps`, and the `internet` pool holds the new node's two
    addresses, which this prints, one line each:

        G=$(physical stack output node_guas --json | mise x -- jq -r --arg n <node> '.[$n]')
        kub get ciliumloadbalancerippool internet -o json | mise x -- jq -r '.spec.blocks[].cidr' | grep -xF -e "$(ip_of <node>)/32" -e "$G/128"

    On the dedicated VIP's node, two more: the `read` above shows an
    assigned reserved address and an attached volume, and the items of
    declarative/physical.md §6 for the node volume that need no pod
    pass.

    **Not established, and read by the first run that meets it:**

    -   whether the health gate, which the refresh's run of the program
        reaches, waits on the missing node and fails the refresh;
    -   a re-created control plane joining a live cluster under its old
        name: the join removing the old member, the new one listed as a
        learner and then promoted, with two members live throughout;
    -   what the new kubelet, Cilium and Talos discovery keep of the old
        machine once its Node object is deleted;
    -   whether OCI's quota accounting releases a terminated instance
        before the create that follows it. A create refused on the
        quota rather than on capacity is that case, and the same `up`,
        run again later, retries it.
-   **§3.2 Homelab worker VM.** Trigger: VM/disk loss or rebuild.
    Gist: from the root of the checkout that holds `.credentials/`,
    with `physical` the function of declarative/physical.md §6, which
    runs `pulumi` under `physical`'s own passphrase (credentials.md
    §4.4): `physical state unprotect '<disk URN>'`, then `physical up
    --replace '<image URN>' --replace '<disk URN>'`, the image being
    the worker's `FactoryImage`. `physical stack --show-urns` lists
    the disk as `…:Volume::kluster-worker-disk` and the image as
    `…:FactoryImage::kluster-worker-nocloud`. The new disk is created
    from the Talos image cached on the machine running the apply
    (`components/talos/image.py`, `IMAGE_CACHE`), at the capacity
    `HOMELAB_DISK_GIB` declares rather than the size the old disk had,
    and boots with EPHEMERAL across it, no reboot needed; a disk grown
    on the host ahead of the constant comes back at the constant's size,
    which the run's warning names (physical/homelab-host.md §1). The libvirt
    provider first opens that file at the disk's create, after the
    delete-first replacement has removed the old disk. So a run that
    names the disk alone where the cache has lost the file, or never
    held it, is refused at that delete: the disk's `before_delete`
    hook (`components/homelab/rebuild.py`) finds no file at the path
    the run declares, and the run fails with `before hook
    "kluster-worker-disk-source" failed: no file is at <path>, …`,
    naming the two-flag command. The seed and the domain go ahead of
    the disk, so the worker is down by then and the disk is kept. A
    plain `physical up` creates the two again on the kept disk and
    protects it again. The two-flag command rebuilds it: straight after
    the refusal the disk is still unprotected, and after a plain `up`
    the unprotect comes first again. The hook guards a disk once an
    `up` has run with it, since a delete runs the hooks state
    recorded, and it covers a missing file only: a create that fails
    for another reason fails after the delete, the disk pending
    replacement. Named too, the
    image is replaced before the disk is
    touched: its create fetches the artifact where the file is missing
    and reuses the file where it is present, and a fetch that fails
    fails the run with the old disk in place. Its original, deleted
    last, leaves the file and warns that it is not the file it
    recorded. The run then
    replaces the disk, the domain and the seed, the seed from the
    current machine config (declarative/physical.md §3) — the way to a
    fresh worker, where a STATE wipe on the kept disk boots the seed as
    it stood at the last rebuild; backed local-path
    data returns by a restore from its VolSync repository (the DR path,
    storage.md §3.1 — no drill of it has run, §4), and a claim under
    `off` comes back empty and is derived again; NAS claims
    re-point untouched; BGP session re-establishes from the static IP
    in the config.

    **The peak recorder runs around the `up`** (§8.5): `mise x uv -- uv
    run perf record .claude/peaks`, from the root of the checkout in a
    terminal of its own, from before the `up` until both metrics
    stores, which the worker holds, have restored themselves and run.
    The control planes keep their addresses, so it runs once. It is then
    stopped, `mise x uv -- uv run perf import .claude/peaks` writes the
    file into the history store once vmagent's queue has drained, and
    the file is deleted. The cluster's own scrape of the control planes
    resumes under the labels it had, which is the case of §8.5 where a
    count read across the gap reads its events twice.
-   **§3.3 Total-cloud-loss / total-home-loss** — owned by nodes.md
    §5 (cold-standby drill, both directions). The loss of every cloud
    node with the account intact is §3.5.
-   **§3.4 Cloud CP node resize.** Trigger: a merged change to
    `conventions.NODE_OCPUS` or `NODE_MEMORY_GB`. Gist: the merge's own
    `up-physical` is rejected at its approval; then the guardrails, then
    one control plane at a time, each a targeted `physical up` from the
    checkout, with etcd healthy again before the next; then a deploy
    that finds nothing to do. Facts that shape it:

    -   **A resize is an update in place that reboots the node.** The
        pinned OCI provider marks neither `shapeConfig` nor any field in
        it as replacing, and its Terraform upstream gives `shape_config`
        no `ForceNew` and no `CustomizeDiff` rule
        (`core_instance_resource.go`, at the commit pulumi-oci 5.6.0
        bridges). The change goes out as one `UpdateInstance` call
        carrying the new `shapeConfig`, and the provider waits on its
        work request (`updateOptionsViaWorkRequest`). The call sets no
        `updateOperationConstraint`, so OCI's default, `ALLOW_DOWNTIME`,
        applies, and a running instance "is rebooted as part of the
        change shape operation"
        ([Changing the Shape of an Instance](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/resizinginstances.htm)).
        The instance keeps its addresses, its network interfaces and its
        volume attachment. Talos boots again from STATE, and the machine
        configuration selects its link by `physical: true` rather than
        by name, so an interface the new shape renames changes nothing.
    -   **Every control plane is an etcd member**, so one apply that
        resized all three would reboot them together and lose quorum.
        The merge's `up-physical` plans exactly that, which is why it is
        rejected rather than approved (framework/ci.md §3). The
        rejection stops that run's chain and sends its failure notice,
        and it frees the `deploy` concurrency group, which holds a later
        run queued behind a pending one.
    -   **The quotas go first.** A resize grows the counts the
        compartment quotas bound, and a bound written for the old size
        can admit the first node at its new size and refuse the next.
        Oracle documents that a shape change needs service limits for
        the new shape and that the instance otherwise keeps its original
        one; the tenancy's A1 core and memory limits, per AD and
        regional, on the console's *Limits, Quotas and Usage* page, are
        read against the new quotas before anything is applied. The
        quota and the budget update in place.
    -   **A1 capacity is what a reboot risks.** A running instance holds
        its capacity; the resize asks its availability domain for the
        larger shape and reboots, and Oracle documents no outcome for a
        shape change that meets an AD with no A1 capacity. Its documented
        check beforehand is a compute capacity report
        (`CreateComputeCapacityReport`) for the shape in the node's AD, a
        reading at one moment rather than a reservation, taken by the
        capacity reading below. The standing quorum is the hold: one
        member down leaves two, and no second node is touched until the
        first is back.
    -   **hath goes down with its node.** It has one instance, on the
        node holding the dedicated VIP, and that node's reboot takes it
        offline until the node is back.

    **The capacity reading** comes first, and §3.5 takes it too. It is
    taken in the OCI console's Cloud Shell, with the console set to the
    cluster's region, and nothing in this repository makes the call. A
    report's compartment is always the tenancy's root, which `physical`'s
    key cannot reach, being confined to its own compartment, while Cloud
    Shell runs Oracle's `oci` CLI under the operator's own identity.
    `credentials derived oci-physical`, which makes the other OCI calls
    of these runbooks, signs with that key, so this reading stays with
    the operator. The
    form is the CLI's documented one,
    [`oci compute compute-capacity-report create`](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/compute/compute-capacity-report/create.html),
    with `--shape-availabilities` given inline as JSON, whose keys are
    those of the API's `CreateCapacityReportShapeAvailabilityDetails`.
    With `<tenancy OCID>` from the console's *Tenancy details* page, and
    `<ocpus>` and `<GB>` the size the instances are to have —
    `conventions.NODE_OCPUS` and `NODE_MEMORY_GB` as the change declares
    them — the reading of every availability domain of the region is
    one loop:

        T='<tenancy OCID>'
        for AD in $(oci iam availability-domain list --compartment-id "$T" --query "join(' ', data[].name)" --raw-output); do
          echo "$AD $(oci compute compute-capacity-report create --compartment-id "$T" --availability-domain "$AD" \
            --shape-availabilities '[{"instanceShape":"VM.Standard.A1.Flex","instanceShapeConfig":{"ocpus":<ocpus>,"memoryInGBs":<GB>}}]' \
            --query 'data."shape-availabilities"[0].join(`" "`, ["availability-status", to_string("available-count")])' --raw-output)"
        done

    It prints a line per AD: its name, the `availability-status` of the
    report's one entry under `shape-availabilities`, which is
    `AVAILABLE`, `OUT_OF_HOST_CAPACITY` or `HARDWARE_NOT_SUPPORTED`, and
    its `available-count`, the number of instances of that configuration
    the AD can create
    ([`CapacityReportShapeAvailability`](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/models/oci.core.models.CapacityReportShapeAvailability.html)).
    For `VM.Standard.A1.Flex` the count comes back `null` beside
    `AVAILABLE`. **An AD passes at `AVAILABLE`, with a count of at least
    1 where a count is given.** A resize reads each node's AD before
    that node's `up`, and a node whose AD does not pass waits.

    From the root of the checkout that holds `.credentials/`, with
    `physical` the function of declarative/physical.md §6, `urns` the
    helper of §3.1, and `tal` and `CPS` the setup of
    physical/gateway-cutover.md §8.1 (`tal -n "$CPS"` asks every control
    plane):

        Q=($(urns '\$oci:(Limits/quota:Quota::kluster-quota|Budget/budget:Budget::kluster-budget)$')); printf '%s\n' "${Q[@]}"
        [ ${#Q[@]} -eq 2 ] && physical preview --diff "${Q[@]/#/--target=}"    # two updates: the quota's statements, the budget's amount
        physical up "${Q[@]/#/--target=}"

    Then, for each node in turn, with `<node>` its name (`cp1`), never
    two at once:

        U=($(urns '\$oci:Core/instance:Instance::kluster-<node>$')); printf '%s\n' "${U[@]}"
        [ ${#U[@]} -eq 1 ] && physical preview --diff "${U[@]/#/--target=}"    # one update: shapeConfig
        physical up "${U[@]/#/--target=}"
        tal -n "$CPS" etcd status                # every member listed, none reporting an error

    Each list is printed, and its first command runs only at the count
    the step knows, as in §3.1. Any plan
    other than the one in the comment is a diff nobody expected, and
    nothing is applied until it has been read. The next node waits
    until `etcd status` answers from every control plane, lists every
    member and reports no error; a member still joining shows as an
    error or a missing row, and the reading is taken again.

    **A node that does not come back** — the `up` fails, the instance
    is not `RUNNING`, or `etcd status` keeps missing its member — stops
    the rollout where it is, with no other node touched, since quorum
    holds at two. The instance's state and shape, which
    `credentials derived oci-physical read "$(ocid oci:Core/instance:Instance kluster-<node>)"`
    prints, with `ocid` the helper of §3.1, say which case it is.
    `RUNNING` at its old shape is a failed update: nothing changed,
    `physical refresh --run-program "${U[@]/#/--target=}"` brings state
    back to the cloud, and the node is retried later.
    Stopped, it is started from the console. One that does not boot at
    its new shape goes back to its original one, Oracle's instruction
    for that case, through the console's *Edit shape*, followed by the
    same refresh, and the resize waits for capacity. One that cannot be
    started at all is replaced by §3.1, which creates the replacement
    at the declared size.

    Once all three are done, the deploy chain is dispatched
    (`deploy.yml` takes `workflow_dispatch`): `plan-physical` finds no
    diff, `up-physical` is skipped, and the chain runs on to the
    layers the rejected run never reached.

-   **§3.5 The whole cluster, the account intact.** Trigger: the loss
    of every cloud control plane while the tenancy, the region and the
    balancer survive; and the bring-up's rebuild drill, which runs it
    once on the empty cluster (physical/bring-up.md §2, stage 5a).
    Gist: the three instances are gone, by the loss or by a termination
    outside the program; a targeted refresh records that in `physical`'s
    state; one `physical up` creates the fleet, the worker and the
    day-1 chain again; then the states of `k8s-base` and `apps` are
    refreshed and the deploy chain applies both stacks from empty. The
    steps below are written for the deliberate run. After an actual
    loss, step 3 has already happened, step 0 records what still reads,
    and steps 1 and 2 run next.

    **What is kept.** The balancer with its listeners and backend sets;
    the network, from the VCN to the subnet; the dedicated VIP's
    reserved public IP; the Talos secrets; the node volume; the cloud
    image with its archive and bucket; the guardrails; and the libvirt
    pool. Outside the cluster, the gateway, the overlay, the `dns`
    zones, the backup bucket and the state backend take no write.

    So every identity that a consumer outside the cluster holds
    survives:

    -   the reserved VIP, which the A record of `vip1.hosts` names and
        hath registers with a third party;
    -   the balancer's two addresses, which the records of
        `kluster.hosts` name, and the IPv4 among them the cluster
        endpoint: the certificate SANs of every control plane and every
        machine configuration, and the server of the kubeconfig;
    -   the worker's IPv4, a convention, which the gateway's BGP
        neighbor names; its MAC and the link-local derived from it, the
        gateway's IPv6 next hop; and its GUA, which the gateway's
        pinhole names, read off the delegated /64 and the MAC;
    -   the Talos PKI, so the operator's talosconfig and the kubeconfig
        copies in `Pulumi.k8s-base.yaml` and `Pulumi.apps.yaml` keep
        opening the cluster.

    **What is lost, and created again.** The three instances with their
    boot volumes; the balancer's backends; the VIP's secondary private
    IP, whose address OCI picks again, so the `vip1_private` output
    moves and `k8s-base` reads the new one at its first `up`; the
    volume's attachment; the worker's image, disk, seed and domain, by
    §3.2's form; and the day-1 chain, one `ConfigurationApply` per node,
    the `Bootstrap` and the `Kubeconfig`, so that a fresh etcd is
    bootstrapped under the kept PKI. With etcd go every object of
    `k8s-base` and `apps`, the KubeSpan keys and node identities, which
    no object outside the cluster names, and the sealing key, which the
    new controller draws.

    **Where it applies as written: before bring-up stage 6.** No sealed
    value exists there. The tolerance of physical/bring-up.md §1
    declares nothing for an absent value, so the new sealing key leaves
    nothing to seal again; the BGP session holds no password on the
    worker's side and is not up, so no `lan` route is installed on the
    gateway to withdraw. A rebuild after stage 6 also takes stage 6's
    sealing sitting again, with `bgp seal` and never `bgp record`, which
    would draw a new password and rewrite the gateway's BGP
    configuration; after stage 9 that sitting seals every writer key
    with the bare `credentials derived b2-writer seal`, the
    applications' keys with the rest. Either way it seals the etcd
    snapshots' read-only key, `reader-etcd`, with
    `credentials derived b2-reader seal`, which no form of
    `b2-writer seal` covers. Until that sitting, both metrics stores wait in `Init` for
    the monitoring namespace's writer key. Once it has run, each store
    restores itself from the backup bucket before it starts
    (storage.md §5), and the readings below hold the history to its
    baseline. Once an application holds data, its data comes back
    by the restores of storage.md §5. This procedure carries neither.

    **A1 capacity is the risk it takes.** It gives up, by construction,
    the hold of cluster/nodes.md §3.2, never needing more than one
    create at once: three creates follow three terminations, each in
    the AD its node's position fixes, and a full AD refuses its create
    for as long as the hunt takes, which may be days (cluster/nodes.md
    §3.1). The quotas admit the whole fleet at once, since they hold
    exactly the fleet (cluster/nodes.md §3.2), once OCI has released
    the terminated instances, which §3.1 lists as not established.
    Before stage 6 a hunt costs calendar
    time and no money, the half-built fleet billing what the fleet
    bills. So a deliberate run goes only on a day when §3.4's capacity
    reading, taken in the hour before the termination for every AD of
    the region at the declared size, passes in each; and the
    termination and the rebuild run back to back. A create that is
    refused fails that instance alone. The launched nodes wait out the
    windows of declarative/physical.md §6, and the same `up` again
    creates the refused one.

    **Setup.** Every command runs from the root of the checkout that
    holds `.credentials/`, with `physical` the function of
    declarative/physical.md §6, `urns` the helper of §3.1, and `tal`,
    `CP`, `CPS`, `WORKER` and `ip_of` the setup of
    physical/gateway-cutover.md §8.1, whose node volume item also gives
    `ids`, with `V=hath-cache` and `N` the address of `cp1`. Scratch
    goes under the checkout's `.claude/drill/`, which is deleted when
    the run closes, and a transcript of each step goes on the issue
    that records the run. Steps 1 to 5 run in one sitting.

        D=.claude/drill; mkdir -p "$D"
        SCOPE='::kluster:cloud:CloudNodes(\$|::)|::kluster:cloud:NodeVolume(::|\$oci:Core/volumeAttachment:)|::kluster:talos:TalosDay1(\$|::)|::kluster:homelab:HomelabHost(::|\$libvirt:index/(domain|volume):)|FactoryImage::kluster-worker-nocloud$|pulumi:pulumi:Stack::'
        protected() { physical stack export | mise x -- jq -r '.deployment.resources[] | select(.protect) | .urn' | sort; }
        outside() { local e d; e=$(physical stack export --show-secrets) && d=$(mise x -- jq -c --arg s "$SCOPE" '[.deployment.resources[] | select((.urn | test($s)) or (.urn | endswith("::pulumi:providers:pulumi-python::default")) | not) | {urn, inputs, outputs, protect}] | sort_by(.urn)' <<<"$e") && sha256sum <<<"$d"; }
        anchors() { for n in kluster.hosts vip1.hosts; do for t in A AAAA; do if a=$(mise x -- doggo --short "$n.unlimited-code.works" "$t" @1.1.1.1); then echo "$n $t $(printf '%s' "$a" | sort | tr '\n' ' ')"; else echo "$n $t no reading"; fi; done; done; }
        sealing() { mise x -- kubeseal --kubeconfig ~/.kube/kluster --fetch-cert \
            $(mise x uv -- uv run python -c 'from kluster import conventions as c; print("--controller-namespace", c.SEALING_NAMESPACE, "--controller-name", c.SEALING_CONTROLLER)'); }
        history() { mise x -- kubectl --kubeconfig ~/.kube/kluster get --raw "/api/v1/namespaces/monitoring/services/vmsingle-history:http/proxy/$1"; }
        metrics() { local m; m=$(history metrics) && awk '/^vm_rows\{type="storage\// { n += $2 } END { printf "rows %.0f\n", n }' <<<"$m"
            history 'api/v1/query?query=min(tfirst_over_time(node_load1%5B100y%5D))' | mise x -- jq -r '"earliest \(.data.result[0].value[1])"'; }

    `SCOPE` matches every resource the run changes, and nothing else:
    the cloud nodes with what hangs off them, the attachment, the
    day-1 chain, the worker's domain and volumes with its image, and the
    stack's outputs. It matches the components `CloudNodes`,
    `NodeVolume`, `TalosDay1` and `HomelabHost` themselves as well,
    because a component's registered outputs change with what it holds.
    Each component sits at the top of the stack, so the pattern anchors
    on `::` before its type (§3.1).

    `metrics` reads the history store through the API server's service
    proxy: the rows it holds, the count its backup job compares, and the
    time of its earliest sample, of `node_load1`, a name the history
    holds from its first day: this cluster's nodes have reported it since
    the store began, and the range Wave C's head imports carries the
    legacy VPS node's (cluster/migration.md §2.1).

    `outside` prints a digest of every other resource's entry, secrets
    included, so that only the digest is printed. It digests only what
    an export and a `jq` that both succeeded returned, so a failed read
    prints no digest rather than the digest of nothing. It leaves out the
    stack's default `pulumi-python` provider too, the provider of every
    dynamic resource: its entry records the stack's whole configuration,
    and any `physical up` rewrites it to the configuration of the commit
    it runs, so a configuration merged since the last `up` changes the
    entry with nothing outside the scope changed, and no preview shows
    the difference. Taking the baseline after an `up` instead would make
    step 0 a write; the configuration is the commit's, and a review of
    the commit is where it is read.

    **An empty `protected` or `outside` is no reading.** Each prints
    nothing where its read of the export fails, and neither prints
    nothing for the stack as it stands, which protects resources of its
    own. An empty one, in a baseline or in a reading taken against one,
    is taken again. `anchors` prints `no reading` in place of the
    addresses of a query `doggo` got no answer to, and on any other answer
    what `doggo` printed, which for a name with no record of the type is
    nothing; so its reading is judged by which of its lines name an
    address, as step 0 does below.

    **Step 0: the baseline, and the go.**

        gh workflow disable deploy.yml --repo Aetf/kluster
        physical preview --refresh --run-program --expect-no-changes
        for s in dns k8s-base apps; do mise x -- pulumi preview --refresh --expect-no-changes --stack "$s"; done
        physical stack export > "$D/export.json"
        protected > "$D/protected"; outside > "$D/outside"; anchors > "$D/anchors"; ids > "$D/ids"; sealing > "$D/sealing.pem"
        metrics > "$D/metrics"
        k() { mise x -- kubectl --kubeconfig ~/.kube/kluster -n monitoring "$@"; }
        k patch cronjob vmbackup-recent vmbackup-history -p '{"spec":{"suspend":true}}'
        until active=$(k get jobs -o jsonpath='{.items[?(@.status.active)].metadata.name}') && [ -z "$active" ]; do sleep 10; done
        k create job drill-history --from=cronjob/vmbackup-history
        until k get job drill-history -o jsonpath='{.status.succeeded}{.status.failed}' | grep -q .; do sleep 10; done
        k get job drill-history -o jsonpath='{.status.succeeded}'
        for o in cluster_endpoint cluster_endpoint_v6 vip1 worker_gua; do echo "$o $(physical stack output "$o")"; done > "$D/outputs"
        wc -l < "$D/protected"; cat "$D/outside" "$D/anchors" "$D/ids" "$D/metrics" "$D/outputs"; grep -c 'BEGIN CERTIFICATE' "$D/sealing.pem"

    After bring-up stage 6, the history is backed up once more after its
    baseline is read, so the backup a rebuild restores holds at least the
    baseline's rows. Both backup schedules are suspended first, and the
    drill's own job is created only once no backup job is running: a
    job created from a CronJob runs beside the CronJob's own, which
    `Forbid` does not stop, and two runs copying one store from two
    snapshots each delete what the other uploads, so the last to finish
    marks complete a set neither snapshot held. Suspended, neither runs
    again before step 3, so the loss cuts no run off. The wait ends when
    the job succeeds or fails, and the last line prints `1` for a
    success. Both schedules go with etcd at step 3, and the rebuilt
    cluster declares them anew, running; a run that does not go resumes
    them, beside enabling `deploy.yml` again, with the same `patch`
    and `"suspend":false`. Before stage 6 there is no backup job, and the
    `kubectl` lines are left out.

    **The last line shows every baseline, and the go needs each one
    whole.** The `protected` count is not zero, and `outside` is a
    digest. Three of the four `anchors` lines name an address, every one
    but `vip1.hosts AAAA`, a name that is IPv4 only, and none says `no
    reading`. `ids` prints `1 found` and the partition's line, whose
    `uuid` may print `null` on the boot that provisioned the volume
    (physical/gateway-cutover.md §8.1). After
    stage 6, `metrics` prints a `rows` line and an `earliest` line. Each
    of the four outputs has a value, and the certificate count is `1`.
    A baseline that is not whole is taken again before the go, because
    every later comparison against it reads as a pass where the reading
    it is compared with failed the same way.

    Disabling `deploy.yml` is bring-up stage 4's way back used as a
    freeze: a merge, a Renovate automerge among them, would otherwise
    run the chain against a half-built cluster. Every preview exits 0,
    and a diff stops the run: it starts converged, so that every later
    diff is its own. `gateway-health` is clean (physical/gateway.md
    §1.6). A deliberate run then takes §3.4's capacity reading, and goes
    only if every AD passes; otherwise it waits for another day, with
    nothing changed but the workflow and the two backup schedules, which
    are enabled and resumed again.

    **On the go, the peak recorder starts** (§8.5), in a terminal of
    its own at the root of the checkout, which outlives the sitting, and
    runs until step 9 imports what it recorded:

        mise x uv -- uv run perf record .claude/drill/peaks

    It reads the control planes' addresses once, when it starts, and
    the rebuilt nodes have new ones, so step 6 starts it again.

    **Step 1: the day-1 chain leaves state.**

        T=($(urns '::kluster:talos:TalosDay1\$talos:')); printf '%s\n' "${T[@]}"
        [ ${#T[@]} -eq 6 ] && physical destroy --run-program "${T[@]/#/--target=}"

    The list and the destroy's preview hold six resources, one
    `ConfigurationApply` per node, the `Bootstrap` and the
    `Kubeconfig`, and nothing else. Their delete resets nothing, because
    `on_destroy` keeps `reset` off (`components/talos`): every node
    still answers `tal -n <node> version`.

    The destroy takes `--run-program`, although the worker's disk is
    not among its targets. The disk's `before_delete` hook (§3.2) is
    recorded in state with it, and the engine runs no delete hook
    without the program (declarative/physical.md §3), so a `destroy`
    without `--run-program` refuses while any resource in the stack's
    state records a delete hook, whichever resources it targets.

    **Step 2:** `physical state unprotect '<worker disk URN>'`, the
    disk of §3.2.

    **Step 3: the loss.** For a deliberate run, terminate `kluster-cp1`,
    `kluster-cp2` and `kluster-cp3`, each with its boot volume deleted,
    with `ocid` the helper of §3.1:

        I=($(for n in cp1 cp2 cp3; do ocid oci:Core/instance:Instance "kluster-$n"; done)); printf '%s\n' "${I[@]}"
        [ ${#I[@]} -eq 3 ] && credentials derived oci-physical terminate "${I[@]}"
        credentials derived oci-physical read "$(ocid oci:Core/publicIp:PublicIp kluster-vip1)" "$(ocid oci:Core/volume:Volume kluster-hath-cache)"

    The list prints three identifiers, and `terminate` runs only at that
    count, as in §3.1. It takes them in turn: it deletes each instance
    with its boot volume, waits for the work request OCI answers with, a
    few minutes each, and prints the instance and its boot volume once
    both read `TERMINATED`. A work request that ends anywhere but
    `SUCCEEDED` stops it, naming its errors, before the next instance is
    touched; run again, it leaves an instance already `TERMINATED` as it
    is. The loss is taken outside the program rather than by a
    `destroy`, because a destroy of `cp1` reaches the reserved IP
    (§3.1). Readings: the three instances and their boot volumes print
    `TERMINATED`; `kluster-vip1` prints `lifetime=RESERVED assigned=-`;
    `kluster-hath-cache` prints `AVAILABLE attached-to=-`;
    `gateway-health` is still clean. From here the way is forward only.

    **Step 4: the state records the loss.**

        S=$(urns '\$oci:NetworkLoadBalancer/backendSet:BackendSet::' | wc -l); echo "$S"
        T=($(urns '::kluster:cloud:CloudNodes\$oci:(Core/instance:Instance|Core/privateIp:PrivateIp|Core/publicIp:PublicIp|NetworkLoadBalancer/backend:Backend)::|::kluster:cloud:NodeVolume\$oci:Core/volumeAttachment:VolumeAttachment::'))
        printf '%s\n' "${T[@]}"
        [ "$S" -gt 0 ] && [ ${#T[@]} -eq $((6 + 3 * S)) ] && physical refresh --run-program "${T[@]/#/--target=}"

    The list holds the three instances, the secondary private IP, the
    reserved IP, the attachment, and each node's backend in each of the
    `S` backend sets (§3.1). Readings: the instances, the secondary
    private IP and the attachment leave state; the reserved IP stays,
    its `private_ip_id` reading empty; the backends stay, because OCI
    keeps the backends of a terminated instance, and step 5 replaces
    them; an attachment that stays takes the fallback of
    declarative/physical.md §6. The refresh runs the program to its end
    with no control plane in existence.

    **Step 5: the rebuild.**

        W=($(urns '::kluster-worker-nocloud$|::kluster-worker-disk$')); printf '%s\n' "${W[@]}"
        [ ${#W[@]} -eq 2 ] && physical preview --diff "${W[@]/#/--replace=}"
        p=$(physical preview --json "${W[@]/#/--replace=}") && mise x -- jq -r --arg s "$SCOPE" '[.steps[] | select(.op != "same") | .urn | select(test($s) | not)] | length, .[]' <<<"$p"
        physical up "${W[@]/#/--replace=}"

    `W` holds the worker's `FactoryImage` and its disk. The scope check,
    the second preview, prints `0`, the count of planned steps outside
    `SCOPE`, and then the URN of each; it reads only what a preview that
    succeeded returned, so a failed preview prints no count and is no
    reading. The plan holds
    eleven creates: the three instances, the secondary private IP, the
    attachment and the six resources of the day-1 chain; one update,
    the reserved IP's `private_ip_id`; replacements of every backend
    and of the worker's image, disk, seed and domain; and the stack's
    outputs. Anything else, the node volume above all, is a diff nobody
    expected, and nothing is applied until it has been read. The `up`
    ends on the Talos health gate, every node named, with no reboot of
    the worker: the new disk is created at the capacity
    `HOMELAB_DISK_GIB` declares (physical/homelab-host.md §1), and
    `tal -n "$WORKER" get volumestatus EPHEMERAL` reads that capacity
    less the image's partitions and STATE.

    **Step 6: `physical` converges.** The peak recorder is stopped with
    Ctrl-C and started again with the same command, which appends to
    `.claude/drill/peaks` and reads the new nodes' addresses: until then
    it read their etcd, through the API server, and none of their files.
    A further `physical up` reports
    no changes, and `physical preview --refresh --run-program
    --expect-no-changes` exits 0 over the whole stack, the gateway and
    the overlay included. `protected` prints what `$D/protected` holds,
    and `outside` what `$D/outside` holds.

    **Step 7: the readings that need no network plugin,**
    physical/gateway-cutover.md §8.1 on the rebuilt cluster, its setup
    taken again from the new outputs, with `~/.kube/kluster` written
    again and the talosconfig written again from the `talosconfig`
    output: `version` answers from every node at the pin; the worker's
    link-local is the convention's, and the worker holds `worker_gua`;
    the etcd fsync item passes, both halves, every boot volume being new
    (declarative/physical.md §6); the fronts item of the same section
    passes, an anonymous `GET /version` on 6443 reading `401` at each of
    the balancer's addresses, from a host that is no cluster member and
    reaches the internet in each family, taken directly and never
    through an HTTP proxy, the IPv4 address under the cluster's CA and
    the IPv6 address with verification off; the node volume's first
    item passes, its `ids` compared with `$D/ids` by the block below;
    `u-storage` is ready on every node; and the node label sits on
    `cp1`. The volume's other items ran at bring-up and are not
    repeated.

        ids > "$D/ids.rebuilt"; cat "$D/ids.rebuilt"
        awk 'function f(k, l) { if (!match(l, " " k "=[^ ]*")) return ""; l = substr(l, RSTART + length(k) + 2, RLENGTH - length(k) - 2); return l == "null" ? "" : l }
            FNR == 1 { c[++i] = $0 }; FNR == 2 { u[i] = f("uuid", $0); p[i] = f("partition_uuid", $0) }
            END { ok = i == 2 && c[1] == "1 found" && c[2] == "1 found" && p[1] != "" && p[2] == p[1] && u[2] != "" && (u[1] == "" || u[2] == u[1]); print "ids " (ok ? "pass" : "fail") }' "$D/ids" "$D/ids.rebuilt"

    It prints `ids pass` where both readings print `1 found` and the
    same partition UUID, and the rebuilt node holds a filesystem UUID,
    the same one as the baseline wherever the baseline holds one;
    anything else prints `ids fail`. The partition UUID is the
    partition's GPT GUID, which a partition provisioned anew would draw
    afresh, so with the count it tells the volume's own partition from
    another. A baseline with no filesystem UUID was read on the boot
    that provisioned the volume, before discovery probed the partition
    again, while the rebuilt node finds a partition it did not provision
    and reads one. A rebuilt node that reads none has a partition its
    discovery found empty, and Talos formats a partition it finds empty
    in place, keeping the partition and its GUID; the count and the
    partition alone would pass a volume formatted afresh. Where the
    baseline holds no filesystem UUID, the comparison holds the rebuilt
    node to the partition, the count and a filesystem UUID of its own; a
    format that discovery has since probed again reads as a UUID like
    any other, and such a baseline cannot tell it from the original.
    physical/gateway-cutover.md §8.1 meets the same empty reading by
    taking item 4's reading as item 5's baseline; the rebuild has no
    later reading of the old node to take. A read that failed prints no
    count, and compares as a failure.

    **Step 8: `k8s-base` and `apps` from empty.**

        mise x -- pulumi refresh --stack k8s-base
        mise x -- pulumi refresh --stack apps
        credentials derived sync --only kubeconfig

    Each refresh runs under the committed kubeconfig copy, which
    authenticates, the PKI and the endpoint being kept, and drops every
    object the rebuilt cluster does not hold, a custom resource whose
    definition went with etcd among them, with no error. **Without those two
    refreshes a plain `up` of either stack creates nothing**: a plain
    `up` diffs the program against the state, which still holds every
    object the old cluster held, and the chain's `up` takes no
    `--refresh` (framework/ci.md §3.2). The `Kubeconfig` was issued
    again, so the sync of §2.5 follows. Then: a pull request committing
    the two stack files alone;
    `gh workflow enable deploy.yml --repo Aetf/kluster`; and the merge.
    The chain runs: `plan-physical` finds no diff, and `up-k8s-base` and
    `up-apps` are both stacks' first `up` on an empty cluster.

    **Step 9:** bring-up stage 5's gate on the rebuilt cluster, the
    sealing certificate differing from `$D/sealing.pem`. Where the run
    is after bring-up stage 6, the sealing sitting of the paragraph on
    stage 6 above follows, and its pull request commits
    `Pulumi.k8s-base.yaml`, and after stage 9 `Pulumi.apps.yaml` with
    it. Once its merge has
    applied `k8s-base`, both stores leave `Init`, and `metrics` prints
    the baseline's earliest sample and at least the baseline's rows.
    Then the peak recorder is stopped with Ctrl-C, and the import writes
    its file into the history store, printing the lines written for each
    control plane (§8.5). It refuses while vmagent still holds what it
    queued for the stores while they waited in `Init`, and is run again
    once that has drained:

        mise x uv -- uv run perf import "$D/peaks"

    Before bring-up stage 6, the history store started empty with
    `k8s-base`'s apply, and the import writes the run there all the
    same. Then the pass readings.

    **It passes when each of these holds:**

    1.  Step 5's `up` exits 0 behind the health gate with no reboot of
        the worker, the worker's EPHEMERAL reads as step 5 says, and
        step 6's readings hold.
    2.  Step 7's readings hold, its comparison of the volume's `ids`
        with the baseline printing `ids pass`.
    3.  Bring-up stage 5's gate holds on the rebuilt cluster, each
        of `up-k8s-base` and `up-apps` converged in its one run, and no
        node reports `DiskPressure` through either of them.
    4.  **Nothing outside changed:**
        -   `gateway-health` is clean at step 0, after step 3 and after
            step 9;
        -   `physical`'s refreshed preview is clean, the gateway, the
            overlay's members, routes, rules and DNS, and the backup
            bucket included, and `outside` equals the baseline;
        -   `mise x -- pulumi preview --refresh --expect-no-changes
            --stack dns` exits 0, the four outputs equal `$D/outputs`,
            and `anchors` prints what `$D/anchors` holds;
        -   every plan read during the run passed the scope check.
    5.  **The metrics history came back**, where the run is after bring-up
        stage 6: `metrics` prints the earliest sample `$D/metrics` holds,
        and no fewer rows, and the history's backup job succeeds on its
        next run (§6.2). `metrics` counts the rows only over a read that
        succeeded, so a failed read prints no `rows` line rather than
        `rows 0`, and a `metrics`, at step 0 or here, that prints none
        is no reading. Before stage 6, no key backs the stores up, and
        both start empty.
    6.  `deploy.yml` is enabled again, and `.claude/drill/` is deleted.

    **What a failure leaves running.** Through step 2, the old cluster,
    whole: steps 1 and 2 changed state alone, and they run in the same
    sitting as step 3, after step 0's go. After step 3 and until step 5
    passes, no control plane exists, the old worker idles until step 5
    deletes it, and the balancer answers nothing; the reserved IP is
    unassigned and the volume detached; the gateway, the overlay and the
    zones run as before, since nothing in them reads the cluster.
    Recovery is forward, steps 4 and 5 again. A create refused at step
    5 leaves that instance missing, and its dependents, the day-1 chain
    among them, are not created, so nothing is bootstrapped; the
    launched nodes reboot on Talos's 70-minute timer. A preview of a
    plain `physical up` is read and the `up` run, at intervals, until
    the instance launches. The `--replace` flags are not repeated for a
    resource the failed run already replaced, and §3.2 covers a worker
    left half-built. Any other failure is fixed forward in a pull
    request (physical/bring-up.md §3).

    **Not established, and read by the first run that meets it:**

    -   whether a targeted `destroy` of an instance refuses while a
        protected dependent stays outside the targets, or drops it; the
        termination of step 3 stands either way;
    -   after an actual loss, whether step 1's deletes go through with
        no node to reach; a resource whose delete does not go through
        leaves state with `physical state delete`.

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
carries the escrowed generations alone. As of 2026-11-17 nothing reads the key: of the ops
repository's workflows, `lint.yml` checks the workflow files
themselves, `probes.yml` runs `state-backend probe` and
`drift-trigger.yml` dispatches the drift check, and none of them
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

**As of 2026-11-17, one row runs, and only in part.** Of the
credential expiry tripwires, the state-backend server certificate's
runs daily in the ops repository's `probes.yml`, beside the age of the
appliance's newest dump (state-backend.md §6). Both checks are the
cluster's under the rule below, and `probes.yml` runs them until the
cluster's own have reported. Its alert job
dispatches into the intake, whose handler is not built
(`kluster-ops#715`), so a failing probe is a red run there and nothing
more (the alert contract, below). The ops-repository row, the
state-backend rebuild, has no workflow written (`kluster-ops#57`). No
in-cluster row has its job written: not the etcd restore-verify, and
not the two restores or the orphan-volume audit, whose recipe
(storage.md §3.3) is not written either. `k8s-base` declares
what they would stand on: the operators the two restores drive, the
hourly etcd snapshot the restore-verify would read, which takes none
until bring-up stage 6 seals its key (storage.md §5), and the alerting
every in-cluster row reports into. None of it runs until the cluster's
bring-up applies it (physical/bring-up.md).
The last column is the designed form, and where each scheduled job
runs is the census below. Every row is an operator act today, and
the only one written out as a procedure is the state-backend restore
(state-backend.md §7.3.1). By the standing principle above, that makes
every recovery path on this table an assumed-broken one.

| Drill | Cadence | Form (designed) |
| --- | --- | --- |
| CNPG restore (immich pattern, ported from legacy) | Monthly | In-cluster CronJob, alert on failure |
| State-backend rebuild — scratch micro from Butane → restore latest dump (drill key) → verify → destroy (state-backend.md §7.3) | Quarterly | Ops-repo workflow, alert on failure |
| etcd snapshot restore-verify — latest B2 snapshot into a scratch etcd, health + key sanity | Monthly | In-cluster CronJob in `etcd-backup`, alert on failure |
| VolSync spot-restore — rotating PVC into a scratch namespace, checksum, and for an embedded database the rule below, tear down | Monthly | In-cluster CronJob, alert on failure |
| Metrics-history restore — both stores restore themselves on the rebuilt cluster, and the history's earliest sample and rows match the baseline (§3.5, pass condition 5) | Each whole-cluster rebuild (§3.5) | The rebuild drill's readings |
| Orphan-volume audit, target zero (storage.md §3.3) | Quarterly | In-cluster CronJob, alert only on findings |
| Credential expiry tripwires (credentials.md §4) | Continuous (scheduled checks) | Each placed by the rule below like any scheduled job (the census); `actionable` alert when an expiry approaches |
| **Offline day**: age key rotation (proves offline custody, state-backend.md §7.4) + full cold-standby reverse bootstrap on homelab libvirt (nodes.md §5) + offline-kit verification against the register (credentials.md §2.1) + a `pulumi preview` against the Vultr-fallback stack config (nodes.md §3.1 — proves the scripted fallback still computes, creating nothing) + anything the probes can't reach | Yearly | One `actionable` issue, human-run |

**The peak recorder runs around every run that creates nodes or
applies `k8s-base` from empty, from the moment `physical` holds the
control planes' addresses** (§8.5): a control plane's replacement and
the worker's (§3.1, §3.2), the whole-cluster rebuild above, whose
steps name it, and the bring-up
([physical/bring-up.md](physical/bring-up.md) §5). Such a run installs
the monitoring partway through or replaces the worker the stores live
on, so the cluster records none of its first minutes, which carry its
peak, and the recording is what the per-wave gate and the dashboard
read of them. What it cannot record is what comes before those
addresses: a node's files until `physical`'s `up` has written its
address, though its etcd is read by name as soon as the API server
answers.

**Rule: a checksum does not pass a claim that holds an embedded
database.** The VolSync spot-restore of a claim whose application keeps
an embedded database in it (storage.md §3.1 says which claims those are)
passes only when that application starts on the restored copy, or when
the engine's own check passes on it, such as SQLite's
`PRAGMA integrity_check` returning `ok`. A checksum match proves only
that the restore reproduced the copy, and a copy that `Direct` tore
reproduces faithfully. A failure is answered by restoring the copy
before it, which is what storage.md §5's RPO for such a claim assumes.
Where the failed copy does not open while that one does, the failure
is the evidence storage.md §3.4 names for adopting a snapshot-capable
driver on that claim's node class.

**Destroy dates are not on that clock, and only one of them is a date.**
A retired **kit** owes nothing forward: `credentials kit rotate` re-wraps
every generation to the successor recovery key inside its own run, so
once `credentials derived check` passes and each generation is confirmed
to open under that key, the retired database is destroyable on a
property rather than after a wait (credentials.md §4.2). A retired **backup generation** keeps a real
clock — its escrow ciphertext is the only copy of the identity that
opens the dumps and the etcd snapshots written under it, so it stays
until the last of those objects falls out of its prefix's retention.
The two prefixes keep an object for different lengths: a dump is gone
30 days after the rotation that stops encrypting to the generation
(state-backend.md §5), and an etcd snapshot is current for 14 days and
hidden for 30 more, 44 in all (storage.md §5, `conventions.backup`),
counted from the `k8s-base` apply that carries the rotation's
recipients. The generation is destroyable once both have passed
(state-backend.md §7.4).
Nothing records that date: `credentials kit rotate` leaves the retired kit
byte-for-byte as it was and a generation's ciphertext carries no expiry,
so a probe has no field to read. Honoring it is part of the yearly
offline day until the register carries it.

**Where a scheduled job runs is ci.md §3's rule.** A job runs in the
cluster unless its credential would give an unmerged ref's runs
something they do not already hold. Work that keeps the state of one
device outside the cluster, a dump of its database or a copy of its
backups, runs with that device instead: on the device, or, for a
device that runs no timer of its own, on the homelab host that reads
it. A check that watches a device never runs on it: the cluster test
places it, and where that test keeps it out of the cluster and the
homelab host already reads the device, it runs there. The ops
repository is the last resort, with the reason beside each job there.
Of the drills above, the state-backend rebuild alone runs in the ops
repository, because its OCI user administers the `drill` compartment.
The two restores are kube-native operations on scratch namespaces, the
restore-verify starts a scratch etcd inside its own pod, and the
orphan-volume audit compares each node's on-disk volume directories
against the live volumes, which needs what only a node can read; all
four are the cluster's. Each automated job in the cluster or the ops
repository, and the gateway's daily check on the homelab host, is
covered against stopping silently, because a job that silently stops
running is indistinguishable from a failing one: an ops-repository
workflow by its dead-man timer, an in-cluster CronJob by the
scheduled-job rule family (the rule below) and, behind the family, the
cluster's check-in, and the daily check by the homelab host's check-in
(both in the alert contract). The family is
declared with the rest of the cluster's rules in `k8s-base`, and it reads
a CronJob only once the CronJob is declared: a job that waits for its
sealed value, as the metrics stores' backups, the pull of OCI's
block-volume series, the etcd snapshot and the state table's upkeep do,
reports nothing until the value is sealed in. Bring-up stage 6 seals every value `k8s-base`
reads, each of those keys among them, and reads each job's first run;
what bounds a value it missed is the pull request of stage 8 that
deletes the sealed tolerance, from which an absent value stops the run
by name (physical/bring-up.md §1). The dead-man
timers do not exist yet, and until they do, an ops-repository workflow
that stops running reports nothing. A device's own jobs are
watched as the census below says: the gateway's by its daily check,
and the daily check by its check-in. One calendar act is left to the
operator, the yearly offline day, because its steps need the offline
kit and physical custody, which no job holds (the table above). The
copy of the kubeconfig into `k8s-base`'s and `apps`'s configuration is
an operator act too, since it reads `physical`'s state under
`physical`'s passphrase, which no job but `physical`'s own holds, but it
is no calendar act: `KubeconfigCopyExpiring` asks for it, ahead of the expiry of the
client certificate the copy carries (§2.5, credentials.md §3).

**What runs on a clock, and where.** Every job the installation
schedules, in the cluster, on a device or on GitHub, with the reason
for its place. The last column says which run, as of 2026-11-17:

| Job | Runs in, and why | Credentials there | Liveness to Home Assistant | Built |
| --- | --- | --- | --- | --- |
| etcd snapshot, hourly | The cluster: `siderolabs/talos-backup` as a CronJob in `etcd-backup`, on the control planes (storage.md §5) | A certificate Talos issues for `os:etcd:backup` and renews itself; the `etcd` writer key, sealed | The family's rules (§7.1), then the cluster's check-in | No: declared once bring-up stage 6 seals the `etcd` key (storage.md §5) |
| Age of the newest object under `etcd/` | No job of its own: the snapshot job's stale rule stands in for it, since a run that succeeded is an object uploaded, and the monthly restore-verify reads the newest object's age as a second, independent reading | None | The snapshot job's rules | No, with the snapshot job |
| VolSync backups, one `ReplicationSource` per backed claim | The cluster: VolSync's own schedule, the claim's retention class (`conventions.backup.RETENTION_CLASSES`, storage.md §5) | The namespace's writer key and each repository's password, sealed | The backup-freshness family (declarative/workloads.md §3), then the cluster's check-in | No: none is declared until the first claim under a class lands |
| CNPG barman backups, per database cluster | The cluster: the operator's own schedule, beside continuous WAL archiving (storage.md §5) | The namespace's writer key, sealed | The backup-freshness family, then the cluster's check-in | No: no database cluster is declared yet |
| The metrics stores' backups, hourly | The cluster: a CronJob per store, in `monitoring` (storage.md §5) | The `vm` writer key, sealed | The family's rules (§6.2), then the cluster's check-in | No: declared once bring-up stage 6 seals the `vm` key (storage.md §5) |
| The block volumes' metrics, pulled hourly into the history store | The cluster: a CronJob in `monitoring` | The OCI metrics reader's key, sealed (credentials.md §3) | The family's rules (§6.3), then the cluster's check-in | No (`kluster-ops#790`) |
| The authentication canary, every few minutes | The cluster: a CronJob in Authelia's namespace that requests a protected path with no credential (declarative/cluster-infra.md §2) | None: it presents none | Its own rule, which fires on anything but a refusal; the family's rules for the job; then the cluster's check-in | No: it ships with Authelia (rfc-008 §5.2) |
| The state table's upkeep, daily: the census, the prune of the rows the retention rule expires, and the ceiling | The cluster: the CronJob `state-upkeep` in a namespace of its own, on the control planes, as the role `upkeep` (state-backend.md §2). It is no device's job: it deletes rows of the live table and checks the box, and a check never runs on what it watches. What it holds reads and deletes rows that an unmerged ref's runs already read and write (threat-model.md §3.1) | The `upkeep` client bundle, sealed (credentials.md §3) | The family's rules (state-backend.md §7.6), then the cluster's check-in | No: the first `up-k8s-base` after its merge declares it, its bundle sealed in ahead of it (`kluster-ops#784`) |
| State-backend dump age, hourly | The cluster: the CronJob `state-backend-dumps` in `monitoring`, listing `pulumi-state/` (state-backend.md §6) | The list-only `B2 freshness key (state dumps)`, sealed | The family's rules (state-backend.md §6), then the cluster's check-in | No: the first `up-k8s-base` after its merge declares it, its key sealed in ahead of it (`kluster-ops#832`); `probes.yml` reads it today |
| State-backend server certificate's expiry, hourly | The cluster: the CronJob `state-backend-certificate` in `monitoring`, reading the leaf off a Postgres STARTTLS handshake with the appliance (state-backend.md §6) | None: the box sends its certificate before anything authenticates | The family's rules (state-backend.md §6), then the cluster's check-in | No: the first `up-k8s-base` after its merge declares it (`kluster-ops#832`); `probes.yml` reads it today |
| The kubeconfig copies' expiry, `KubeconfigCopyExpiring` | The cluster: a rule vmalert evaluates against the expiry `k8s-base` reads off its own copy, `conventions.cluster.KUBECONFIG_ALERT_MARGIN` ahead of it. It holds no credential, and it watches a date in `k8s-base`'s configuration rather than the cluster it runs on (§2.5) | None: the date is the certificate's public half | The cluster's check-in | No: it runs from the first `k8s-base` apply that declares monitoring |
| The other credential expiry tripwires | Each placed by the same rule. Two are checked in the cluster: the state backend's server certificate, by the CronJob above, and the kubeconfig copies' client certificate, by the rule above. The `ci` and `operator` client bundles report their expiry at their first `pulumi` command, and the `upkeep` bundle at the state table's upkeep's first run after it (state-backend.md §6); the destroy dates stay with the offline day | — | — | — |
| etcd restore-verify, monthly | The cluster: a CronJob in `etcd-backup` | The `etcd` writer key, which lists and reads, and an age identity of its own, sealed | The family's rules, then the cluster's check-in | No (`kluster-ops#57`) |
| CNPG restore, VolSync spot-restore, orphan-volume audit | The cluster (the drills above) | The writer keys of the backups they read, sealed; the audit needs none | The family's rules, then the cluster's check-in | No |
| The state-backend appliance's dump, nightly (`state-dump.timer`) | The appliance itself: the job dumps the box's own database (state-backend.md §5) | The dump key, `writeFiles` alone, in the box's Ignition | The dump-age check above; the box's own failed-run notice reaches only a shell on it (state-backend.md §6) | Yes |
| The state-backend appliance's OS updates, a weekly window (Zincati) | The appliance itself: they update the box's own OS (state-backend.md §1) | None beyond the box's own | None, deliberately (state-backend.md §6) | Yes |
| The gateway controller's autobackup, monthly | The gateway itself: the controller's own schedule, a console setting | None | The gateway's daily check, which holds the newest autobackup to 32 days (physical/gateway.md §1.6) | Yes |
| The gateway's weekly backup pull (`gateway-backup.timer`) | The homelab host: it keeps copies of the gateway's backups, and the gateway runs no timer of its own (physical/gateway.md §1.6) | The host's SSH access to the gateway | The daily check, which reads how fresh the copies are: a run that failed is reported at the next daily check, and a pull that stopped is reported within one of the controller's autobackup periods, plus eight days, of the stop. Both hold while the daily check runs, which its check-in watches | Yes |
| The gateway's daily check (`check-gateway.timer`) | The homelab host: the check reads the gateway as `root` over SSH, which the cluster may not hold, and the host already reads the gateway (physical/gateway.md §1.6) | The host's SSH access to the gateway; the check-in's webhook id, in a file yadm encrypts on the host | Its findings, mailed with their playbooks, and its check-in to `timer.homelab_ops_check_gateway` after every run that delivered its findings or had none (the alert contract) | Yes |
| Slot-drift probe | The ops repository: it lists every repository's and Environment's secrets, which takes a GitHub credential (credentials.md §4) | A token that lists secrets | Its alert job, then its own timer | No (`kluster-ops#1`) |
| Issue-sync poller, hourly | The ops repository: it writes that repository's issues, which acts on GitHub (architecture.md §4.3) | The `alertmanager/read` token, and the workflow's own token | Its alert job, then its own timer. Its "monitoring unreachable" issue is the GitHub leg of a cluster that cannot be reached | No (`kluster-ops#57`) |
| Weekly drift trigger, `drift-trigger.yml` | The ops repository: the trigger App starts runs on this repository's `main`, which acts on GitHub (framework/ci.md §3) | The trigger App's key and client id | Its alert job, then its own timer | Yes |
| The drift check, `drift.yml` | This repository's CI, dispatched by the trigger: it needs `physical`'s passphrase, which threat-model.md §3.1 keeps from every unmerged ref | The CI Environments | Its own alert job | Yes |
| State-backend rebuild drill, quarterly | The ops repository, in the `drill` Environment: its OCI user administers the `drill` compartment, and nothing an unmerged ref holds can create an instance (state-backend.md §7.3) | The `drill` Environment | Its alert job, then its own timer | No (`kluster-ops#57`) |
| The ops repository's `probes.yml`, daily | The ops repository, until the two in-cluster checks of the state backend above have reported a pass; it then retires, and its two secrets with it | The `B2 freshness key (state dumps)` | Its alert job, then its own timer | Yes |

Outside the rule are the jobs that do not run on a clock: the ops
repository's `lint.yml`, which runs on push and pull request and gates
that repository's own workflow files, and its dispatch handler
(`kluster-ops#715`), which runs on each dispatch and writes that
repository's issues.

**Rule: every CronJob that does the installation's own scheduled
work — a backup, a check or a drill — is watched by the scheduled-job
rule family**: *stale* at `conventions.backup.max_age` of its period,
and *last run failed* within its run deadline. Both rules are
`actionable`, and each names the job's playbook. The facts the
declaring component and the rules share — namespace, CronJob name,
period and run deadline — are one row in `conventions.schedule`, from which
`components.monitoring.rules` derives both rules, and a test holds
every CronJob the `k8s-base` and `apps` programs declare to a row, and
every row to a CronJob one of them declares. Both read the
CronJob series of kube-state-metrics. Stale is the time since the last
success, or since the CronJob's creation where there is none, past the
job's `max_age`, which also catches a job suspended or no longer
scheduled at all. Last-run-failed is a last schedule later than that
same mark, held for the run deadline: a failed run is reported within
its deadline rather than after one and a half periods, which matters
once a job is monthly, and it resolves when a later run succeeds. A job
is watched by the family rather than by a timer of its own, because a
timer per job would need every job to post to Home Assistant itself,
which duplicates alertmanager. What notices a cluster that can no
longer report a stopped job is the cluster's check-in, a timer outside
it (the alert contract below).

**The alert contract.** Every ops-repo drill and probe above, and
every workflow here that fails on `main` (framework/ci.md §3 names
any it still excuses), reports through the dispatch intake
(architecture.md §4.3); what the cluster raises, the in-cluster drills
included, reports through alertmanager, to an intake of its own. What
every side of each intake agrees on is fixed here, and it is the
contract the Home Assistant side is built against:

-   **The event.** One `repository_dispatch` into the ops repo, whose
    event type, tiers and payload fields are the census
    `kluster.conventions.alert`, which says what each field holds.
    A source is `<repo>/<workflow>`, the workflow named by its file
    name without the suffix. CI sends the event through its producer,
    `alert.yml` (ci.md §3); the ops repo's own workflows send
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
-   **alertmanager's post to Home Assistant.** alertmanager's body is
    fixed, so the cluster posts to a second intake automation, a
    webhook trigger that accepts `POST` from the home site alone, where
    the post comes from. Only its id is sealed, the value
    `conventions.sealed.ALERT_WEBHOOK`; the URL is assembled around it
    from `conventions.HOME_ASSISTANT`'s address and Home Assistant's
    port and webhook path, which the SealedSecret renders into the
    Secret alertmanager reads, so the address is held once and a moved
    one needs no new seal. The post stays direct, at Home
    Assistant's own address, which only the homelab worker reaches and
    where monitoring therefore runs
    ([rfc-008](rfc/rfc-008-workload-migration.md) §4.1); it never
    addresses Home Assistant by its name, and never rides the cluster's
    relay or the overlay. Every alert in the body
    carries a `tier` label from the census and `summary` and
    `playbook` annotations (`kluster.components.monitoring.rules`),
    and only firing alerts are sent. Per alert, the automation
    restarts the cluster's timer when the tier is `heartbeat`, and
    otherwise pushes a notification titled
    `[<tier>] cluster/<alertname>: <summary>` whose message is the
    playbook. alertmanager repeats a firing alert on its own interval,
    so an `actionable` alert pushes again until it resolves; the issue
    leg that would deduplicate it is the poller, which is not built
    (architecture.md §4.3).
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
    haos-webhook`). An in-cluster job has no timer of its own: the
    scheduled-job rule family watches it (the rule above), and the
    cluster's check-in below is the timer behind the family.
-   **The homelab host's check-in.** The gateway's daily check
    (physical/gateway.md §1.6) is watched by a Home Assistant timer of
    its own, `timer.homelab_ops_check_gateway`, named from the source
    its findings mail carries, `homelab-ops/check-gateway`. Its
    duration is 36 hours, `conventions.backup.max_age` of the daily
    cadence, and it survives a restart. The unit's `ExecStartPost=`
    runs `check-in check-gateway` after every run that exits 0, which
    is a healthy run or one whose mail was sent, so a check-in means
    the findings reached the operator or there were none. It POSTs
    once, with an empty body, to a webhook of its own, whose automation
    accepts `POST` from the home site alone and whose one action
    restarts the timer. There is one webhook per job, because the id
    is the whole credential: its holder can restart that job's timer
    and nothing else. The id is the host's, in a file yadm encrypts,
    with no register row, like the job's SSH access to the gateway (the
    census). There is no retry: one lost check-in is absorbed by the
    next run, and two in a row expire the timer. A check-in is not an
    alert: it carries no tier, pushes nothing and reaches neither
    intake. The timer's expiry pushes "no check-in from
    homelab-ops/check-gateway", whose message is the playbook,
    physical/gateway.md §3, "The check stopped checking in". It
    reports a timer, a unit, the user manager or a tool shim that
    stops. A homelab host that stops takes Home Assistant with it,
    since Home Assistant is a domain on that host, and nothing pushes:
    the residual every Home Assistant timer here carries,
    `timer.kluster_cluster` among them.
-   **The cluster's check-in.** A rule that always fires, in the
    `heartbeat` tier, reaches alertmanager's intake once per repeat
    interval, ten minutes, and at most one group interval, a minute,
    after it (`rules.HEARTBEAT_REPEAT` and `HEARTBEAT_GROUP_INTERVAL` in
    `kluster.components.monitoring`). Each one restarts
    `timer.kluster_cluster`, whose duration is `rules.HEARTBEAT_TIMER`,
    `conventions.backup.max_age` of the repeat, fifteen minutes, and
    which survives a restart. Its expiry pushes "no check-in from the
    cluster": the cluster stopped evaluating rules or stopped
    delivering them, which no in-cluster alert can say. Its playbook, from the root of the
    checkout:
    whether the worker is up and `Ready` (§3); whether vmalert and
    alertmanager run in the `monitoring` namespace
    (`mise x -- kubectl -n monitoring get pods`); whether the receiver's
    configuration is in use (`mise x -- kubectl -n monitoring get
    vmalertmanagerconfig`, whose status names a missing Secret); and,
    with all of that in order, alertmanager's own playbook for failing
    notifications (architecture.md §4.3).

What is built of it is the census, CI's producer, the alert job every
ops-repository workflow on a clock ends in, and the cluster's side:
alertmanager's receiver, the rules, the scheduled-job rule family among
them, and the check-in, declared in `k8s-base` and running once the
bring-up applies it and seals the webhook in. Home Assistant's side of
the cluster's alerting is built: the alertmanager intake, the cluster's
check-in timer and its expiry push. The homelab host's check-in is
built whole, and runs: the script and its unit line on the host, and in
Home Assistant its webhook automation, its timer and the timer's expiry
push. The handler is not built, nor are the ops repository's dead-man
timers, `timer.kluster_ops_<workflow>`, and their automations: until
the handler exists, a dispatch is accepted and starts nothing, and an
alert from CI or from the ops repository is a red run and nothing more.

## 5. Playbook index

The unified alert channel requires every alert to name its playbook
(architecture.md §4.3); this index is where the names resolve.
Owning docs keep the content — the index only locates it.

| Playbook family | Lives in |
| --- | --- |
| State backend (cert/CA, PG major, rebuild, age rotation; the state table's daily upkeep, a run that failed or the job gone stale; the cluster's dump and certificate checks, a run that failed or a check gone stale, which §6 there routes to its playbook) | physical/state-backend.md §6 and §7 |
| Gateway (ZeroTier container down, firmware-wiped device, UDM replacement; and every finding of the daily gateway check: a machine, the boot chain, a delivered unit, the routing daemon or the offline package cache, a check that did not run, a check that stopped checking in, a certificate not renewing, an autobackup not copied off) | physical/gateway.md §3 |
| Node replacement (CP node, worker VM, block volume and VIP extras; the whole cluster with the account intact) | §3 here |
| Upgrades (Talos serial, Cilium canary); the cluster credential's copies near their expiry (`KubeconfigCopyExpiring`) | §2 here |
| Backup restores (CNPG, VolSync, etcd) | storage.md §5; the drills are §4 here |
| Alert-channel failure (HA push down → meta-alert; GitHub leg down; a scheduled ops-repo workflow that stopped checking in) | architecture.md §4.3; the contract and the dead-man are §4 here |
| CI alerts (a red `checks` or `images` run; a drift diff) | framework/ci.md §3.1 and §3.2 |
| The metrics stores (rows a store ignores; a backup run that failed, a backup gone stale or refused, or a store that does not restore; a pull of OCI's block-volume series that failed or went stale; a scrape job that reports nothing, or no scrape job at all) | §6 here |
| The etcd snapshot (a run that failed, or snapshots gone stale) | §7 here |
| The control planes (etcd slow or stalled, its leader changed or gone, a control plane under CPU or I/O pressure, a boot volume throttled) | §8 here |

vmalert rule families adopt this index as they are ported: an alert
that cannot point at a row (or at its owner doc's census) does not
ship — the same-change rule. For the rules the cluster declares it is
a test rather than a review question: `tests/test_monitoring.py` holds
every rule the `k8s-base` program declares to a `playbook` that names a
document and a section that document has, beside a tier from the
census and a summary (architecture.md §4.3).

## 6. Metrics store playbooks

What to do when the monitoring stack reports on itself. The stack and
its scrapes are declared in `components.monitoring`
([declarative/cluster-infra.md](declarative/cluster-infra.md) §1,
item 6). The commands below run from the root of the checkout.

### 6.1 A row the metrics store ignores

**Trigger**: `MetricsDropped`, `actionable`. `vm_rows_ignored_total`
grew in the last 30 minutes, under the `reason` and the `job` the alert
carries: a store's count of rows it received and kept none of, or
vmagent's of rows past its own limits, which it sent to neither store.
vmagent counts a row a store ignores as delivered, so nothing else
shows the loss: a series that loses a row in each scrape is missing whole rather
than sparse, and every reading over it reads nothing.

**Gist**:

1. **Read the reason.** `too_many_labels` is a series past the 40
   labels vmsingle accepts (`-maxLabelsPerTimeseries`, unset here, so
   its default). `too_long_label_name` and `too_long_label_value` are a
   label past 256 characters or a value past 4 KiB. `big_timestamp` and
   `small_timestamp` are samples stamped far from the store's clock,
   which is a source's clock and not its labels.
2. **Find the series.** The store or the vmagent the alert's `job`
   names logs at most one ignored series every 5 seconds, whole:
   `mise x -- kubectl -n monitoring logs -l app.kubernetes.io/name=vmsingle | grep 'ignoring series'`,
   with `vmagent` in place of `vmsingle` for vmagent's.
   The series' `job` and its name say which scrape produced it.
3. **Repair the scrape that adds the labels**, in the component that
   declares it: the chart's scrapes and etcd's in
   `components.monitoring`, Cilium's there too. The usual cause is a
   `labelmap` that copies a whole class of the source's metadata, a
   node's or a pod's labels, onto every series; the repair keeps the
   one label a reading groups by and drops the rest. A previewed apply
   of `k8s-base` lands it.
4. **Confirm**: `increase(vm_rows_ignored_total[30m])` reads 0 for
   every reason, the series the log named is present, and the alert
   resolves 30 minutes after the last ignored row.

### 6.2 A metrics store's backup failed or is stale

**Trigger**: the scheduled-job family's two alerts on the backup
CronJob the alert names, `vmbackup-recent` or `vmbackup-history`, both
`actionable` (`conventions.schedule.METRICS_BACKUPS`, the rule in §4).
`MetricsBackupFailed`: the job's last run, scheduled after its last
success, has not succeeded within the 25 minutes a run is cut off at.
`MetricsBackupStale`: the job has not succeeded for an hour and a half,
the one rule for stale over its hourly period
(`conventions.backup.max_age`), or has never succeeded since it was
created that long ago. The bucket's copy of that store is falling
behind, and a rebuild would restore it only as far as its last success
(storage.md §5).

**Gist**:

1. **Which alert.** `MetricsBackupFailed` is a run that failed, and the
   steps from step 2 on find why. `MetricsBackupStale` firing without
   it is a job no run of which was scheduled after its last success,
   since a run that was is the failed alert's. The CronJob is suspended,
   the controller that schedules it is not running, or the creation of
   its Job was refused: the controller records a schedule only once it
   has created the Job.
   `mise x -- kubectl -n monitoring get cronjob <job>` reads its
   `SUSPEND` and its `LAST SCHEDULE`. The program suspends no job, so a
   suspension is a hand's, and
   `mise x -- kubectl -n monitoring patch cronjob <job> -p '{"spec":{"suspend":false}}'`
   lifts it. A refused creation is a `FailedCreate` event, which
   `mise x -- kubectl -n monitoring describe cronjob <job>` lists with
   what refused it: an admission webhook, a quota, or a namespace being
   deleted. An unsuspended job with no such event whose last schedule
   stopped moving is the controller manager's, which runs on each
   control plane as `kube-controller-manager-<node>` in `kube-system`.
2. **Read the last run.**
   `mise x -- kubectl -n monitoring get jobs --sort-by=.metadata.creationTimestamp`
   lists the namespace's runs from the oldest, each named `<job>-<n>`; the
   newest of the alert's job is the last of its name, and
   `mise x -- kubectl -n monitoring logs job/<run> --all-containers` says
   where it stopped. Each line before the failure names the step it
   starts.
3. **A run cut off at its deadline.**
   `mise x -- kubectl -n monitoring describe job <run>` reads
   `DeadlineExceeded`, and the run's log ends partway through the
   upload: it took longer than the 25 minutes a run is allowed, which a
   store's first backup, a full upload from the worker, can. `vmbackup`
   resumes from the parts a cut-off run already uploaded, so the runs
   after it converge on a complete backup with nothing done here. Until
   one of them completes, the store's copy in the bucket is a backup the
   restore refuses (step 7), and a store whose node is lost meanwhile
   does not start. Runs that are cut off hour after hour without
   converging are a deadline too short for the store, which is its row's
   in `conventions.schedule`. An hourly period leaves a deadline of 29
   minutes at most, the row's slack, so a store that needs longer needs
   a longer period.
4. **A pod that never started.** It waits on what it mounts or reads:
   the writer key's Secret, `writer-vm-monitoring`, which a SealedSecret
   the controller cannot open never creates, and which the sealing
   sitting of bring-up stage 6 (physical/bring-up.md §2) seals again
   after a rebuild (§3.5); or the scripts' `ConfigMap`, `metrics-backup-shell`. `mise x -- kubectl -n
   monitoring describe pod` names which.
5. **The store did not answer.** The snapshot is taken through the
   store's Service, so a store that is down fails every backup. A store
   whose pod sits in `Init` is its restore, step 7.
6. **The history store's backup refused.** The log reads `holds <n>
   rows, fewer than the <m> the last backup recorded at
   vm/monitoring/history.rows`. Its retention keeps every row, so a
   store holding fewer started without its history, and its backup
   would overwrite the history in the bucket. Restore it: delete its
   claim, `mise x -- kubectl -n monitoring delete pvc vmsingle-history`, and its
   pod; the operator makes the claim again, empty, when it next
   reconciles the store, and the restore
   brings the store back from its last backup, losing only what it took
   in since it started empty. Where fewer rows are deliberate, series
   deleted through the store's keyed delete endpoint, the record is
   hidden instead, in the B2 console, and the next run records afresh.
7. **A store that does not restore** sits in `Init`, one of its two
   restore containers failing: `mise x -- kubectl -n monitoring logs <pod> -c
   restore-check`, then `-c restore`. The first fails where the bucket
   or the key does not answer; the second where `vmrestore` refuses the
   backup, which it does for one whose last run was cut off part way.
   The store does not start meanwhile, by design: an empty store would
   start over the history the bucket holds. vmagent queues what it
   scrapes for that store, up to 1 GiB, until it starts. Where the
   bucket or the key was what failed, nothing more is needed: the pod
   runs its restore containers again, and the next attempt that reads
   the bucket restores.

   **A backup cut off part way**, by the worker's loss during a run or
   by the run's deadline, is one `vmrestore` refuses. Where the store
   still runs, its next successful run completes the backup again and
   nothing more is needed. Where the store is to be restored from it,
   the backup is repaired first, in the B2 console, as the account's
   owner, since no key in automation deletes. `vmbackup` hides the
   completion marker, `backup_complete.ignore`, before it changes
   anything, then hides the parts the new snapshot lacks and uploads
   the new ones. Each partition's `parts.json` and `appliedRetention.txt`
   are among both, on every run: their contents change, so `vmbackup`
   never matches them against the destination's copy, and hides that
   copy and uploads the file again under the same name
   (`lib/backup/common/part.go`, `key`, at v1.150.0). So under the
   store's prefix, `vm/monitoring/history/` or `vm/monitoring/recent/`,
   with file versions shown, the cut-off run is every hide marker and
   every upload from the moment the marker was hidden, and for a name
   the run both hid and uploaded, its three newest versions are the
   earlier upload, the run's hide marker, and the run's upload. To bring the last
   completed backup back:

   1. delete the hide marker of every file the run hid, the completion
      marker among them;
   2. delete every file version the run uploaded, rather than hiding
      it: a name the run both hid and uploaded, each partition's
      `parts.json` among them, then shows its earlier version again,
      where a hide would take that version back out of the backup;
   3. delete the store's pod; the restore runs again on the lock file
      `vmrestore` left in the data directory, and the store starts on the
      backup as it stood before the cut-off run.

   **The clock**: a hidden version is purged by the bucket's lifecycle
   rule `BACKUP_VERSION_RETENTION_DAYS`, 30 days, after it was hidden
   (storage.md §4). The repair has to come inside those 30 days of the
   cut-off run, or the history it hid is gone. For the recent store,
   whose loss costs a month of detail, the store may start empty
   instead: hide everything under its prefix, then delete its claim,
   `mise x -- kubectl -n monitoring delete pvc
   vmsingle-monitoring-victoria-metrics-k8s-stack`, and its pod, since
   the claim holds the lock file, and the restore refuses a lock with
   no backup behind it.
8. **Confirm**: the next run's log ends with the backup, and for the
   history with `recorded <n> rows`; both alerts resolve at the next
   evaluation after it.

### 6.3 The pull of OCI's block-volume series failed or is stale

**Trigger**: the scheduled-job family's two alerts on the CronJob
`oci-metrics-pull`, both `actionable` (`conventions.schedule.OCI_METRICS_PULL`,
the rule in §4). `OciMetricsPullFailed`: its last run, scheduled after
its last success, has not succeeded within the 25 minutes a run is cut
off at. `OciMetricsPullStale`: it has not succeeded for an hour and a
half, the one rule for stale over its hourly period
(`conventions.backup.max_age`), or has never succeeded since it was
created that long ago. The stores
hold none of OCI's block-volume series since its last success, the
throttled I/O among them. A run reads up to seven days back from the
newest sample the history store holds of each volume, so a gap shorter
than that is filled by the first run that succeeds after it; a longer
one is lost, since OCI keeps one-minute data for seven days.

**Gist**:

1. **Which alert.** `OciMetricsPullFailed` is a run that failed, and
   the steps from step 2 on find why. `OciMetricsPullStale` firing
   without it is a pull no run of which was scheduled after its last
   success, since a run that was is the failed alert's: read the
   CronJob as §6.2's first step does, with `oci-metrics-pull` for the
   job.
2. **Read the last run.**
   `mise x -- kubectl -n monitoring get jobs --sort-by=.metadata.creationTimestamp`
   lists the namespace's runs from the oldest, each named `<job>-<n>`; the
   newest `oci-metrics-pull-<n>` is the last of its name, and
   `mise x -- kubectl -n monitoring logs job/<run>` says which step it
   was in when it failed, each step announced before it starts. A
   metric that meets an error stops alone, logged as `<metric> stopped
   <step>`, and the run goes on to the next metric and exits 1 at the
   end; the steps below say what each error is.
3. **A run cut off at its deadline.**
   `mise x -- kubectl -n monitoring describe job <run>` reads
   `DeadlineExceeded`, and the log ends in the step that was running
   when the 25 minutes a run is allowed ran out. What the run wrote
   stays: it writes each day of a metric as it reads it, and the next
   run reads on from the newest sample the history store holds, so the
   first run that completes fills the rest, up to seven days back. A
   run cut off hour after hour is a step that hangs, and the step the
   log ends in names which.
4. **A pod that never started.** It waits on what it mounts: the key's
   Secret, `oci-metrics`, which a SealedSecret the controller cannot
   open never creates, and which the sealing sitting of bring-up stage 6
   (physical/bring-up.md §2) mints and seals again after a rebuild; or
   the `ConfigMap`, `oci-metrics-pull`.
   `mise x -- kubectl -n monitoring describe pod` names which.
5. **OCI refused the key** (`NotAuthenticated`, a 401). The key the
   cluster holds is not one the reader's user carries: re-running
   `credentials derived oci-metrics mint` retires the old key once its
   ciphertext is committed, and every run fails until `k8s-base` is
   applied with the new one. Apply `k8s-base` through the merge chain;
   the next run fills the gap.
6. **OCI refused the request** (`NotAuthorizedOrNotFound`). The
   reader's policy reads one metric namespace of `physical`'s
   compartment and nothing else (credentials.md §3). A request that does
   not name that namespace is refused by the policy's condition, as is
   one for another compartment: compare the namespace and the
   compartment in the run's settings, the `settings.json` of the
   `ConfigMap`, with `conventions`.
7. **A volume with no sample.** The log ends with `no sample of <volume>
   on <node> in the window`, after every other volume was written: OCI
   returned no series under that volume's OCID. The OCID is the one
   `physical` published for it when `k8s-base` was last applied, so a
   volume replaced since, from the workstation, is one: apply
   `k8s-base` again, which reads the new OCID. A series the log reports
   `skipping` is the replaced volume's old one, which needs nothing.
   Where the OCID is current, OCI has no series for the volume, and its
   console's Block Volume metrics page says whether it has any.
8. **A series that names no volume.** The log reads `a series of
   <metric> carries no resourceId, only the dimensions <names>`: OCI
   answered with series of another shape than the query asks for,
   which groups each metric's series by the volume's OCID. No apply
   repairs that; the fix is the query in
   `kluster.components.monitoring.oci_pull`, by a pull request, and the
   dimensions the log names are what the new query reads. The runs
   between keep failing for that metric, and the first one after the
   fix merges fills the gap, up to seven days of it.
9. **A plan of `k8s-base` refuses the volume map**: `physical`'s output
   holds no OCID for a volume the census declares, the state a
   targeted `physical` apply leaves a volume it did not create in.
   Apply `physical`, then `k8s-base`.
10. **Confirm**: the next run logs every volume written and exits 0,
   and both alerts resolve at the next evaluation after it. The history
   store answers `oci_blockstore_VolumeReadOps` for each volume up to
   the run.

### 6.4 A scrape job reports nothing

**Trigger**: `ScrapeTargetDown` or `ScraperDown`, both `actionable`,
held for five minutes (the scrape-target family,
`kluster.components.monitoring.rules`). A rule reads only what a scrape
job reports, so a job that reports nothing leaves every rule that reads
it evaluating over no series: silent, whatever it watches. The family
watches every job some other alert reads, directly or through the
recording rules it reads, which the program derives from the rules'
expressions. `ScrapeTargetDown` names the `job` it is
about: a target of it whose scrape fails, or no target of it listed at
all. `ScraperDown` is no target of any job reporting, which is vmagent
stopped, or none of what it scrapes reaching the recent store; while it
holds, no `ScrapeTargetDown` fires, since every job is down for the one
reason. A recent store that is itself down stops vmalert from
evaluating anything, which is the cluster's check-in going quiet
instead (§4).

How soon an alert fires depends on how the series ended. A failing scrape
writes `up` as 0, and a target that leaves discovery is marked stale,
so either alert fires five minutes after it. A series that ends with
no stale marker is still read for one of vmalert's query steps, five
minutes, after its last sample, so its alert fires about ten minutes
after it. vmagent writes no marker when it stops, which is why
`ScraperDown` fires about ten minutes after vmagent stops.

**Gist**:

1. **Which alert.** `ScraperDown` is step 4. For `ScrapeTargetDown`,
   read `up{job="<job>"}` against the recent store, in Grafana's
   Explore: a target at 0 is listed and failing, step 2; no series at
   all is no target listed, step 3.
2. **A target listed and failing.** Its pod runs, or ran, and its
   metrics endpoint does not answer vmagent.
   `mise x -- kubectl -n monitoring get pods` shows a pod restarting or
   killed for memory, and `mise x -- kubectl -n monitoring describe pod
   <pod>` and its log say why. vmagent's own reading of the scrape is
   its targets page: `mise x -- kubectl -n monitoring port-forward
   svc/vmagent-monitoring-victoria-metrics-k8s-stack 8429`, then
   `http://localhost:8429/targets`, which names the error each target's
   last scrape met.
3. **No target listed.** Service discovery finds nothing for the job:
   its workload is gone or scaled to nothing, its pod has no address
   (`Pending`, waiting on the worker, §3), or its Service or its scrape
   object is gone. `mise x -- kubectl -n monitoring get pods,svc` and
   the scrape objects (`vmservicescrape`, `vmpodscrape`, `vmnodescrape`)
   name which, and vmagent's `/service-discovery` page, beside the
   targets page of step 2, lists the targets discovery found and
   dropped. A job whose name changed — a scrape's relabeling, or the
   release the stack is installed as — is watched under the old name
   until the program derives the new one: the names come from
   `scrape_jobs` in `kluster.components.monitoring`, which a change to
   a scrape updates in the same pull request.
4. **No target of any job.** vmagent:
   `mise x -- kubectl -n monitoring get pods -l app.kubernetes.io/name=vmagent`
   and its log, which says whether it scrapes and whether the recent
   store accepts what it writes. What it scraped and could not write is
   buffered on its disk and sent once the store accepts it again.
5. **Repair** in the component that declares the scrape or the
   workload: the chart's scrapes, etcd's and Cilium's in
   `components.monitoring`. A previewed apply of `k8s-base` lands it.
6. **Confirm**: `up{job="<job>"}` reads 1 for every target, and the
   alert resolves at the next evaluation. The rules that read the job
   read it again from then on. Those reading a state, such as a node's
   readiness or a CronJob's last success, report what happened
   meanwhile at once. `MetricsDropped` counts the rows a store ignored
   meanwhile. After a gap longer than its 30 minutes it reads the
   store's whole counter as new, so a store that has ignored any row
   since it started raises it again for 30 minutes; the log in §6.1
   step 2 says whether the rows are new.

**The jobs the family names are checked against the recent store**
whenever an exporter changes: a release of kube-state-metrics,
alertmanager or VictoriaMetrics, or a change to a scrape. `scrape_jobs`
claims which job exports each metric a rule reads by name alone, and
nothing the program declares holds those claims. In Grafana's Explore,
`min by (job) (up)` reads 1 for every job whose targets are all up,
each one `scrape_jobs` names among them; and each of
`count by (job) ({__name__=~"kube_.+", __name__!~"kube_apiserver_.+"})`,
`count by (job) ({__name__=~"alertmanager_.+"})`,
`count by (job) ({__name__=~"vmagent_.+"})` and
`count by (job) (vm_rows_ignored_total)` lists the jobs `scrape_jobs`
claims for it and no other. A job that answers and is not claimed is
one whose stopping would silence a rule: its claim goes into
`scrape_jobs` in the pull request that brings the change.

## 7. The etcd snapshot's playbooks

What to do when the hourly etcd snapshot reports on itself. The job is
`siderolabs/talos-backup`, declared in `components.backup.etcd` as the
CronJob `etcd-snapshot` in the `etcd-backup` namespace, on the control
planes ([cluster/storage.md](cluster/storage.md) §5). The commands below
run from the root of the checkout.

### 7.1 The etcd snapshot failed or is stale

**Trigger**: the scheduled-job family's two alerts on the CronJob
`etcd-snapshot`, both `actionable` (`conventions.schedule.ETCD_SNAPSHOT`,
the rule in §4). `EtcdSnapshotFailed`: its last run, scheduled after
its last success, has not succeeded within the 15 minutes a run is cut
off at. `EtcdSnapshotStale`: it has not succeeded for an hour and a
half, the one rule for stale over its hourly period
(`conventions.backup.max_age`), or has never succeeded since it was
created that long ago. A run that succeeded is a snapshot uploaded, so
the newest snapshot under `etcd/` is as old as the last success, and a
restore from the bucket loses everything since (storage.md §5). Each
snapshot is whole on its own, so a run that succeeds again needs
nothing from the ones that failed.

**Gist**:

1. **Which alert.** `EtcdSnapshotFailed` is a run that failed, and the
   steps from step 2 on find why. `EtcdSnapshotStale` firing without it
   is a job no run of which was scheduled after its last success: read
   the CronJob as §6.2's first step does, in `etcd-backup` and with
   `etcd-snapshot` for the job. A run the controller could not start
   within ten minutes of its minute is skipped rather than run late,
   and leaves no Job.
2. **Read the last run.**
   `mise x -- kubectl -n etcd-backup get jobs --sort-by=.metadata.creationTimestamp`
   lists the runs from the oldest, each named `etcd-snapshot-<n>`, and
   `mise x -- kubectl -n etcd-backup logs job/<run>` ends in the one
   line a failed run writes, which names the step that failed. The steps
   below go by that line's opening words.
3. **A pod that never started.** It waits on what it mounts or reads:
   the Secret `etcd-snapshot`, which Talos writes for the
   `ServiceAccount` of that name; or the writer key's Secret,
   `writer-etcd`, which a SealedSecret the controller cannot open never
   creates, and which the sealing sitting of bring-up stage 6
   (physical/bring-up.md §2) seals, and seals again after a rebuild
   (§3.5), with `credentials derived b2-writer seal --only writer-etcd`.
   `mise x -- kubectl -n etcd-backup describe pod` names which. The
   same sitting seals the snapshots' read-only key, `reader-etcd`, again
   with `credentials derived b2-reader seal`; the snapshot does not read
   that one, so it is never what holds the pod back. Talos writes the
   Secret `etcd-snapshot` only for the role and the namespace the control
   planes' machine configuration grants (`conventions.ETCD_SNAPSHOT_GRANT`,
   [declarative/physical.md](declarative/physical.md) §2), so a
   `ServiceAccount` that stands with no Secret beside it is outside the
   grant: a `physical` apply puts the grant back.
4. **`failed to get talosconfig` or `failed to create talos client`.**
   The talosconfig in the Secret `etcd-snapshot` did not open. Talos
   writes it again when it renews the certificate, within the last hour
   of its six; a run that fails this way hour after hour is the
   previous step's missing grant.
5. **`failed to take etcd snapshot`.** apid refused or could not be
   reached. `PermissionDenied` is a certificate without the
   `os:etcd:backup` role, the grant of step 3. A connection error is the
   `talos` Service in `default`, through which the client reaches apid
   on a control plane:
   `mise x -- kubectl -n default get endpointslices -l kubernetes.io/service-name=talos`
   lists the control planes Talos keeps behind it on port 50000, and an
   empty list is a Talos with the grant off. An error from etcd itself
   is the member the run reached, and the control planes' etcd is what
   to look at (§3). A line that ends in `no space left on device` is
   none of these, and is step 9's.
6. **`failed to encrypt etcd snapshot`.** One that ends in `no space
   left on device` is step 9's. Otherwise, a recipient did not parse.
   The recipients are the committed escrowed backup generations
   (`src/kluster/lib/state_backend/machine/backup-recipients.txt`),
   which `k8s-base`'s plan refuses with a line that is no recipient,
   so a run that fails this way runs a configuration no plan made.
   Apply `k8s-base` through the merge chain.
7. **`failed to push encrypted snapshot`.** B2 refused the upload. An
   `InvalidAccessKeyId` or `SignatureDoesNotMatch` is a key the cluster
   holds that is not the one `physical` holds, which a `physical` apply
   that replaced the key leaves behind: seal it again with
   `credentials derived b2-writer seal --only writer-etcd`, commit
   `Pulumi.k8s-base.yaml`, and apply `k8s-base` through the merge
   chain. An `AccessDenied` is an object name outside `etcd/`, the one
   prefix the key writes, which is the run's `S3_PREFIX` against
   `conventions.ETCD_SNAPSHOT_PREFIX`. An unfinished upload a failed run
   leaves is cancelled by the bucket (storage.md §4).
8. **A run cut off at its deadline.**
   `mise x -- kubectl -n etcd-backup describe job <run>` reads
   `DeadlineExceeded`. A run streams one snapshot of the database and
   uploads it, which takes seconds at the database's size, so a run cut
   off at 15 minutes is a step that hung, and the log's last line names
   the step it was in.
9. **A working directory that filled.** The log's last line is
   `failed to take etcd snapshot: error reading: … no space left on
   device`, or `failed to encrypt etcd snapshot: … no space left on
   device`: the snapshot and its ciphertext outgrew the in-memory
   directory, whose size is `components.backup.etcd.WORK_SIZE`, half
   the memory nodes.md §1.1 keeps available on a node. The database's size
   is what grew, and every run fails the same way until the bound
   changes, which is a change to that budget, or the directory moves
   onto a disk other than etcd's.
10. **Confirm**: the next scheduled run logs
   `Uploading <file> to bucket aetf-kluster-backup with key etcd/<file>`,
   its Job reads `Complete`, and both alerts resolve at the next
   evaluation after it.

## 8. The control planes' playbooks

What to do when a reading of the control planes leaves the budget
[cluster/nodes.md](cluster/nodes.md) §1.1–1.2 gives it. The readings,
the rules that record them and the alerts over them are
`kluster.lib.monitoring.readings` and `kluster.components.monitoring.rules`
([declarative/cluster-infra.md](declarative/cluster-infra.md) §1,
item 6). The `Control planes` dashboard in Grafana draws every reading
from the history store, each beside the same reading an earlier period
back, the dashboard's variable setting how far:
`mise x -- kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80`,
then `http://localhost:3000`. The commands below run from the root of
the checkout, and `<node>` is the node the alert carries.

A planned peak produces most of these alerts once: a bring-up or a
rebuild (§3), a Talos upgrade (§2), a wave's land
([cluster/migration.md](cluster/migration.md) §0 rule 6), hath's warm
copy, VolSync's movers at 03:00. One that comes during such a peak and
resolves after it is recorded on the work's issue with the reading
beside it, which is what the per-wave gate reads; one that comes
outside any is what the steps below are for. §8.5 is how the peak of a
bring-up or a rebuild, which no store in the cluster sees, reaches the
history all the same.

### 8.1 etcd is slow

**Trigger**: one of four alerts, each naming the member by its `node`.

-   `EtcdWalFsyncSlow`, `actionable`: the member's WAL fsync p99 over
    5 minutes is over 10 ms, and has been for 10 minutes.
-   `EtcdBackendCommitSlow`, `actionable`: its backend commit p99 is
    over 25 ms, and has been for 10 minutes.
-   `EtcdApplySlow`, `actionable`: its mean apply over 5 minutes is
    over 100 ms.
-   `EtcdDiskStall`, `notify`: a WAL fsync took over a second in the
    last 5 minutes, which is what etcd's own `slow fdatasync` warning
    reports ([declarative/physical.md](declarative/physical.md) §6).

The first three are the targets of etcd's FAQ, which bind at every
moment, peaks included (nodes.md §1.2 rule 1). A fsync and a commit wait
on the boot volume, which etcd shares with the images, the logs and
every cloud-pool claim; an apply waits on the member's CPU as well.

**Gist**:

1.  **Read the disk beside etcd.** On the dashboard, the member's node
    on the Disk row: the boot volume's writes, bytes written, mean
    write latency and I/O in flight, its I/O pressure, and OCI's
    throttled I/O. Write latency in tens of milliseconds with tens of
    requests in flight is the volume saturated, the shape of a
    bring-up (nodes.md §1.2); throttled I/O above 0 is the volume held
    to its performance level, which is §8.4.
2.  **Read etcd's own view.**
    `mise x -- talosctl -n <node> logs etcd | grep -E 'slow fdatasync|took too long'`
    names each slow write and each request that waited on one, with
    its time.
3.  **Find the writer.** The volume's writes come from what ran on the
    node at that time: image pulls
    (`mise x -- kubectl get events -A --field-selector reason=Pulling`),
    a pod starting or a claim filling, VolSync's movers. The Load
    classes row's bytes written by class say which class grew; etcd's
    share there is its WAL alone, so its backend's writes show as the
    platform's. An apply that is slow while the disk is quiet is CPU
    instead, which is §8.3.
4.  **Repair**: a peak that was not planned is shaped as nodes.md §1.2
    shapes a planned one, by the pulls the kubelet runs at once and the
    resource operations an apply runs at once. A steady writer past the
    budget nodes.md §1.2 rule 2 gives writers other than etcd, which the
    Disk row reads as an upper bound over the hour, goes to the operator
    with the reading, which is how the canon handles a reading that
    contradicts the budget (nodes.md §1.1).
5.  **Confirm**: the reading is back inside its budget on the
    dashboard, and the alert resolves at the next evaluation after.

### 8.2 etcd's leader changed or is gone

**Trigger**: `EtcdLeaderChanged`, `notify`: the member saw the
leader change in the last 15 minutes. `EtcdNoLeader`, `actionable`: the
member has had no leader for a minute, which stops every write the
cluster makes through it.

**Gist**:

1.  **A planned move.** A Talos upgrade (§2) or a node replacement
    (§3) moves the leader as it restarts each member, and the alert
    resolves 15 minutes after the last move.
2.  **Read the members.** `mise x -- talosctl -n <node> etcd status`,
    run against each control plane, reports the leader that member
    sees and its raft term, and
    `mise x -- talosctl -n <node> logs etcd` names each election and
    why. On the dashboard, the etcd row: heartbeat send failures and
    the peer round trip say whether the members reached one another
    in time, and the WAL fsync whether the leader's disk held it back;
    etcd's FAQ names a slow disk as the usual cause of a leader that
    misses its heartbeats, which is §8.1.
3.  **No leader.** A member with no leader is cut off from the others
    or has lost quorum. The members reach one another over KubeSpan:
    the dashboard's Network row shows each member's connected peers and
    the worker's scrapes of each control plane, and
    `mise x -- talosctl -n <node> get kubespanpeerstatuses` lists each
    peer and whether it is up. Two members down is quorum lost, which
    is §3's.
4.  **Confirm**: `mise x -- talosctl -n <node> etcd status` names
    one leader on every control plane, and the alert resolves.

### 8.3 A control plane under pressure

**Trigger**: `ControlPlaneCpuPressure`, `actionable`: the node's CPU
pressure (some) averaged over 10 minutes is over 20 %, the state in
which etcd missed its deadlines (nodes.md §1). `ControlPlaneIoPressure`,
`actionable`: its I/O pressure (some) over 10 minutes is over 10 %. Both
are the per-wave gate's own windows (migration.md §0 rule 6).

**Gist**:

1.  **Which class.** On the dashboard's Load classes row, CPU and memory
    used by class on the node, and the CPU and memory limits of the
    front and the workloads against the 0.5 core and 3.5 GiB nodes.md
    §1.1 allows them.
2.  **CPU.** A workload class past its limits is a workload's limit to
    correct, in the `apps` declaration of its own. The control-plane
    slice grows with what the API server is asked: an apply's burst of
    creates, which the deploy chain's parallelism bounds (nodes.md
    §1.2). The platform is the node less the two, Cilium's agent and the
    controllers among it.
3.  **I/O.** As §8.1 steps 1 and 3: the boot volume's readings say how
    hard it was written, and the node's events what wrote it.
4.  **Confirm**: the reading is back inside its budget and the alert
    resolves.

### 8.4 A boot volume throttled

**Trigger**: `BootVolumeThrottled`, `notify`: OCI's
`VolumeThrottledIOs` for a node's boot volume is above 0 in the last
90 minutes, an hour and a half: OCI refused I/O to hold the volume to
its performance level. OCI's series reach the stores through the hourly
pull, up to an hour after the minute they describe (§6.3), so the alert
follows the throttling by up to that.

**Gist**:

1.  **When and how hard.** On the dashboard's Disk row, throttled I/O
    per volume beside the node's boot volume writes, and OCI's
    guaranteed IOPS and throughput, which are the volume's live
    performance level: 3,000 IOPS and 24 MB/s for a 50 GB Balanced
    volume (nodes.md §1.2).
2.  **A level below that** is a volume no longer at Balanced, the
    level `components.cloud.nodes` leaves a boot volume at by declaring
    none (nodes.md §1.2).
3.  **Throttling at the level** is a peak past the rate nodes.md §1.2
    rule 3 shapes peaks to: its shaping bounds the pulls the kubelet
    runs at once and the resource operations an apply runs at once,
    and a peak that exceeds the throttle with that shaping in place
    goes to the operator with the reading, as every reading that
    contradicts the budget does (nodes.md §1.1).
4.  **Confirm**: throttled I/O is 0 for an hour and a half, and the
    alert resolves.

### 8.5 The peaks the cluster cannot see

**When**: around every run that creates nodes or applies `k8s-base`
from empty, from the moment `physical` holds the control planes'
addresses until the history store runs again: a bring-up
([physical/bring-up.md](physical/bring-up.md) §5), a control plane's
replacement and the worker's (§3.1, §3.2), and the whole-cluster
rebuild (§3.5), whose steps name it. Such a run installs
the monitoring partway through, or replaces the worker the stores live
on, so its first minutes, which carry its peak, reach no store in the
cluster. `perf` (`kluster.scripts.perf`) records them from the
workstation and writes them into the history store afterward
(the design of Aetf/kluster-ops#790).

**Record**, from the root of the checkout that holds `.credentials/`,
in a terminal that outlives the sitting:

    mise x uv -- uv run perf record <file>

-   Every 5 seconds it reads, on each control plane, its hostname and
    then `/proc/pressure/{cpu,io,memory}`, `/proc/diskstats`,
    `/proc/loadavg` and `/proc/meminfo` through Talos, under
    `~/.talos/kluster`, and etcd's metrics through the API server's node
    proxy, under `~/.kube/kluster`, once the API answers. It appends
    them to `<file>` and flushes the file after every round.
-   Of etcd's metrics it keeps every family some reading takes as an
    input, found in the readings' own expressions, and whether a member
    has a leader, leads, and was slow to apply.
-   It reads the control planes' addresses from `physical`'s
    `node_private_ips` once, when it starts, under `physical`'s own
    passphrase. A run that replaces the control planes stops it once
    `physical`'s `up` has written their new addresses, and starts it
    again on the same file. Until then an old address can answer as
    another rebuilt node: a node whose hostname is not the name it was
    read for has its files left out, and that is logged.
-   A read that does not answer costs its own rounds alone. One that
    outlasts its round starts again only once it has answered, within
    its bound of 10 seconds, and every other read keeps the 5-second
    cadence. Before the API server answers, each node's
    files are recorded without its etcd. A source's failure is logged
    when it starts and when it ends.
-   Ctrl-C stops it once the reads under way have answered. A recorder
    killed mid-write leaves a line cut off at the end of the file, which
    the next `perf record` on the file drops before it appends, and
    which the import skips.

**Import**, once the history store is running:

    mise x uv -- uv run perf import <file>

-   **It refuses while vmagent holds data queued for the stores.** A
    store that waited in `Init` has its scrapes queued by vmagent until
    it starts, and the import judges where the cluster scraped by what
    the history holds; it reads vmagent's pending data through the API
    server first, and is run again once that has drained.
-   **It leaves out what the cluster scraped.** Two of the cluster's own
    samples of a node's job no more than four scrape intervals apart,
    two minutes, bracket a stretch of its scrape, missed scrapes inside
    it included, and a recorded line inside such a stretch is left out.
    Every other line is written, up to a stretch's first sample and from
    its last.
-   **Each region of the recording is a series of its own.** The lines
    written between two of the cluster's stretches carry
    `recorded_from`, the time the region starts, so a region after a
    stretch never resumes the series of the region before it.
-   **It leaves out what an earlier import wrote**: a line within a
    round, 15 seconds, of an earlier import's sample of the same node
    and job. So a second import of the file writes nothing, and one cut
    off partway, such as by the API server not answering, ends non-zero
    with what it wrote kept; a second run writes the rest, short of at
    most a round where the first stopped.

For each node and job it prints how many lines it wrote and how many it
left out, as scraped and as imported already.

**What the readings show of a recorded period.** Each recorded series
carries the `job` and the `node` the cluster's scrape gives the same
series, and `source="peak-recorder"`, so every reading, the
`Control planes` dashboard and the per-wave gate read a recorded period
as they read a scraped one. **At a seam, where the recording meets a
new series of the cluster's own scrape** or a new series of the
recording meets the end of the cluster's, neither holds the time from
the last recorded round to the first scrape, or from the last scrape to
the next recorded round, so a count, such as the WAL's syncs over a
second or the leader changes, loses at most what one round of 5
seconds held there. A window of a rate reading, such as a pressure or
the boot volume's writes, that holds the tail of one series and the
head of the other reads both, each at its own rate over its own part
of the window: up to twice the rate, for that one window at each seam.
A node exporter pod the cluster replaces leaves the same seam in the
readings.

**Where the cluster's own series resumes under the labels it had**,
after a gap the recording filled — a gap in its scrape longer than two
minutes with the control planes unchanged, such as the worker's
replacement (§3.2) — a count read by a range query that reaches across
the gap reads the gap's events twice in the first window after the
resume: once from the recording, and once from the cluster's series,
which VictoriaMetrics reads across the gap from its sample before it.
An instant query reads it true outside the window that holds the
resume.

**Confirm**: on the dashboard, the run's window holds the pressure,
disk and etcd rows of every control plane.
