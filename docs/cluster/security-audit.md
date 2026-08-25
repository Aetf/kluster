# Security Audit — Next-Generation Cluster Design

An independent security review of the design set (architecture, nodes,
storage, migration, the four declarative-layer docs, framework/CI),
from an attacker's perspective: what a compromised pod, a compromised
worker VM, a leaked credential, or a malicious pull request can reach,
and where the design's own stated guarantees (chiefly Tier-0 "backups
are the HA mechanism") depend on a control that was not yet written
down.

> **Status**: audited 2026-08-23 against the design set as of that
> date. Every accepted finding below is *designed into* the document
> that owns the mechanism (linked per finding); this file is the
> register and rationale, not a second source of truth. Findings are
> the review's own — the underlying architecture decisions
> (cloud-side CP, two-pool LB, backups-as-HA, standing-rent
> discipline) were reviewed and left intact.

## How to read this

Each finding: the attack, why it matters *here* (not in the abstract),
the fix, and where the fix now lives. Severities are relative to this
cluster's own threat model (§4.1 of architecture.md): a single $0-trust
cloud tenancy holding etcd, a home network the cluster is wired into at
L3, and a design that treats off-site backups as the last line.

---

## High severity

### H1 — Pod → OCI metadata → full machine config

**Attack.** Machine config is delivered as `user_data` via the OCI
instance metadata service (physical.md §1); it contains the cluster
PKI and the etcd secretbox key. `169.254.169.254` is reachable from
every pod by default, and OCI's IMDSv2 protection is a *static*
`Authorization` header (no AWS-style hop-limit / session token), so a
pod adding the header reads `user_data` anyway. Any compromised
public-facing pod (hath, qbittorrent Web UI) escalates to cluster
admin with one request.

**Fix.** The k8s-base cluster-wide baseline network policy denies pod
egress to `169.254.0.0/16` — this, not IMDSv2, is the real control;
legacy IMDS (v1) is disabled on the instances as defence in depth.
Verified at bootstrap (pod curl to the endpoint must be denied).

**Lives in.** cluster-infra.md §2 (policy), physical.md §1 (IMDS
disable) + §6 (verification), architecture.md §4.1.

### H2 — Unauthenticated BGP: worker VM can hijack LAN routing

**Attack.** The UDM↔worker BGP session (architecture.md §3.4,
cluster-infra.md §2) had no session authentication and no import
filter. The worker VM runs the entire homelab workload set and is the
design's most exposed node; if compromised — or if any LAN host claims
its static IP while it is down — it can advertise arbitrary prefixes to
the UDM (the AdGuard DNS IPs as /32s, a more-specific of the main LAN)
and MITM the whole home network.

**Fix.** MD5 session password on both ends, plus an inbound
prefix-list on the UDM accepting only `192.168.70.0/24 le 32` and the
ULA /64 `le 128`, with a `maximum-prefix` cap. Verified at bootstrap by
advertising a bogus prefix and confirming rejection.

**Lives in.** cluster-infra.md §2 (BGP), physical.md §6 (verification),
architecture.md §4.1.

### H3 — CI credential blast radius + PR-code execution

**Attack.** CI holds the union of all secrets (OCI, Cloudflare, B2
management, UDM root SSH, ZT, passphrase, kube/talos configs), and a PR
`preview` job **executes the PR branch's Python with provider
credentials** — a malicious PR or a poisoned dependency in `uv.lock`
exfiltrates everything at preview time. `noop-automerge` amplifies it
by merging "zero-diff" dependency bumps unreviewed, after which the
`up` job runs them with full credentials.

