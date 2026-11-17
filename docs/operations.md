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

| Surface | PR opened by | Applied by | Policy |
| --- | --- | --- | --- |
| Talos version (machine-config pin + Image Factory schematic + `mise.toml`'s `talosctl`) | renovate (GitHub-releases datasource) | `physical` stack for the pin and the schematic; `talosctl upgrade` by hand, one node at a time in §2.1's order (declarative/physical.md §2) | Reviewed; one pull request moves `versions:talos` and the `talosctl` pin together, because `tests/test_talos_validate.py` holds the two equal; §2.1 runbook |
| Kubernetes version | same PR family (Talos-coupled) | `talosctl upgrade-k8s` | Reviewed; after the Talos bump it belongs to |
| Cilium chart | renovate — the custom manager for an OCI chart's `versions:chart-` pin in `Pulumi.yaml`, moving its version and digest together; alone in a `cilium` group, out of the `in-cluster` one, the way Talos travels alone | CI chain (merge = deploy) | Reviewed, **never automerged** — §2.2 runbook; ≥1.20.2 floor (ExternalAuth, which from that release fails closed on a backend it cannot use, [cilium/cilium#47929](https://github.com/cilium/cilium/pull/47929)), checked by `update_crds`, which the bump is finished by running on its branch: the chart's version is the ref Cilium's definitions are rendered from |
| k8s-base charts (cert-manager, CNPG, VolSync, sealed-secrets, VictoriaMetrics, …) and the Gateway API definitions | renovate — custom managers on the `versions:chart-` pins (the Helm data source for an HTTP repository, the docker one for an OCI registry, which moves the digest with the version) and on the `versions:manifest-` pin (its release and sha256 together); one `in-cluster` group, Cilium excepted, with the cluster images the block pins | CI chain | Reviewed — chart bumps always produce a real diff; major behind dashboard approval. A bump of a pin `update_crds` reads is red until `update_crds` runs on its branch, because `packages/crds` records the pins it was generated from (framework/pulumi.md §4). `mise.toml`'s `kubeseal` moves in this group's pull request with the sealed-secrets chart: renovate's mise manager reads that pin too, and a rule takes it out of the `toolchain` group into this one, because a test holds it equal to the chart's `appVersion` (framework/pulumi.md §4). The tool's half waits out the release age, which the chart's does not, and is finished by `mise lock`, as a bump in the `toolchain` group is |
| **Cluster images, infra and app alike** (CNPG operands, self-built, third-party app images) | renovate | CI chain (merge = deploy) | **Minor automerge** (patch stream folded into minor — the legacy `patch: enabled: false` precedent — except where a pin's release line moves by patches alone, as local-path-provisioner's does, each of its releases being a `v0.0.N` patch: a package rule in `renovate.json5` names each such dependency and re-enables its patches); **major behind dashboard approval + review**; CNPG operand major additionally gated on the self-built image line (workloads.md §4). The safety valve is the deploy-failure alert, not a per-bump eyeball — this deliberately reverses legacy's "applications get eyeballed" stance. A self-built image (ci.md §4) is published under a fingerprinted tag, `<TAG>-<12 hex digits>`, and a published tag never changes, so a consumer takes a new build by moving to its new tag; renovate offers that move only under a versioning that matches the fingerprint without capturing it, such as `regex:^(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)-[0-9a-f]{12}$` for emailproxy. Two consumers pin these images today: kluster-code, by plain tag, and this repository's `Pulumi.yaml`, by fingerprinted tag and digest, under a versioning `renovate.json5` gives each image. Of what a self-built image is built from, the commit golinks is built from moves by hand, because upstream publishes no releases; a `pgvecto.rs` or `vchord` release (both in `vchord-cnpg.conf`) waits on dashboard approval, because each is an extension upgrade of immich's existing database; every other version and base digest arrives as an ordinary renovate pull request, the digest refreshes grouped once a month |
| blog image / built branch | blog repo CI | git-sync | Automatic — content, not code |
| nspawn rootfs (caddy, AdGuard, ZeroTier) | renovate here — docker datasource, reading whole references off the `versions:image-gateway-…` pins | `physical` stack: the device pulls the pinned manifest itself and unpacks it beside the tree it is running, then the boot chain's machine script restarts what changed | Reviewed; the run-number tag reads as a major bump, so every one waits on dashboard approval |
| State-backend pins: the Fedora CoreOS release the image is imported from, with its artifact's digest (`settings.FCOS_RELEASE`, `settings.FCOS_ARTIFACT_SHA256`); the Postgres image, its major line's tag with the digest the box runs (`settings.POSTGRES_IMAGE`, rendered into the Butane file); the age release (`settings.AGE_VERSION`) | Zincati (periodic window) for the running OS; renovate for the three settings, one custom manager each on `state_backend/settings.py` — the release and its digest captured together, read off the stream's own metadata through a custom data source; the Postgres tag and digest captured together, the digest's moves taken monthly | Zincati: auto. Nothing on the box moves Postgres. A release bump: `operator-stack state-backend up`, which imports a new image and replaces nothing. A Postgres digest or major, or an age bump: `operator-stack state-backend up --force`, which dumps the box, replaces it and restores into the new one (state-backend.md §7.2) | Reviewed, grouped apart from everything else because each bump changes the box, and all but the release replace it. An age bump is one pull request moving `settings.AGE_VERSION` and the `age` pin in `mise.toml` together: renovate's mise manager reads that pin too, and a rule takes it out of the `toolchain` group into this one, because `tests/test_age.py` holds the two pins equal. The pull request is finished by hand: `AGE_SHA256` beside `settings.AGE_VERSION` is a digest renovate cannot recompute, and the `checks` workflow's `state-backend pins` refuses it stale. state-backend.md §1–2, and §7 for the age pin |
| Pulumi SDK + providers, Python deps, Actions versions, `jdx/mise-action` excepted (the row of the `toolchain` and `mise release` groups) | renovate | **noop-automerge workflow** — merges a bump that changes nothing but paths on its allow-list (`uv.lock`, the `packages:` block with the `sdks/` it regenerates) once the preview is proven empty (the zero-diff rule, ci.md) | Automerged when on the list and diff-free; a bump that edits any other path — `pyproject.toml`, an action pin in a workflow — or that produces a real diff is human review; major behind dashboard approval |
| Every tool `mise.toml` pins under `[tools]`, with uv's second pin (`renovate.json5`'s `constraints.uv`) and mise itself (the `jdx/mise-action` pins in the workflows with the `version:` input they pass it, `deploy/cloud-session/toolchain.sh`'s `mise_version`, and `renovate.json5`'s `constraints.mise`), but a pin a rule after the `toolchain` rule takes into another group, today `talosctl` (the Talos row), `age` (the state-backend row) and `kubeseal` (the k8s-base charts row), and `http:dash`, which renovate does not read (the gateway's dash row) | renovate — its `toolchain` group: the mise manager's dependencies and the `renovate-config` manager's `uv`, read from the releases the `mise.toml` pin is read from. mise under its three names, `jdx/mise-action`, `jdx/mise` and the `renovate-config` manager's `mise`, is a group of its own, `mise release`. Rules after the `toolchain` rule take pins out of the group, today `talosctl`, `age` and `kubeseal`, and with them a major release of the local `postgres` client, which goes into the state-backend appliance group because a test holds its major equal to the appliance image's. `kubeseal` goes into the `in-cluster` group, the k8s-base charts row's, because a test holds it equal to the sealed-secrets chart's `appVersion` | the merge: every CI job that runs a `mise` command installs mise at the workflows' pin and the tools from `mise.toml` and `mise.lock`, and a cloud session does the same at its start, through `deploy/cloud-session/toolchain.sh`, whose `mise_version` a test holds equal to the workflows'; a workstation takes the tools at its next `mise install` | Reviewed — every file either group edits, `mise.toml` and the workflows among them, is off noop-automerge's allow-list, so a person merges it; each group travels alone because a pulumi, uv or mise bump changes how every other check runs; major behind dashboard approval. A bump of a `mise.toml` pin is finished by the `mise lock` that moves its entry in `mise.lock` (the paragraph on recorded bytes above): renovate runs it on the branch with the mise `constraints.mise` names, which a test holds equal to the workflows' pin. Renovate reads that constraint from `main`, so it is the mise a branch's install runs on every branch but the one moving mise, and that is why `mise release` travels apart from the `toolchain` group: it moves no `mise.toml` pin, so it writes no lock, and its pins of mise need none, since they have no entry there and name a version alone (threat-model.md §2.6). Every `mise.lock` change renovate writes is therefore written by the mise that installs it. The `mise release` pull request is where a new mise first reads entries earlier ones wrote, `main`'s lock under its `checks`, and a refusal there is answered by `mise lock` with the new mise onto that branch. A run that fails marks `renovate/artifacts` red and leaves the entry unmoved. Without the host's allow-list, a run whose `mise version` probe fails is skipped: the pull request arrives without the entry and without that mark, and `checks` refuses it at the install. Either way a person runs `mise lock <tool>` onto the branch. The `gawk` pin stays on the appliance's line, within which renovate offers nothing: the appliance's Fedora CoreOS major moves it, by the re-check its comment in `mise.toml` describes, which renovate's pull request for that major asks for in its body |
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
    too. Cadence: after every `physical` `up`, and at least monthly,
    which no 30-day window can slip past. In a bring-up the first copy
    waits for stage 4 of physical/bring-up.md §2 rather than following
    `physical`'s first `up`: the commit of both stack files is an `up`
    of `k8s-base` and `apps`, whose Environments take no reviewer, and
    the bring-up's checks that need no network plugin run before Cilium
    exists (physical/gateway-cutover.md §8.1). Gist: `credentials
    derived sync --only kubeconfig` from the checkout that holds the kit, then commit
    `Pulumi.k8s-base.yaml` and `Pulumi.apps.yaml`, whose copies the two
    stacks open their Kubernetes provider with (credentials.md §3). A
    stack already holding the current value as a secret is left alone,
    so a run writes only what changed. A stale copy keeps opening the
    cluster only while the certificate it carries is valid and its
    authority still trusted. Past that, both stacks fail to authenticate
    to the API server — a provider error, not `UnusableKubeconfig`,
    which only an absent or blank copy raises — until the copy is made.

## 3. Node replacement and resize runbooks (census)

-   **§3.1 Cloud CP node.** Trigger: hardware loss, A1 reclamation,
    or deliberate rebuild. **Rule: no instance is destroyed through the
    program.** A lost node is recovered by a targeted
    `refresh --run-program` and an `up`. A deliberate replacement is a
    create-first `up --target-replace`, the order the regional quota is
    sized for: the fleet plus one node (cluster/nodes.md §3.2).

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

    **A lost node.** OCI terminated it, or it is gone for good: the
    instance reads `TERMINATED`, or the node is to be abandoned in
    place. From the root of the checkout that holds `.credentials/`,
    with `physical` the function of declarative/physical.md §6, `tal`
    and `CPS` the setup of physical/gateway-cutover.md §8.1, and
    `<node>` the lost node's name (`cp2`):

        ocid() { physical stack export | jq -r --arg t "$1" --arg n "$2" '.deployment.resources[] | select(.type == $t and .outputs.displayName == $n) | .id'; }
        credentials derived oci-physical read "$(ocid oci:Core/instance:Instance kluster-<node>)"
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
    (credentials.md §4.4). A URN carries `$`, which the array keeps as
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
    every node named, and §2.5's sync follows it, as it follows every
    `physical` `up`.

    One fact of this path is not established, and the first recovery
    reads it: whether the health gate, which the refresh's run of the
    program reaches, waits on the lost node and fails the refresh.

    **A deliberate replacement.** The node is up and is to be replaced:
    drain it, then, with `U` its instance's URN,
    `physical preview --diff --target-replace "$U"` and, once its plan
    has been read, `physical up --target-replace "$U"`. The
    replacement launches first, from the program's current values, and
    the old instance is deleted at the end of the run. While both run
    they are two nodes of one name, which evict each other's etcd member
    (declarative/physical.md §1). How that window is held is settled on
    its own (Aetf/kluster-ops#742), and no deliberate replacement of a
    control plane runs before it is. On the dedicated VIP's node the
    attachment is replaced with the instance, and is protected, so it
    is unprotected first with `physical state unprotect`.
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
    (`components/talos/image.py`, `IMAGE_CACHE`), and the libvirt
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
    -   the cluster endpoint, the balancer's two addresses: the records
        of `kluster.hosts`, every apid SAN and every machine
        configuration, and the server of the kubeconfig;
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
    configuration. Once an application holds data, its data comes back
    by the restores of storage.md §5. This procedure carries neither.

    **A1 capacity is the risk it takes.** It gives up, by construction,
    the hold of cluster/nodes.md §3.2, never needing more than one
    create at once: three creates follow three terminations, each in
    the AD its node's position fixes, and a full AD refuses its create
    for as long as the hunt takes, which may be days (cluster/nodes.md
    §3.1). The regional quota admits the whole fleet at once, since it
    is the fleet plus one node. Before stage 6 a hunt costs calendar
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
        protected() { physical stack export | jq -r '.deployment.resources[] | select(.protect) | .urn' | sort; }
        outside() { physical stack export --show-secrets | jq -c --arg s "$SCOPE" '[.deployment.resources[] | select((.urn | test($s)) or (.urn | endswith("::pulumi:providers:pulumi-python::default")) | not) | {urn, inputs, outputs, protect}] | sort_by(.urn)' | sha256sum; }
        anchors() { for n in kluster.hosts vip1.hosts; do for t in A AAAA; do echo "$n $t $(mise x -- doggo --short "$n.unlimited-code.works" "$t" @1.1.1.1 | sort | tr '\n' ' ')"; done; done; }
        sealing() { mise x -- kubeseal --kubeconfig ~/.kube/kluster --fetch-cert \
            $(mise x uv -- uv run python -c 'from kluster import conventions as c; print("--controller-namespace", c.SEALING_NAMESPACE, "--controller-name", c.SEALING_CONTROLLER)'); }

    `SCOPE` matches every resource the run changes, and nothing else:
    the cloud nodes with what hangs off them, the attachment, the
    day-1 chain, the worker's domain and volumes with its image, and the
    stack's outputs. It matches the components `CloudNodes`,
    `NodeVolume`, `TalosDay1` and `HomelabHost` themselves as well,
    because a component's registered outputs change with what it holds.
    Each component sits at the top of the stack, so the pattern anchors
    on `::` before its type (§3.1).

    `outside` prints a digest of every other resource's entry, secrets
    included, so that only the digest leaves the pipe. It leaves out the
    stack's default `pulumi-python` provider too, the provider of every
    dynamic resource: its entry records the stack's whole configuration,
    and any `physical up` rewrites it to the configuration of the commit
    it runs, so a configuration merged since the last `up` changes the
    entry with nothing outside the scope changed, and no preview shows
    the difference. Taking the baseline after an `up` instead would make
    step 0 a write; the configuration is the commit's, and a review of
    the commit is where it is read.

    **Step 0: the baseline, and the go.**

        gh workflow disable deploy.yml --repo Aetf/kluster
        physical preview --refresh --run-program --expect-no-changes
        for s in dns k8s-base apps; do mise x -- pulumi preview --refresh --expect-no-changes --stack "$s"; done
        physical stack export > "$D/export.json"
        protected > "$D/protected"; outside > "$D/outside"; anchors > "$D/anchors"; ids > "$D/ids"; sealing > "$D/sealing.pem"
        for o in cluster_endpoint cluster_endpoint_v6 vip1 worker_gua; do echo "$o $(physical stack output "$o")"; done > "$D/outputs"

    Disabling `deploy.yml` is bring-up stage 4's way back used as a
    freeze: a merge, a Renovate automerge among them, would otherwise
    run the chain against a half-built cluster. Every preview exits 0,
    and a diff stops the run: it starts converged, so that every later
    diff is its own. `gateway-health` is clean (physical/gateway.md
    §1.6). A deliberate run then takes §3.4's capacity reading, and goes
    only if every AD passes; otherwise it waits for another day, with
    nothing changed but the workflow, which is enabled again.

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
        physical preview --json "${W[@]/#/--replace=}" | jq -r '.steps[] | select(.op != "same") | .urn' | grep -Ev "$SCOPE"
        physical up "${W[@]/#/--replace=}"

    `W` holds the worker's `FactoryImage` and its disk. The scope check,
    the second preview through `grep`, prints nothing. The plan holds
    eleven creates: the three instances, the secondary private IP, the
    attachment and the six resources of the day-1 chain; one update,
    the reserved IP's `private_ip_id`; replacements of every backend
    and of the worker's image, disk, seed and domain; and the stack's
    outputs. Anything else, the node volume above all, is a diff nobody
    expected, and nothing is applied until it has been read. The `up`
    ends on the Talos health gate, every node named. The new disk is
    created at its declared size, which leaves the worker short of disk
    once step 8 has deployed both stacks onto it: an open gap,
    Aetf/kluster-ops#787.

    **Step 6: `physical` converges.** A further `physical up` reports
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
    (declarative/physical.md §6); the node volume's first item passes,
    `ids` printing what `$D/ids` holds; `u-storage` is ready on every
    node; and the node label sits on `cp1`. The volume's other items ran
    at bring-up and are not repeated.

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
    sealing certificate differing from `$D/sealing.pem`; then the pass
    readings.

    **It passes when each of these holds:**

    1.  Step 5's `up` exits 0 behind the health gate, and step 6's
        readings hold.
    2.  Step 7's readings hold, the volume's two UUIDs those of the
        baseline.
    3.  Bring-up stage 5's gate holds on the rebuilt cluster, and each
        of `up-k8s-base` and `up-apps` converged in its one run.
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
    5.  `deploy.yml` is enabled again, and `.claude/drill/` is deleted.

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

**As of 2026-11-12, one row runs, and only in part.** Of the
credential expiry tripwires, the state-backend server certificate's
runs daily in the ops repository's `probes.yml`, beside the age of the
appliance's newest dump (state-backend.md §6). Its alert job
dispatches into the intake, whose handler is not built
(`kluster-ops#715`), so a failing probe is a red run there and nothing
more (the alert contract, below). The other ops-repo rows have no
workflow written (`kluster-ops#57`): not the state-backend rebuild,
and not the etcd restore-verify, nor the scheduled snapshot it would
restore. No in-cluster row has its drill written. `k8s-base` declares
what they would stand on, the operators the two restores drive and
the alerting every in-cluster row reports into, and none of it runs
until the cluster's bring-up applies it (physical/bring-up.md). The
orphan-volume audit's recipe (storage.md §3.3) is not written either.
The last column is the designed form
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
silently stops running is indistinguishable from a failing one. The
family is declared and evaluates over no series until a backup source
reports its last sync; until the workflows, the dead-man and those
series exist, a drill that never started is indistinguishable from
both. The only calendar ritual
left is the yearly offline-day issue.

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
-   **alertmanager's post to Home Assistant.** alertmanager's body is
    fixed, so the cluster posts to a second intake automation, a
    webhook trigger whose id is in the sealed value
    `conventions.sealed.ALERT_WEBHOOK` and which accepts `POST` from
    the home site alone, where the post comes from. The post stays direct, at Home
    Assistant's own address, which only the homelab worker reaches and
    where monitoring therefore runs
    ([rfc-008](rfc/rfc-008-workload-migration.md) §4.1); it never rides
    the cluster's relay or the overlay. Every alert in the body
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
    haos-webhook`).
-   **The cluster's check-in.** A rule that always fires, in the
    `heartbeat` tier, reaches alertmanager's intake once per repeat
    interval, ten minutes, and at most one group interval, a minute,
    after it (`rules.HEARTBEAT_REPEAT` and `HEARTBEAT_GROUP_INTERVAL` in
    `kluster.components.monitoring`). Each one restarts
    `timer.kluster_cluster`, whose duration is `rules.HEARTBEAT_TIMER`,
    `conventions.backup.max_age` of the repeat, fifteen minutes, and
    which survives a restart. Its expiry pushes "no check-in from the
    cluster": the cluster stopped evaluating rules or stopped
    delivering them, which no in-cluster alert can say. Its playbook:
    whether the worker is up and `Ready` (§3); whether vmalert and
    alertmanager run in the `monitoring` namespace
    (`kubectl -n monitoring get pods`); whether the receiver's
    configuration is in use (`kubectl -n monitoring get
    vmalertmanagerconfig`, whose status names a missing Secret); and,
    with all of that in order, alertmanager's own playbook for failing
    notifications (architecture.md §4.3).

What is built of it is the census, CI's producer, and the cluster's
side: alertmanager's receiver, the rules and the check-in, declared in
`k8s-base` and running once the bring-up applies it and seals the
webhook in. The handler, the ops repo's own intake, and the Home
Assistant automations and timers are not: until the handler exists, a
dispatch is accepted and starts nothing, and a CI alert is a red run
and nothing more.

## 5. Playbook index

The unified alert channel requires every alert to name its playbook
(architecture.md §4.3); this index is where the names resolve.
Owning docs keep the content — the index only locates it.

| Playbook family | Lives in |
| --- | --- |
| State backend (cert/CA, PG major, rebuild, age rotation) | physical/state-backend.md §7 |
| Gateway (ZeroTier container down, firmware-wiped device, UDM replacement; and every finding of the daily gateway check: a machine, the boot chain, a delivered unit, the routing daemon or the offline package cache, a check that did not run, a certificate not renewing, an autobackup not copied off) | physical/gateway.md §3 |
| Node replacement (CP node, worker VM, block volume and VIP extras; the whole cluster with the account intact) | §3 here |
| Upgrades (Talos serial, Cilium canary) | §2 here |
| Backup restores (CNPG, VolSync, etcd) | storage.md §5; the drills are §4 here |
| Alert-channel failure (HA push down → meta-alert; GitHub leg down; a scheduled ops-repo workflow that stopped checking in) | architecture.md §4.3; the contract and the dead-man are §4 here |
| CI alerts (a red `checks` or `images` run; a drift diff) | framework/ci.md §3.1 and §3.2 |
| The metrics store (rows it ignores) | §6 here |

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
item 6).

### 6.1 A row the metrics store ignores

**Trigger**: `MetricsDropped`, `actionable`. vmsingle's
`vm_rows_ignored_total` grew in the last 30 minutes, under the
`reason` the alert carries. The store received those rows and kept none
of them. vmagent counts them as delivered, so nothing else shows the
loss: a series that loses a row in each scrape is missing whole rather
than sparse, and every reading over it reads nothing.

**Gist**:

1. **Read the reason.** `too_many_labels` is a series past the 40
   labels vmsingle accepts (`-maxLabelsPerTimeseries`, unset here, so
   its default). `too_long_label_name` and `too_long_label_value` are a
   label past 256 characters or a value past 4 KiB. `big_timestamp` and
   `small_timestamp` are samples stamped far from the store's clock,
   which is a source's clock and not its labels.
2. **Find the series.** vmsingle logs at most one ignored series every
   5 seconds, whole:
   `kubectl -n monitoring logs -l app.kubernetes.io/name=vmsingle | grep 'ignoring series'`.
   Its `job` and its name say which scrape produced it.
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
