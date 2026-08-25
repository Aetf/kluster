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
**anchors** (§2), a reusable per-zone mail component
(MX/SPF/DKIM/DMARC), and — per zone — **CAA records (issuance pinned
to Let's Encrypt) and DNSSEC enablement** (2026-08-23: the entire cert
story is DNS-01, which makes both cheap and worth having). Verified 2026-08-22: no ready-made Pulumi
SPF/DMARC tooling exists — the ecosystem offers only raw record
resources and officially recommends wrapping your own component — and
none is needed: the current SPF is a single include (no flattening) and
DMARC a static TXT. One implementation gotcha on record: quote TXT
values so SPF strings don't get split on spaces.

## 2. Naming hierarchy (formalizing the existing conventions)

-   **`*.hosts.<zone>` is the anchor namespace** — already the live
    convention (`archvps.hosts`). **IP literals exist only here**, with
    low TTLs. New estate: `kluster.hosts` → the NLB (A/AAAA, from
    physical outputs) and `vip1.hosts` → the dedicated VIP (operator
    convenience; hath itself needs no DNS). Retired at absorption:
    `archvps.hosts` (with the legacy VPS) and `abacus.hosts` (machine
    no longer exists — its dependents in the current file, the Abacus
    ZT entry and the jupyter/mc records, are dead weight to drop during
    the import census). The state-backend micro deliberately gets **no
    anchor**: clients pin its IP (`verify-full`, SAN = IP literal) so
    the state backend's hot path never depends on this stack
    (physical/state-backend.md §3).
-   **`*.zt.<zone>`** — the ZeroTier host block, unchanged as a
    convention (private IPs in public DNS, deliberate and existing
    practice); its contents mirror the ZT member roster
    (physical/gateway.md §2.1): `udm.zt` added, `abacus.zt` dropped at
    import, the VPS record retires in Wave F.
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
-   **Owned by this stack, not by `apps`.** Split-horizon is DNS, and
    the AdGuard pair is on the UDM: putting the rewrites here keeps the
    LAN reachability requirement — the ZeroTier join, and its
    availability as a dependency of every CI run that touches the stack
    — off the busiest stack in the repo and on the quietest one. `apps`
    then needs no LAN access at all.
-   **adguardhome-sync retires.** With Pulumi dual-writing the dynamic
    config, the sync service is redundant *and* a conflict source (it
    would overwrite bob's Pulumi-written rewrites). Consequence,
    accepted: static-config parity (filters, upstreams) between the two
    instances is now the gw-config estate's job — both instances'
    configs are declared there, and one standing service leaves the
    homelab host.
-   **Placement**: rewrites are emitted automatically for any app
    with a LAN-side gateway attachment — split-horizon (both
    gateways), LAN-only (`lan-gw`), or IoT-reachable (`media-gw`,
    rewrite targeting the media VIP) — an app cannot forget its
    rewrite because it never writes it by hand. Crossing a stack
    boundary does not weaken that: an app's route declaration is
    **plain data** in a module both stacks import, so `apps` builds
    its HTTPRoutes from it and `dns` builds the rewrites from the same
    rows. One edit, two stack diffs, both previewable — rather than
    one edit and a second stack to remember. LAN ULA AAAAs are
    emitted alongside (RFC 6724 caveat noted, architecture.md §1.3).

## 4. LAN DNS: three name planes

LAN-side naming is absorbed into the same design, split by what each
plane names:

1.  **Device plane — `home.arpa` (main VLAN) and `iot.home.arpa` (IoT
    VLAN)**: device hostnames, DHCP-derived and served by the UDM's
    resolver as today. Inherently dynamic — not Pulumi-managed; any
    *static* host entries that prove necessary go through the unifi
    provider.
2.  **Service plane — public-zone names, resolved via AdGuard**:
    every LAN-reachable service uses its *public* hostname
    (`<app>.<zone>`); AdGuard rewrites (§3) steer LAN/ZT clients to the
    `lan` VIP. **LAN-only services are rewrite-only**: the helper emits
    the AdGuard rewrite and the `lan-gw` route but *no* Cloudflare
    record — public resolvers see NXDOMAIN, while cert-manager DNS-01
    still issues real certificates for the name (challenge records
    don't require the name itself to be published). Issuance for
    these names is **per-zone wildcard certificates**, not per-name:
    every issued certificate lands in public Certificate Transparency
    logs, and per-name issuance would republish exactly the LAN-only
    service census that rewrite-only just hid.
3.  **`lan.ucw.phd` retires.** Its two historical roles are both
    superseded: split-horizon duplicates collapse into the rewrites on
    the public names, and LAN-only names become rewrite-only names in
    the public zones (with proper TLS, which `lan.` names never had
    cleanly). The zone's entries are dropped one-by-one as each app
    migrates, like the archvps repointing.
4.  **Gateway-local TLS stays gateway-issued** (decided 2026-08-23).
    The UDM caddy's vhosts (UniFi console, AdGuard UIs) follow the
    same naming move — their `lan.ucw.phd` names become rewrite-only
    names in the public zones — but caddy **keeps issuing its own
    certificates** (DNS-01 with its own zone-scoped Cloudflare token,
    a gw-config device secret, cluster-infra.md §1.1) rather than
    consuming cert-manager's: the gateway's TLS must keep renewing
    when the cluster is down or mid-rebuild, and pushing certs from
    the cluster to the device would invert that survival dependency.
    Same wildcard discipline as item 2 (CT hygiene).

## 5. The one-line helpers

`public_route(host=…, zones=…, proxied=…)` on the app component base
emits the coherent set: HTTPRoute (to `internet-gw`, `lan-gw`, or both
per the §3.6 matrix), the CNAMEs across the zone set, and — for
both-gateway apps — the AdGuard rewrites. `lan_route(host=…)` is the
LAN-only variant (rewrite + `lan-gw` route, no public record, §4);
its `iot_reachable=True` parameter attaches `media-gw` instead and
points the rewrite at the media VIP — the review-visible form of
"IoT devices may reach this app" (cluster-infra.md §2,
physical/gateway.md §4.2).
`public_port(…)` is the raw TCP/UDP analog, additionally emitting the
NLB listener and its security rule (physical.md §1's
derived-not-enumerated principle).

## 6. Migration shape

Per-app cutover falls out of the anchor design: an app's records point
at `archvps.hosts` until the app migrates, then its component declares
the same names against `kluster.hosts` (and the DNSControl entry is
deleted). Estate records import wholesale into the `dns` stack early —
note the handful of **estate records that themselves reference the VPS**
(the apex/`www` A records of unlimited-code.works, unlimitedcodeworks.xyz
and jiahui.love): they repoint to `kluster.hosts` when their serving
apps move, and none may still reference `archvps.hosts` when the VPS
retires (migration.md Wave F checks this). The DNSControl repo retires
with a pointer commit (the old-tracker rule, migration.md §0).