**Fix.** Per-stack GitHub Environments (dns sees only Cloudflare; apps
never holds the UDM key / OCI admin creds; physical credentials are
main-only, split across two environments — ungated `physical-plan` for
the zero-diff plan job, reviewer-gated `physical` for applies (ci.md
§3, amended 2026-08-24) — the one approval door kept, because a layer
that can root the gateway is not the frictionless-apps layer). PRs get
no physical preview at all. Previews run only for same-repo branches
(`pull_request`, never `pull_request_target`; fork PRs get no
secrets). noop-automerge scoped to renovate lockfile/pin PRs; secret
scanning + push protection on. **Residual, accepted 2026-08-24**:
merged main code — noop-automerged dependency bumps included —
executes with physical credentials in the ungated plan job; the gate
guards *apply*, not execution. Chosen over a gated weekly drift
preview for zero-click renovate cadence; a bump that actually changes
physical rendering still surfaces as a plan diff and stalls at the
gate.

**Lives in.** ci.md §2 (ZT confinement), §3 (partitioning + preview
boundary).

### H4 — Backups are deletable (no anti-ransomware / anti-fat-finger)

**Attack.** storage.md §5 makes backups the sole persistence guarantee,
but restic/barman need list+delete on their repos to prune. A single
shared B2 key (or a compromised CI job) can therefore erase every
backup — after which Tier-0 "declarative rebuild + backups" has no
second half.

