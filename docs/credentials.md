# Credential Register

The single place that describes every credential the system runs on:
where it is born, what it may touch, where it is stored, who consumes
it, and when it rotates. It sits at the docs root because credentials
cross every layer boundary (cluster / physical / declarative /
framework); the owning design doc holds each credential's *why*, this
register holds the *inventory*. Values never appear here — only the
facts about them.

## 1. Rules

1.  **One credential, one row — same change.** Introducing a
    credential and adding its register row is a single change (the
    alert/playbook rule's sibling). A credential without a row is a
    bug.
2.  **Fine-grained scope, stated per row.** Every credential is
    scoped to the minimum its consumer needs; when the platform
    cannot scope that far, the row records the excess it carries
    (pattern: the dispatch PAT's contents:write, architecture.md
    §4.3).
3.  **Storage channels are a closed set.** Offline store · Pulumi
    config secret (provider-credential channel, cluster-infra.md
    §1.1) · SealedSecret (in-cluster consumption) · CI environment
    secret · alerts-repo Actions secret · on-box (delivered by
    provisioning, e.g. Butane-embedded). A row names its channel(s);
    a credential living anywhere else is misplaced.
4.  **Every key rotates or expires.** Each row carries a cadence or
    expiry and the reminder mechanism (platform e-mail, the ci.md §3
    expiry probe, or the annual drill); a retired key gets an
    earliest-destroy date (the age-key precedent, state-backend.md
    §5, generalized).
5.  **Provisioning is scripted.** Minting and distributing a
    credential is an executable procedure (the state-backend
    everything-is-a-script rule, generalized); each row references
    it once implementations land.
6.  **Boundary**: per-app secrets (OIDC clients, app API keys)
    follow the two-channel rule (cluster-infra.md §1.1) and are
    enumerated by the program itself — they are out of register
    scope. The register tracks infra and cross-system credentials.

## 2. Offline tier (never-in-automation)

Held only in the operator's offline store; automation never sees
them. Compromise response and destroy discipline live with the owning
doc.

| Credential | Purpose | Rotation / destroy |
| --- | --- | --- |
| State-backend CA private key | Signs server/client certs (state-backend.md §3) | ~10 y; compromise = regenerate + reissue all |
| age backup private keys (gen N, N−1) | Decrypt state-backend pg_dumps | Yearly generation; destroy retired key at rotation + 30 d |
| Pulumi state passphrase (escrow copy) | Recover state secrets if CI copy is lost | Rotate on compromise; working copy is a CI secret (§3) |
| Account roots: OCI tenancy, GitHub, Cloudflare, Backblaze, ZeroTier Central | Console/owner access + MFA recovery | Per-platform hygiene; MFA recovery codes stored alongside |

## 3. Automation tier

| Credential | Scope | Lives in | Consumer | Rotation |
| --- | --- | --- | --- | --- |
| OCI API key | physical-stack user/compartment | Pulumi config secret + CI env | `physical` | Yearly; scripted re-mint |
| Cloudflare token (zones) | DNS edit, estate zones only | Pulumi config secret + CI env | `dns`, `apps` (CNAMEs) | Yearly |
| Cloudflare token (DNS-01) | `_acme-challenge` edit only | SealedSecret | cert-manager | Yearly |
| B2 key (management) | Bucket/key/lifecycle admin | Pulumi config secret + CI env | `physical` | Yearly |
| B2 keys (writers) | Prefix-scoped, write-only (audit H4): VolSync, CNPG barman, etcd snapshots, micro pg_dump | SealedSecret (in-cluster) · CI env (etcd) · on-box (micro) | restic/barman, ci.md §3, micro cron | Yearly; scripted |
| UDM SSH key | gw-config push (host key pinned) | Pulumi config secret + CI env | `physical` | Yearly |
| UniFi API key | Dedicated local admin, Network API | Pulumi config secret + CI env | `physical` | Yearly |
| ZeroTier Central API token | The one network | Pulumi config secret + CI env | `physical` | Yearly |
| ZT CI member identity | Confined by flow rules (gateway.md §2.3) | CI env (generated in-state) | CI per-run join | With flow-rule changes or yearly |
| GitHub dispatch PAT | `kluster-alerts` contents:write (excess: can push there — accepted, architecture.md §4.3) | CI env | Alert producer step | GitHub expiry + reminder e-mail |
| HA webhook URL/ID | One notify endpoint | SealedSecret + CI env | alertmanager, producer step | On exposure; low value alone |
| Alertmanager read token | Read-only alert list at the gateway route | alerts-repo Actions secret | Issue-sync poller | Yearly |
| State-backend client certs (`ci`, `operator`) | postgres:// mTLS | CI env · operator machine | Pulumi state access | 2–3 y; ci.md §3 expiry probe |
| Pulumi state passphrase | Decrypts state secrets | CI env (all stacks) | every `pulumi` run | Rotate on compromise; offline escrow (§2) |
| Talos machine secrets + talosconfig | Cluster PKI roots | Pulumi state (physical) · CI env (talosconfig for etcd snapshots) | Talos ops, ci.md §3 | Cluster lifetime; regenerate = rebuild |
| kubeconfig | cluster-admin | physical output → CI env | `k8s-base`, `apps` | With cluster CA |
| sealed-secrets sealing key | Decrypts every SealedSecret | In-cluster + offline export (playbook) | controller | Restored from legacy → rotated at Wave F, then on compromise |
| libvirt SSH identity | Homelab host, virsh only | Pulumi config secret + CI env | `physical` (worker VM) | Yearly |
| AdGuard API credentials | alice/bob rewrite API | Pulumi config secret + CI env | `apps` rewrites | Yearly |
| restic repo passwords | Per-PVC VolSync repos | SealedSecret (via `backed_pvc` helper) | VolSync | Stable; loss = repo loss, escrow with backups design |

## 4. Provisioning & audit

-   **Bootstrap order awareness**: rows above marked "Pulumi config
    secret" exist before their stack's first `up` (minted by their
    platform's console/CLI — the scripted procedures); SealedSecrets
    exist only after k8s-base restores the sealing key
    (migration.md).
-   **The quarterly drill audits the register**: every row checked
    for expiry-vs-reminder coverage, retired keys past their
    earliest-destroy date destroyed, orphan credentials (a platform
    key with no row) revoked. This is the register's freshness
    mechanism — a document nobody re-reads is how key sprawl
    happened last time.
