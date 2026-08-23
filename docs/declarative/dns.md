# Declarative Design: DNS

How every DNS record — public and split-horizon — is declared. There is
no DNS stack and no in-cluster DNS controller (architecture.md §6.4):
records live **beside the resource they name**, in whichever stack that
is, and the standalone DNSControl repo
([Aetf/dns](https://github.com/Aetf/dns)) is absorbed and retired.

> **Status**: designed 2026-08-22. Not implemented.

## 1. Public DNS (pulumi-cloudflare)

-   **Zones + anchors in `physical`** (physical.md §5): the zone
    resources and one stable **anchor name per entry path** — the NLB's
    A/AAAA (e.g. `ingress.<zone>`) and, for dedicated-VIP workloads,
    the reserved IP's A record. Anchors are the only records that hold
    IP literals; they carry low TTLs.
-   **Per-app records in `apps`**: a public hostname is a **CNAME to
    the anchor**, declared inside the app's component. A cloud-node
    rebuild or VIP re-home therefore touches exactly one record (the
    anchor), previewed in `physical`.
-   **Absorbing DNSControl**: existing records are imported
    (`pulumi import`) or re-declared zone by zone during migration;
    records naming legacy-cluster endpoints migrate with their apps.
    The DNSControl repo retires with a pointer commit per the
    old-tracker rule (migration.md §0).

## 2. Split-horizon (AdGuard on the UDM)

LAN/ZT clients must resolve split-horizon apps to their `lan` VIPs,
never the cloud path (architecture.md §3.4). The AdGuard instances
(alice/bob) live on the UDM; alice is the sync source of truth.

-   **Mechanism**: a small dynamic-provider resource wrapping the
    AdGuard rewrite API against alice (the legacy golinks work
    established the API technique); bob receives it via
    adguardhome-sync. Idempotent diff/apply like the gw-config
    provider.
-   **Placement**: the rewrite is declared **beside the app**, emitted
    automatically for any app that attaches to both gateways — a
    split-horizon app cannot forget its rewrite because it never writes
    it by hand.

## 3. The one-line helper

As code health allows (physical.md §5), the framework provides
`public_route(host=...)`-style helpers on the app component base so a
single declaration emits the coherent set: HTTPRoute (to `internet-gw`,
`lan-gw`, or both per the §3.6 matrix), the CNAME to the right anchor,
and — for both-gateway apps — the AdGuard rewrite. Raw-TCP/UDP apps get
the analogous `public_port(...)` emitting the LB Service annotation
set, the NLB listener, and its security rule (the derived-not-enumerated
principle, physical.md §1).

## 4. Non-goals

-   No external-dns (architecture.md §6.4) — records are previewable
    Pulumi diffs like everything else.
-   No DNS for hath (its protocol is IP-based; the dedicated-VIP anchor
    record exists for operator convenience only).
-   LAN ULA AAAA records: emitted alongside the v4 rewrites, with the
    known RFC 6724 caveat (clients mostly still pick v4;
    architecture.md §1.3).