**Fix.** Bucket keeps prior file versions ≥30 days (hide-then-delete
lifecycle) so deletes are recoverable; per-consumer prefix-scoped keys
**without `deleteFiles`** (an app reaches only its own
`volsync/<ns>/…`, and its prune's deletes degrade to lifecycle-purged
hides — retention semantics survive, destruction doesn't); no
delete-capable key in any automation, the master credential offline
only, account 2FA on. Verified: a scoped key cannot touch a foreign
prefix.

**Lives in.** storage.md §4 (integrity rules), physical.md §6
(verification), architecture.md §4.1.

---

## Medium severity

### M1 — Public Postgres state backend: authentication and OS

**Attack.** ci.md §1 exposes 5432 publicly with TLS + scram on an
unattended 1 GB micro whose database holds `machine_secrets` behind one
passphrase. Password auth on the open internet is a standing
brute-force + Postgres-CVE surface, and the OS was left unspecified.

**Fix.** Client-certificate verification made **mandatory** (not
"available hardening") — *amended 2026-08-24*: the NSG-allowlist half
of the fix was dropped as unimplementable (`api.github.com/meta` lists
thousands of CIDRs against an NSG rule quota in the hundreds, and a
home-/32-only rule would break CI), so client-cert mTLS is
deliberately the only wall (state-backend.md §4); the `pg_dump` is
age-encrypted before upload (it
carries every stack's ciphertext + salt). **OS: an immutable,
auto-updating container OS fully provisioned at create time** (Fedora
CoreOS preferred — Ignition + podman quadlets; the box is a
zero-maintenance appliance, re-provisioned from config, carrying no
state pg_dump+refresh can't rebuild). **OCI Container Instances were
evaluated and rejected**: no persistent storage (15 GB ephemeral only)
and they bill from the same tenancy A1 pool the cluster nodes already
budget, whereas the E2.1.Micro is separately Always Free.

**Lives in.** ci.md §1.

### M2 — `lan` pool wide open to the IoT VLAN

**Attack.** architecture.md §3.4's own measurement: all VLANs share the
LAN zone, LAN→LAN is an unconditional ACCEPT, and pool traffic falls
through the equally-ACCEPT LAN→WAN chain. So every IoT device (cameras,
no-name plugs — the LAN's most-compromised class) can reach the admin
UIs behind `lan-gw` (immich, qbittorrent, grafana).

**Fix.** Ship IoT-VLAN → `192.168.70.0/24` (+ ULA /64) **default drop
with one enumerated allow** (the `media-gw` VIP:443) with the cluster
rather than waiting for a future zone tightening. *Amended
2026-08-24*: the original "recorded cross-VLAN dependencies all
originate cluster→IoT" claim was wrong — smart TVs/streamers →
jellyfin is IoT-originated; the media-gw carve-out serves it without
reopening the pool (physical/gateway.md §4.2).

**Lives in.** architecture.md §3.4, declared via the unifi provider
(physical.md §4); full firewall target state in
physical/gateway.md §4.

### M3 — No node-local firewall beneath the derived OCI rules

**Attack.** The cloud nodes' primary IPs are public VIPs; the host
netstack runs apid, kubelet and KubeSpan. OCI security rules are "derived,
not enumerated" (physical.md §1) — a mis-derived service rule silently
widens exposure with no second layer.

**Fix.** A Talos `NetworkRuleConfig` ingress firewall (default-deny,
platform ports enumerated) in machine config, plus explicit
kube-apiserver `anonymous-auth=false` and audit logging on the public
6443. Zero runtime cost; pure machine config. *Amended 2026-08-24*:
the enumeration covers **host-netns-terminated ports only** — Service
VIP traffic is answered by the BPF datapath ahead of nftables
(verified at bootstrap), so per-service ports never enter machine
config and the co-location principle survives (physical.md §2).

**Lives in.** physical.md §2, §6 (verification), architecture.md §4.1.

### M4 — hath (closed-source binary) co-located with etcd

**Attack.** hath moved from a dedicated worker (legacy) onto the
combined CP+ingress nodes — a closed-source Java client serving public
traffic and taking H@H network commands, one kernel away from etcd. The
threat model discussed "node compromised" but not "which workload most
likely causes it".

**Fix.** Not an architecture reversal (combined roles are the cost
basis) but defence in depth: restricted-PSS default for app namespaces,
strict limits, per-app NetworkPolicy, and the co-location stated as an
accepted residual risk.

**Lives in.** workloads.md §1 (PSS), architecture.md §4.1 (residual
risk).

### M5 — Legacy sealing key never rotated

**Attack.** migration.md restores the legacy sealed-secrets key to
reuse old ciphertext — correct for continuity, but that key exists in
years of backups. sealed-secrets' automatic rotation only affects *new*
seals; the old key decrypts future secrets forever, so any old backup
copy becomes a skeleton key.

**Fix.** Wave F re-seals everything against a fresh key and deletes the
legacy key; "legacy sealing key retired" is a decommission success
criterion.

**Lives in.** migration.md §0.5, §4 (Wave F).

---

## Low severity / best practice

### L1 — ExternalAuth is a single point for all gated apps

Every `auth=True` app (qbittorrent Web UI included — its "run external
program" setting turns a fail-open into public RCE) rides one Cilium
ExternalAuth filter with a known fail-open history (cilium#47178). The
fix is to **harden and monitor the mechanism, not to add per-app
fallback auth** (N app configs defending one mechanism is the wrong
layer): bootstrap fail-closed verification *plus* a standing auth
canary — a synthetic unauthenticated probe with a vmalert rule firing
on anything but 401/302 — so a Cilium upgrade regressing to fail-open
pages instead of silently exposing every gated app, and Cilium bumps
merge only with the canary green. *Lives in* cluster-infra.md §2.

### L2 — CT logs leak the internal service census

Per-name DNS-01 issuance publishes every rewrite-only LAN service into
Certificate Transparency, undoing the NXDOMAIN hiding. Fix: per-zone
**wildcard** certificates. *Lives in* dns.md §4.

### L3 — CAA + DNSSEC

With all DNS in Pulumi and issuance entirely DNS-01, add per-zone CAA
(pin Let's Encrypt) and enable DNSSEC — cheap misissuance defence.
*Lives in* dns.md §1.

### L4 — gw-config SSH host-key pinning

The provider does root-level writes to the UDM over ZeroTier; an
accept-new first contact would let any ZT member MITM into gateway
root. Pin the host key in provider config. *Lives in* physical.md §4.

### L5 — Confine CI ZeroTier members by tag

A leaked CI join credential otherwise joins the home network with
unpoliced forwarding (zt* rides the UDM's default ACCEPT). ZT Central
tag-based flow rules limit CI members to UDM SSH, the UDM's UniFi
Network API, the AdGuard APIs, and libvirt SSH. *Lives in* architecture.md §5.3, ci.md §2.

### L6 — libvirt SSH identity is root-equivalent

physical/homelab-host.md §4's "libvirt group, no root" is effectively root — domain
XML can map any host device/disk. Not a design change; the identity is
guarded at the UDM-key tier, stated so it isn't treated as an
unprivileged account. *Lives in* physical/homelab-host.md §4.

### L7 — haos.ucw ingress change needs HA `trusted_proxies`

The X-Forwarded-For source changes from the old VPS to the gateway pod
CIDR; without updating HA's trusted proxies, client IPs (and HA's
login rate-limiting / ip_ban) get judged against the proxy — the same
failure class as the historical UniFi login lockout. *Lives in*
workloads.md §4.

### L8 — Seed ISO / machine-secret files on the homelab host

The libvirt nocloud seed carries the worker's machine secrets. Root-only
permissions, excluded from every host snapshot/backup scope (same
subvolume discipline as the VM image). *Lives in* declarative/physical.md §3.

### L9 — KubeSpan discovery is an external dependency

Peer discovery uses the public Sidero discovery service (affiliate data
end-to-end encrypted; it sees endpoints/metadata only). Recorded as a
known external availability dependency; self-hosting fails the
standing-rent test. *Lives in* architecture.md §2.1.

### L10 — Repo history hygiene before going public

**Closed 2026-08-25**: history scrubbed, repository public.

Free arm64 CI runners and the whole CI security model (framework/
github.md §2) require a public repository, and the history carried
kluster-code-era stack ciphertext — every secret-bearing blob was a
version of `Pulumi.dev.yaml`: three blobs across three commits (the
initial commit, the salt rotation, the AWS setup), carrying the
encryption salt and seven `secure:` ciphertexts. A scan of all
historical blobs for private keys, cloud access keys and provider
tokens found nothing else. The file was a stale kluster-code copy due
to be regenerated from scratch regardless, so the fix was a removal
rather than a rewrite:

```
git filter-repo --invert-paths --path Pulumi.dev.yaml
```

**Amended 2026-08-25**: this must not be read as "an encryption salt
may never be public". `Pulumi.<stack>.yaml` files, salt and `secure:`
ciphertexts included, are committed to this public repository on
purpose — CI reads them, and the passphrase they are derived against is
32 bytes of HKDF output from the derivation seed (credentials.md §2.2),
which no offline attack on a KDF salt reaches. What made the scrubbed
blobs a finding was whose secrets they were: the legacy cluster's, under
a passphrase that was not derived and is live until Wave F.

Post-conditions, verified on the rewritten history: no blob matches the
ciphertext or salt markers, every other path keeps its exact blob hash,
and the commit count is unchanged. Because the rewrite changed every
commit id, a clone predating it is not fast-forwardable — re-clone
rather than pull. *Lives in* ci.md §4.

### L11 — AdGuard credential in the `apps` CI environment is LAN-DNS control

The split-horizon rewrites make `apps` jobs carry the AdGuard admin
credential (AdGuard has no scoped API), so the frictionless apps tier
can rewrite any LAN name — LAN-wide DNS hijack from the lowest-tier
credential set. Accepted residual: that environment already holds
cluster-admin kubeconfig (H3's accepted core), so this adds breadth,
not depth, and the ZT flow rules still confine where the credential is
usable from. Stated so the apps tier is never mistaken for harmless.
*Lives in* ci.md §2.

---

## Not changed (reviewed, left as designed)

-   **Cloud-side control plane / single $0-trust tenancy** — the
    residual risks (etcd in an untrusted tenancy, tenancy-loss) are
    already carried consciously with the right mitigations (etcd
    encryption at rest, hourly snapshots off-provider, the cold-standby
    drill). H1/H4 harden the *mechanism*, not the placement decision.
-   **Combined CP+ingress+worker nodes** — the cost basis of the
    three-node pool; M4 contains the workload risk rather than
    unbundling the roles.
-   **KubeSpan-only, no home inbound** — the reduced attack surface is a
    security *win*; nothing to add.
-   **protect=True discipline, derived-not-enumerated rules,
    standing-rent rule** — these are the traits that made the findings
    above cheap to fix (one policy, one filter, one lifecycle setting);
    left intact and leaned on.
