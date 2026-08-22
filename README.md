# kluster-py

Pulumi Python code for the next-generation hybrid Kubernetes cluster: Talos
Linux spanning a GCP VPC and the Homelab LAN, with Cilium networking. This
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
  architecture (cloud + Homelab, Talos, KubeSpan, Cilium; two-pool
  LoadBalancer ingress model), including alternatives considered.
- [docs/cluster/nodes.md](docs/cluster/nodes.md) — node & provider selection: measured
  egress economics, cloud worker pricing (reopened 2026-08-22 after the
  Hetzner US price hike; Vultr recommended), homelab host inventory & VM
  sizing, HA tiers.
- [docs/cluster/storage.md](docs/cluster/storage.md) — storage classes (local-path, Longhorn,
  NAS, object storage), backup architecture, JuiceFS root causes &
  containment policy.
- [docs/cluster/migration.md](docs/cluster/migration.md) — workload/data migration plan from
  the legacy cluster.
- [docs/framework/pulumi.md](docs/framework/pulumi.md) — the Pulumi Python framework: `Component`,
  `async_output`/`resolve`, `pulumi.run`, stack layering proposal. Start
  here; §1.4 has cookbook examples.
- [docs/framework/rfc-001-native-async-inputs.md](docs/framework/rfc-001-native-async-inputs.md) —
  design rationale and mechanics of the native async inputs framework (Rev 3).
- [docs/framework/testing.md](docs/framework/testing.md) — unit testing Pulumi code with mocks.
- [docs/declarative/README.md](docs/declarative/README.md) — index of the layer-by-layer
  declarative designs (to be written).

## Status & open decisions

Foundation (framework, entrypoint, tests, docs) is settled as of 2026-08-15:
putils RFC-001 Rev 3, `pulumi.run` entrypoint, pulumi 3.257, ruff clean.

Open items to settle before / during detailed design, roughly in order:

1. **State backend & secrets**: `Pulumi.dev.yaml` is a stale copy from
   kluster-code (`kluster-code:*` config keys are inert here, salt inherited);
   no stack exists in the local file backend. Decide backend (reuse
   kluster-code's Postgres DIY backend?) and regenerate stack config + secrets
   from scratch before the first real `pulumi up`.
2. **CI**: none yet. Port kluster-code's PR → preview → merge-deploys pipeline
   early, before workloads accumulate.
3. **Stack layering**: docs/framework/pulumi.md §3 proposes infra-homelab / infra-cloud /
   k8s-base / applications; the repo is currently a single stack. Decide
   before building the infra layer.
4. **Physical-layer implementation**: `src/kluster/physical/aws.py`
   implements the abandoned AWS plan (talos 1.8.3 hardcoded, AMI lookup
   unused) and is reference-only; replace with the chosen cloud provider
   + libvirt + UniFi/gw-config providers per architecture.md §5.
5. **Legacy k3s residue**: `kx.py` (SealedSecret, chart pins from old config)
   and `base_cluster/nodes.py` (k3s `svccontroller` labels) predate the
   Talos/Cilium design; rewrite alongside the new base cluster. Regenerate
   `packages/crds` for the new chart set (Cilium, Gateway API, ...) when it
   firms up.
6. **Cloud provider — reopened 2026-08-22** (docs/cluster/nodes.md §3.1,
   architecture.md §6.3): the 2026-08-21 Hetzner pick was voided by
   Hetzner's 2026-06-15 US price hike (CPX21 $14 → $38). Current
   recommendation: **Vultr 4 GB (~$24, bundled TBs cover public +
   KubeSpan traffic, native Talos ISO)**, optional Oracle A1 free node as
   a later experiment. Awaiting user's final call.
7. **DNS absorption**: all public DNS records move into pulumi-cloudflare;
   port zones from the DNSControl repo (github.com/Aetf/dns) and retire it
   (architecture.md §5.1).
8. **Storage residue** (docs/cluster/storage.md): B2 bucket decided; JuiceFS CSI
   not installed; second homelab worker VM deferred (Longhorn starts all
   replica=1 + backups). Remaining check: Longhorn ≥1.12 (dual-stack
   support) must be GA by bootstrap time.
