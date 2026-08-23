# Declarative Design: the `dns` Stack + Co-located Records

How every DNS record — public and split-horizon — is declared. DNS
splits along ownership: a small **`dns` stack** owns the zones and the
estate records that belong to no app, while **per-app records stay
beside their apps** (the co-location principle). There is no in-cluster
DNS controller (architecture.md §6.4); the standalone DNSControl repo
([Aetf/dns](https://github.com/Aetf/dns)) is absorbed and retired.

> **Status**: designed 2026-08-22, after a survey of the live
> dnsconfig.js (6 zones; roughly half the records are not
> cluster-related). Not implemented.

## 1. Why a fourth stack

The DNSControl census: ZeroTier host records, Google Workspace mail
(MX/SPF/DKIM/DMARC per zone), site verifications, external hosts
(abacus), two family zones (jiahui.id / jiahui.love), and two alias
zones (peifeng.phd, ucw.phd) mirroring the primary
(unlimited-code.works; unlimitedcodeworks.xyz partially). None of that
has an app to co-locate with — so it gets its own stack with its own
(rare) change cadence, and non-cluster DNS edits never touch a cluster
stack. Ordering: `physical → dns ∥ k8s-base → apps`; `apps` references
`dns` for zone IDs and `dns` references `physical` for anchor IPs.

The `dns` stack owns: zone resources, the estate records above, the
**anchors** (§2), and the reusable per-zone mail component (the current
SPF is a single include and DMARC a static TXT — plain records, no need
to re-create dnscontrol's builders).

## 2. Naming hierarchy (formalizing the existing conventions)

-   **`*.hosts.<zone>` is the anchor namespace** — already the live
    convention (`archvps.hosts`). **IP literals exist only here**, with
    low TTLs. New estate: `kluster.hosts` → the NLB (A/AAAA, from
    physical outputs), `vip1.hosts` → the dedicated VIP (operator
    convenience; hath itself needs no DNS), `abacus.hosts` unchanged;
    `archvps.hosts` retires with the legacy VPS.
-   **`*.zt.<zone>`** — the ZeroTier host block, unchanged (private IPs
    in public DNS, deliberate and existing practice).
-   **Apps are CNAMEs to anchors**: `<app>.<zone>` → `kluster.hosts.…`
    declared inside the app component. A node rebuild or VIP re-home
    touches exactly one anchor record, previewed in `dns`.
-   **Alias zones via zone sets, not copy-paste**: the mirrors
    (peifeng.phd, ucw.phd, …) are today maintained as duplicated record
    blocks. `conventions.py` defines zone sets (e.g. `PUBLIC_ALL`,
    `PRIMARY_ONLY`); an app declares `public_route(host=…,
    zones=PUBLIC_ALL)` once and the helper fans records out across the
    set.
-   **Cloudflare proxy is a helper parameter** (default on): the
    existing per-record reasons — large uploads (photos), non-HTTP
    ports (syncthing, matrix, minecraft) — become explicit
    `proxied=False` arguments instead of lore in comments.

## 3. Split-horizon (AdGuard, both instances, no sync)

LAN/ZT clients must resolve split-horizon apps to their `lan` VIPs,
never the cloud path (architecture.md §3.4). The AdGuard pair
(alice/bob) lives on the UDM.

-   **Mechanism**: a dynamic-provider resource wrapping the AdGuard
    rewrite API (the legacy golinks work established the technique),
    applied **directly to both instances** — idempotent diff/apply.
-   **adguardhome-sync retires.** With Pulumi dual-writing the dynamic
    config, the sync service is redundant *and* a conflict source (it
    would overwrite bob's Pulumi-written rewrites). Consequence,
    accepted: static-config parity (filters, upstreams) between the two
    instances is now the gw-config estate's job — both instances'
    configs are declared there, and one standing service leaves the
    homelab host.
-   **Placement**: rewrites are emitted automatically for any app
    attaching to both gateways — a split-horizon app cannot forget its
    rewrite because it never writes it by hand. LAN ULA AAAAs are
    emitted alongside (RFC 6724 caveat noted, architecture.md §1.3).

## 4. The one-line helpers

`public_route(host=…, zones=…, proxied=…)` on the app component base
emits the coherent set: HTTPRoute (to `internet-gw`, `lan-gw`, or both
per the §3.6 matrix), the CNAMEs across the zone set, and — for
both-gateway apps — the AdGuard rewrites. `public_port(…)` is the raw
TCP/UDP analog, additionally emitting the NLB listener and its security
rule (physical.md §1's derived-not-enumerated principle).

## 5. Migration shape

Per-app cutover falls out of the anchor design: an app's records point
at `archvps.hosts` until the app migrates, then its component declares
the same names against `kluster.hosts` (and the DNSControl entry is
deleted). Estate records import wholesale into the `dns` stack early;
the DNSControl repo retires with a pointer commit (the old-tracker
rule, migration.md §0).
