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
`docs/framework/` (the Pulumi Python framework itself), `docs/declarative/`
(how each layer is declared in the program — see its README for planned
docs: physical, dns, cluster-infra, workloads).

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
- [docs/cluster/migration.md](docs/cluster/migration.md) — the migration plan:
  standing rules (per-app stop-copy-start + DNS repoint, tracker
  retirement, NVMe interleave), the Phase-0 verification gate, waves
  A–E, data-movement techniques, decommission checklist.
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
  declarative designs (to be written).

## Status & open decisions

Foundation (framework, entrypoint, tests, docs) is settled as of 2026-08-15:
putils RFC-001 Rev 3, `pulumi.run` entrypoint, pulumi 3.257, ruff clean.

Open items to settle before / during detailed design, roughly in order:

1. **State backend — decided 2026-08-22** (docs/framework/ci.md §1):
   `postgres://` DIY backend on an OCI E2.1.Micro (public TLS + scram,
   scheduled pg_dump→B2). To do: stand up the micro + port
   deploy/state-backend, then regenerate stack config + secrets from
   scratch (`Pulumi.dev.yaml` is a stale kluster-code copy) before the
   first real `pulumi up`.
2. **CI — designed 2026-08-22** (docs/framework/ci.md §2–3): PR = parallel
   per-layer previews + noop-automerge; merge = chained per-layer ups
   (no separate post-merge preview); ZT join only in physical jobs. To
   do: implement workflows, port noop-automerge + HA failure push.
3. **Stack layering — decided 2026-08-22** (docs/framework/pulumi.md §3):
   four stacks `physical` / `dns` / `k8s-base` / `apps`; conventions
   shared as code, StackReference carries machine facts only. To do:
   split the entrypoint accordingly.
4. **Physical-layer implementation**: `src/kluster/physical/aws.py`
   implements the abandoned AWS plan (talos 1.8.3 hardcoded, AMI lookup
   unused) and is reference-only; replace with the chosen cloud provider
   + libvirt + UniFi/gw-config providers per architecture.md §5.
5. **Legacy k3s residue**: `kx.py` (SealedSecret, chart pins from old config)
   and `base_cluster/nodes.py` (k3s `svccontroller` labels) predate the
   Talos/Cilium design; rewrite alongside the new base cluster. Regenerate
   `packages/crds` for the new chart set (Cilium, Gateway API, ...) when it
   firms up.
6. **Cloud site — decided 2026-08-22** (docs/cluster/nodes.md §3.1-3.2):
   **OCI, 3× A1.Flex 1 OCPU/8 GB under PAYG as combined CP+ingress
   nodes** ($0 today; ≤~$21/mo if the free-allowance halving reaches
   PAYG), Vultr vhp 4 GB ($24, single node + homelab CP) as the scripted
   fallback. Control plane moves cloud-side (architecture.md §6.5 for
   the reversal rationale). Remaining bootstrap verifications: LB-IPAM
   pool containing node primary IPs; NLB dual-stack + source-preservation
   semantics; etcd fsync on OCI block volumes; A1 capacity at creation;
   Egress Gateway under the chosen routing mode + reserved-IP↔secondary-
   private-IP NAT (the hath dedicated-VIP pattern, architecture.md §3.2).
7. **DNS absorption**: all public DNS records move into pulumi-cloudflare;
   port zones from the DNSControl repo (github.com/Aetf/dns) and retire it
   (architecture.md §5.1).
8. **Storage residue** (docs/cluster/storage.md): B2 bucket decided; JuiceFS CSI
   not installed (one quarantined per-app user: VPS syncthing/dav);
   Longhorn deferred out of the initial build — local-path + VolSync is
   the default, adoption criteria on file (§3.2); second homelab worker
   VM deferred.

## Implementation kickoff (design phase closed 2026-08-22)

The design set is complete and internally consistent; these are the
first-session implementation tasks, roughly in order:

1. **`conventions.py` initial values** — the constants every doc
   references but deliberately left to implementation: pool labels,
   gateway/storage-class/anchor names, DNS zone sets, backup retention
   classes, and the concrete `lan` ULA /64.
2. **Tenancy + backend bootstrap** — OCI PAYG signup (home region is
   permanent), the state-backend micro + ported `deploy/state-backend`
   + pg_dump timer (framework/ci.md §1), then regenerate stack
   configs/passphrase (open item 1) and create the four stacks.
3. **Repo plumbing** — renovate config (the CI design assumes
   renovate-class PRs), GitHub rulesets (rebase-merge, up-to-date
   requirement — mind the kluster-code lessons), noop-automerge
   permissions, CI secrets inventory (passphrase, backend URL, provider
   tokens per cluster-infra.md §1.1, ZT ephemeral-member credential).
4. **Physical stack implementation** per declarative/physical.md
   (replaces `src/kluster/physical/aws.py`), gated by the bootstrap
   verification checklist (physical.md §6) — every item verified before
   any app migrates. Includes the homelab host-prep aconfmgr change-set
   and the ZeroTier Central manual items (physical.md §3/§6).
5. **CI workflows** per framework/ci.md §3, plus the ported+upgraded
   **`images.yml`** (multi-arch via native arm64 runners, CNPG images
   wired in — ci.md §4); **`packages/crds` regen** for the new chart
   set; small runbooks as they come up (sealing-key export/restore,
   orphan audit recipe, talosctl day-2 recipes).
6. Then follow **migration.md** Phase 0 → Waves A–F.

Deliberately *not* pre-decided (settle on first contact, in this
order of appearance): exact Talos/Cilium/chart version pins (renovate
takes over after the first pin; known floors: **Cilium ≥1.20** for the
ExternalAuth route filter, ≥1.16 for tunnel-mode EGW, Longhorn ≥1.12
if ever adopted), AdGuard static-config templating shape inside the
gw-config estate, alertmanager routing details beyond "ported from
legacy".
