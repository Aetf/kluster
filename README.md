# kluster-py

Pulumi Python code for the next-generation hybrid Kubernetes cluster: Talos
Linux spanning an OCI VCN and the Homelab LAN, with Cilium networking. This
repo succeeds `~/projects/kluster-code` (the k3s-based legacy cluster); it
will run nearly the same workloads on new infrastructure, with everything —
cloud instances, gateways, storage, DNS — managed by Pulumi.

Project managed by [uv](https://github.com/astral-sh/uv) and mise.

## Layout

| Path | What |
| --- | --- |
| `__main__.py` | Pulumi program entrypoint; registers the async `kluster.main.main` via `pulumi.run`. Must stay a real file (a console-script symlink's `sys.exit` would kill the async entrypoint before it runs). |
| `src/putils/` | The Pulumi framework layer: `Component`, `async_output`/`resolve` (RFC-001), asyncio helpers. Stable and fully tested. |
| `src/kluster/` | The cluster program itself. `physical/` is currently the pre-GCP AWS leftover (see status below). |
| `packages/crds/` | `crd2pulumi`-generated CRD types, regenerated via `uv run update_crds` (currently still the legacy cluster's chart set). |
| `docs/` | Design docs — see index below. |
| `tests/` | Unit tests for the framework layer (Pulumi mocks, no cloud access). |

## Tools

**pulumi**: `mise x -- pulumi`.

Anything installed by `uv`:

**ruff**: `mise x uv -- uv run ruff`.

**tests**: `timeout 60 mise x uv -- uv run pytest` (always use a timeout; a
coroutine that never resolves hangs instead of failing).

## Docs

Organized by topic: `docs/cluster/` (what we're building and why),
`docs/physical/` (physical-layer system designs — the machines and
appliances themselves, as opposed to how they're declared),
`docs/framework/` (the Pulumi Python framework itself), `docs/declarative/`
(how each layer is declared in the program: physical, dns,
cluster-infra, workloads — all written), plus the cross-layer
registers at the docs root (credentials, operations).

- [docs/cluster/architecture.md](docs/cluster/architecture.md) — the canonical cluster
  architecture (3× OCI A1 combined CP+ingress nodes + Homelab worker;
  Talos, KubeSpan, Cilium; two-pool LoadBalancer ingress behind the free
  NLB), including superseded alternatives.
- [docs/cluster/nodes.md](docs/cluster/nodes.md) — node & provider selection (OCI
  decided, Vultr scripted fallback; all prices verified against official
  sources), OCI commercial-model deep dive, homelab host inventory & VM
  sizing, measured infra tax & economy program, HA tiers.
- [docs/cluster/storage.md](docs/cluster/storage.md) — storage classes (local-path +
  VolSync, NAS, object storage; Longhorn deferred), backup architecture,
  JuiceFS root causes & containment policy.
- [docs/cluster/security-audit.md](docs/cluster/security-audit.md) — independent
  security audit (2026-08-23): the findings register behind
  architecture.md §4.1, each fix designed into the doc that owns the
  mechanism.
- [docs/cluster/migration.md](docs/cluster/migration.md) — the migration plan:
  standing rules (per-app stop-copy-start + DNS repoint, tracker
  retirement, NVMe/RAM interleave), the Phase-0 verification gate,
  waves A–F, data-movement techniques, decommission checklist.
- [docs/physical/state-backend.md](docs/physical/state-backend.md) — the
  state-backend appliance (FCOS on the OCI micro): config management,
  Postgres lifecycle, PKI, network exposure, backup, monitoring, and the
  operational playbooks.
- [docs/physical/homelab-host.md](docs/physical/homelab-host.md) — the
  homelab host & worker VM system design: disk shape (nodatacow raw +
  virtio-blk), the second host bridge, two-phase GPU passthrough, the
  host-prep change-set.
- [docs/physical/gateway.md](docs/physical/gateway.md) — the UDM as a
  system: ZeroTier network design (roster, routes, CI-confining flow
  rules, rollout), recovery playbooks, firewall target state (rules
  census + the deferred IoT→LAN tightening).
- [docs/credentials.md](docs/credentials.md) — the credential register:
  every credential's scope, storage slot, rotation; the
  `deploy/credentials/` distribution mechanism; the offline kit.
- [docs/operations.md](docs/operations.md) — day-2 operations: the
  update-ownership matrix, upgrade & node-replacement runbooks, the
  (almost fully automated) drill program, the playbook index.
- [docs/framework/pulumi.md](docs/framework/pulumi.md) — the Pulumi Python framework: `Component`,
  `async_output`/`resolve`, `pulumi.run`, and the decided three-stack
  layering (§3). Start here; §1.4 has cookbook examples.
- [docs/framework/ci.md](docs/framework/ci.md) — state backend (Postgres on an
  OCI micro) and the CI pipeline (per-layer previews/ups, connectivity
  matrix, noop-automerge).
- [docs/framework/rfc-001-native-async-inputs.md](docs/framework/rfc-001-native-async-inputs.md) —
  design rationale and mechanics of the native async inputs framework (Rev 3).
- [docs/framework/testing.md](docs/framework/testing.md) — unit testing Pulumi code with mocks.
- [docs/declarative/README.md](docs/declarative/README.md) — index of the layer-by-layer
  declarative designs (physical, dns, cluster-infra, workloads — all
  written).

## Status

**Built.** The framework (putils RFC-001 Rev 3, `pulumi.run` entrypoint), the
four-stack dispatch, `conventions.py`, the credential scripts
(`credentials`: the offline store, the derivation seed and what it
derives, the B2 seed key), the state-backend appliance's definition and
provisioner
(`state-backend`), the CI workflow set, and renovate. Ruff, `basedpyright`
strict and the tests are clean across everything but the three pre-Talos
leftovers named below.

**Standing in OCI**: the appliance's own VCN, subnet, gateway, security group
and reserved public IP, plus the imported Fedora CoreOS image. The instance
itself is one `state-backend provision` away — the command needs the offline
database, so it runs on the workstation that holds it.

Open items, roughly in order:

1. **State backend**: launch the instance, then regenerate the stack configs
   and passphrase from scratch (`Pulumi.dev.yaml` is a stale kluster-code copy,
   and the history scrub removes it — cluster/security-audit.md L10) and create
   the four stacks.
2. **Repo plumbing**: GitHub rulesets (rebase-merge, up-to-date requirement),
   the per-stack Environments and their secrets, `kluster-ops`' scheduled
   workflows, and the two GitHub Apps' installation wiring.
3. **Physical-layer implementation**: `src/kluster/physical/aws.py` implements
   the abandoned AWS plan and is reference-only; it is replaced by the OCI +
   libvirt + UniFi/gw-config declaration of declarative/physical.md.
4. **Legacy k3s residue**: `kx.py` (SealedSecret, chart pins from old config)
   and `base_cluster/nodes.py` (k3s `svccontroller` labels) predate the
   Talos/Cilium design; they are rewritten alongside the new base cluster, and
   `packages/crds` is regenerated for the new chart set.
5. **DNS absorption**: public records move into pulumi-cloudflare; the zones
   are ported from the DNSControl repo (github.com/Aetf/dns), which then
   retires. Its census is stale in three places (physical/gateway.md §2.1).
6. **Storage residue** (docs/cluster/storage.md): JuiceFS CSI is not installed
   (one quarantined per-app user), Longhorn is deferred with adoption criteria
   on file, and the second homelab worker VM is deferred.

## Working order

The remaining build, in dependency order:

1. **Launch the state-backend instance** (`state-backend provision`), then
   regenerate stack configs and create the four stacks.
2. **Physical stack** per declarative/physical.md, gated by the bootstrap
   verification checklist (§6) — every item verified before any app migrates.
   Includes the homelab host-prep aconfmgr change-set (physical/homelab-host.md
   §4).
3. **The rest of CI**: the ported+upgraded **`images.yml`** (multi-arch via
   native arm64 runners, CNPG images wired in — ci.md §4; **critical path**:
   Wave A's splitpro needs this image, arm64 runners need the repo public, and
   public needs the L10 history scrub first), `packages/crds` regen for the new
   chart set, and the small runbooks as they come up.
4. Then **migration.md** Phase 0 → Waves A–F.

Deliberately *not* pre-decided (settle on first contact, in this
order of appearance): exact Talos/Cilium/chart version pins (renovate
takes over after the first pin; known floors: **Cilium ≥1.20** for the
ExternalAuth route filter, ≥1.16 for tunnel-mode EGW, Longhorn ≥1.12
if ever adopted), AdGuard static-config templating shape inside the
gw-config estate, alertmanager routing details beyond "ported from
legacy".
