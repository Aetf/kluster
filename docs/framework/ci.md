# CI & State Backend

Objective: how the three stacks ([pulumi.md](pulumi.md) §3) are driven —
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
    the ported `deploy/state-backend/` (compose + `init.sh`, reused
    nearly as-is) — the micro instance itself is the one hand-created
    OCI resource, documented here.
-   **Exposure**: public 5432 with TLS + scram-sha-256 (GitHub runners
    have no stable IPs to allowlist; this is how managed Postgres
    products live). Client-cert verification is an available hardening
    step. State secrets remain passphrase-encrypted regardless
    (`PULUMI_CONFIG_PASSPHRASE` in CI secrets / local mise env).
-   **Tenancy co-fate, mitigated**: the backend now shares fate with the
    OCI tenancy (a named risk). Mitigation is the same posture as etcd:
    a **scheduled `pg_dump` to B2** (cron on the micro), restore drilled
    with the rest of nodes.md §5 Tier 0. RPO ≤ 24 h on state is fine —
    state is re-derivable from reality (`pulumi refresh`/import) at
    worst.
-   The legacy homelab Postgres backend keeps serving kluster-code
    untouched until that cluster retires.

## 2. Connectivity per job

| Target | Needed by | Path |
| --- | --- | --- |
| OCI / Cloudflare / B2 APIs | all layers | public |
| kube API, cloud Talos apid | `k8s-base`, `apps` | public (NLB 6443/50000, mTLS) |
| homelab worker's Talos apid | `physical` | via cloud endpoints — talosctl proxies to `--nodes <homelab>` through apid over KubeSpan (**bootstrap verification item**) |
| libvirt + gw-config (UDM SSH) | `physical` only | **per-run ZeroTier join** in the `physical` jobs (ephemeral member, pre-authorized) — no standing runner, no home inbound ports |
| State backend | all layers | public TLS (§1) |

Only the rare `physical` jobs touch ZeroTier; the daily `apps` path is
entirely public-endpoint.

## 3. Pipeline shape

```
PR:    detect-changes ─→ preview-physical ∥ preview-k8s-base ∥ preview-apps
                            └─ all zero-diff → noop-automerge (ported)
merge: up-physical ──needs──→ up-k8s-base ──needs──→ up-apps
```

-   **Merge side runs `up` only.** `pulumi up --yes` performs its own
    preview as phase one; a separate post-merge preview job would be a
    duplicate. The zero-diff "gate" on merge is simply that an up with
    nothing to do is a fast no-op.
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
