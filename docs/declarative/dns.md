# Declarative Design: the `dns` Stack + Co-located Records

How every DNS record — public and split-horizon — is declared. DNS
splits along ownership: a small **`dns` stack** owns the zones and the
estate records that belong to no app, while **per-app records stay
beside their apps** (the co-location principle). There is no in-cluster
DNS controller (architecture.md §6.4); the standalone DNSControl repo
([Aetf/dns](https://github.com/Aetf/dns)) is absorbed and retired.

> **Status**: designed 2026-08-22, after a survey of the live
> dnsconfig.js (6 zones; roughly half the records are not
> cluster-related). Declared since 2026-08-25:
> `src/kluster/components/dns/` holds the record model (`model.py`),
> the estate census (`zones.py`), the app records the legacy VPS still
> serves (`legacy.py`, transitional — §6), the route rows `apps` and
> `dns` share (`routes.py`), and the two components that turn data into
> resources (`zone.py`, `adguard.py`, the latter over the custom
> provider in `src/kluster/providers/adguard_rewrites/`). Not yet
> applied: the zones exist at Cloudflare and are imported into state
> before the first `up`.

## 1. Why a fourth stack

The DNSControl census: ZeroTier host records, Google Workspace mail
(MX/SPF/DKIM/DMARC per zone), site verifications, external hosts
(abacus), two family zones (jiahui.id / jiahui.love), and two alias
zones (peifeng.phd, ucw.phd) mirroring the primary
(unlimited-code.works). unlimitedcodeworks.xyz carries the same shared
estate block, with mail and site verifications of its own on top (§2);
its app half is transitional — today it publishes none of the app names
the other mirrors carry and three of its own, and it becomes a mirror in
app names as apps migrate and take the default zone set (§2). None of
that has an app to co-locate with — so it gets its own stack with its own
(rare) change cadence, and non-cluster DNS edits never touch a cluster
stack. Ordering: `physical → dns ∥ k8s-base → apps`; `apps` references
`dns` for zone IDs and `dns` references `physical` for anchor IPs.

The `dns` stack owns: zone resources, the estate records above, the
**anchors** (§2), a reusable per-zone mail component
(MX/SPF/DKIM/DMARC), and — per zone — **CAA records (§1.1) and DNSSEC
enablement**. Verified 2026-08-22: no ready-made Pulumi
SPF/DMARC tooling exists — the ecosystem offers only raw record
resources and officially recommends wrapping your own component — and
none is needed: the current SPF is a single include (no flattening) and
DMARC a static TXT. One implementation gotcha on record: quote TXT
values so SPF strings don't get split on spaces.

### 1.1 CAA is per zone, set by who issues

A CAA record set speaks for every name under the apex, so it has to
name every certificate authority that issues for any of them. Who
those are differs by zone, which makes the classification declared
data (`dns/zones.py`, `ZONE_ISSUERS`) rather than something derived
from the records:

-   **Certificates the cluster obtains are all DNS-01 from Let's
    Encrypt** (cluster-infra.md §1.1). A zone whose names nothing but
    the cluster serves pins `letsencrypt.org` alone.
-   **A zone holding a Cloudflare-proxied name is served at the edge by
    a Cloudflare-issued certificate**, drawn from Cloudflare's partner
    set and not from the cluster's account. Such a zone authorizes that
    whole set — `letsencrypt.org`, `pki.goog` (Google Trust Services,
    with `cansignhttpexchanges=yes`), `ssl.com` and `sectigo.com` — or
    Universal SSL stops renewing. Let's Encrypt is a member, so those
    same records also cover the names in the zone that the proxy does
    not front, which the cluster serves itself. Cloudflare injects
    this set into responses
    for any zone carrying at least one CAA record, invisibly and only
    while it holds the certificate; declaring it is what makes the
    authorization outlive the edge.
-   **A zone whose names something outside this installation serves gets
    no pin invented for it.** jiahui.id is a Google Site, its
    certificates come from `pki.goog`, and it carries no CAA — so it
    keeps none. A pin that current issuance does not satisfy is an outage
    at the next renewal, and no CAA is not a regression from no CAA.

Both tags are written out: `issuewild` does not inherit from `issue`,
and the LAN-only names are covered by per-zone wildcards (§4).

The zones as classified: unlimited-code.works, unlimitedcodeworks.xyz,
peifeng.phd, ucw.phd and jiahui.love hold proxied names and take the
Cloudflare set; jiahui.id takes none.

## 2. Naming hierarchy (formalizing the existing conventions)

-   **`*.hosts.<zone>` is the anchor namespace** — already the live
    convention (`archvps.hosts`). **IP literals exist only here**, with
    low TTLs. New estate: `kluster.hosts` → the NLB, an A *and* an AAAA
    because the load balancer is dual-stack (architecture.md §3.2), and
    `vip1.hosts` → the dedicated VIP (operator convenience; hath itself
    needs no DNS). Both read `physical`'s outputs rather than literals:
    `cluster_endpoint` and `cluster_endpoint_v6` for the balancer,
    `vip1` for the VIP. The VIP is A-only by construction — it is a
    reserved public IPv4 that OCI 1:1-NATs onto a secondary private
    address, and that mechanism has no IPv6 counterpart. Dropped at
    import: `abacus.hosts` (the machine no longer exists — its
    dependents, the Abacus ZT entry and the jupyter/mc records, went
    with it). `archvps.hosts` is **not** dropped: every app record that
    has not migrated targets it, and it retires with the VPS in Wave F
    (§6). The state-backend micro deliberately gets **no anchor**:
    clients pin its IP (`verify-full`, SAN = IP literal) so the state
    backend's hot path never depends on this stack
    (physical/state-backend.md §3).
-   **Anchors are declared in the primary zone only.** An app
    publishing in several zones gets a CNAME in each, and every one of
    them targets `kluster.hosts.unlimited-code.works`, so a node
    rebuild moves one record instead of one per zone. The mirrors do
    carry an `archvps.hosts` copy of their own — ported with the rest
    of the live estate — but nothing targets it: the legacy CNAMEs name
    the primary's copy as well, and the copies retire with the VPS.
    Declaring the anchors reaches across a StackReference and nothing
    else in this stack does, so it is written not to await: an address
    `physical` has not published yet becomes an unresolved record input
    rather than an error, and `dns` previews the same records before
    and after `physical` is applied. Applying is the step that needs
    them real: all three names are exported by `physical`, so what
    stands between the anchors and a value is `physical`'s own apply.
    Publishing the AAAA is not the same as it working — that the
    balancer answers on the address is the NLB's dual-stack
    verification (physical.md §6).
-   **`*.zt.<zone>`** — the ZeroTier host block, unchanged as a
    convention (private IPs in public DNS, deliberate and existing
    practice). It is not a table: it is one A record per entry of the
    overlay roster, `conventions.overlay.ROSTER`, which is the same table the
    `physical` stack declares the membership from (physical/gateway.md
    §2.1). A device joins the overlay and gets its name here by one
    declaration, and a device that leaves loses both — so the legacy
    VPS's record goes when its roster entry does, in Wave F. Nothing
    here crosses the StackReference: an entry carries the member's name
    and its overlay address together, so the whole block is code, and it
    is declared identically before and after `physical` is applied. The
    gateway is the one member the roster may not carry yet, and `udm.zt`
    appears with its entry (physical/gateway.md §2.5).
-   **Apps are CNAMEs to anchors**: `<app>.<zone>` → `kluster.hosts.…`
    declared inside the app component. A node rebuild or VIP re-home
    touches exactly one anchor record, previewed in `dns`.
-   **Alias zones via zone sets, not copy-paste**: the mirrors
    (unlimitedcodeworks.xyz, peifeng.phd, ucw.phd) were maintained as
    duplicated record blocks. `conventions` defines zone sets (e.g.
    `PUBLIC_ALL`, `PRIMARY_ONLY`); an app declares `public_route(host=…,
    zones=PUBLIC_ALL)` once and the helper fans records out across the
    set.
-   **`PUBLIC_ALL` membership means full mirror**: every zone in the
    set carries one shared estate block — the legacy VPS anchor and the
    web origin (`dns/zones.py`, `MIRRORED_ESTATE`) — plus the ZeroTier
    host records, which reach the same set from the stack program
    because they are derived rather than literal. So a name fanned
    across the set resolves in all of it. The cluster anchors those
    names CNAME to are not in the block: they are in the primary alone,
    per the bullet above. A zone joins the set by carrying the block,
    not by being listed; the two facts are held together by a test.
    What a mirror may add on top of the block is its own mail and site
    verifications, which are per-domain by nature. Membership is also a
    promise about app names that only the migration makes true:
    unlimitedcodeworks.xyz is in the set and carries none of the app
    names the other mirrors do (`dns/legacy.py` — the VPS never served
    them there), so the fan-out first reaches it when an app moves into
    `apps` and takes the default zone set.
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
    What a dynamic provider is, and the rules a `diff` here obeys —
    among them that its two property bags are not symmetrical, so the
    comparison names its keys instead of walking a bag — are
    [framework/pulumi.md](../framework/pulumi.md) §5. The instances'
    login is that section's provider credential: `adguardUsername` and
    `adguardPassword` on this stack, read in the provider's own
    `configure` and declared by no rewrite, so no row carries it into
    state. It is an admin login because AdGuard has no scoped API
    token, and that residual is on record as M6
    (cluster/security-audit.md). A rotation is visible all the same —
    every rewrite is stamped with the instance and a short digest of
    the login, so the preview names what changed.
-   **Owned by this stack, not by `apps`.** Split-horizon is DNS, and
    the AdGuard pair is on the UDM: putting the rewrites here keeps the
    LAN reachability requirement — the ZeroTier join, and its
    availability as a dependency of every CI run that touches the stack
    — off the busiest stack in the repo and on the quietest one. `apps`
    then needs no LAN access at all.
-   **adguardhome-sync retires.** With Pulumi dual-writing the dynamic
    config, the sync service is redundant *and* a conflict source (it
    would overwrite bob's Pulumi-written rewrites), and one standing
    service leaves the homelab host. What gw-config takes over is the
    static half (listeners, upstreams) as a **seed**, not as live state:
    it declares one `AdGuardHome.seed.yaml` per instance, under a name
    the instance itself never reads, and the recovery script copies it to
    the `AdGuardHome.yaml` the instance does read only when the working
    directory holds no such file — after a wipe, and at no other time.
-   **A seed, because the file is the instance's own.** AdGuard Home
    keeps its whole configuration in one YAML file that a running
    instance rewrites whenever it accepts a change through its API, and
    it has no include or multi-file mechanism: `upstream_dns_file`
    externalizes the upstream list and nothing else, while the rewrites
    above land in `filtering.rewrites` inside that same file. Declaring
    the live file would therefore delete this stack's rewrites on every
    apply. The two instances start identical because both initial-state
    files come from one template (only the listen address differs), and their
    dynamic halves stay identical because Pulumi writes both; a change
    made in one instance's web UI afterward is reconciled by nothing.
    See `components/gateway/container.py` for the device's side of this.
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
-   **The first LAN-side row is what makes the instances' address a
    required key.** `dns` reads `adguardEndpoints` — the base URL of
    each instance's administration API — only when the route census
    yields a rewrite, which is what keeps the stack deployable while
    nothing is routed and why `Pulumi.dns.yaml` carries the AdGuard
    login but not yet the endpoints. So the config key ships with the
    row that first needs it, in the same change; the route helper
    (`kluster.components.dns.routes`) carries that contract beside the census
    itself.

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
    Same wildcard discipline as item 2 (CT hygiene), and one site
    block for `*.<zone>` with the three vhosts matched inside it — so
    one certificate, for one identifier set. **The gateway asks for
    the wildcard alone and never the apex**: Let's Encrypt counts its
    duplicate-certificate limit by identifier set across accounts, so
    two issuers requesting the same set share one weekly window and a
    crash-looping renewal on either side can lock the other out. The
    cluster's issuer serves the apex publicly and its certificate
    carries apex and wildcard together; the gateway serves none of
    those public names, so the way to keep the two sets apart is for
    it to ask for less.

## 5. The one-line helpers

> **Not implemented; the implementation lands with the `apps` stack.**
> The helpers belong to the app component base, and the stack that
> would define that base refuses by name until it is written
> (`stacks/apps.py` raises). This section and workloads.md §1 are the
> contract that implementation has to satisfy, and nothing in the
> repository emits a route today — which is also why the route census
> §3 reads from is empty.

`public_route(host=…, zones=…, proxied=…)` on the app component base
emits the coherent set: HTTPRoute (to `internet-gw`, `lan-gw`, or both
per the §3.6 matrix), the CNAMEs across the zone set, and — for
both-gateway apps — the route row `dns` writes the AdGuard rewrite
from. The helper never writes a rewrite itself; that is the split
§3 describes. `lan_route(host=…)` is the LAN-only variant (route row
plus a `lan-gw` route, no public record, §4); its
`iot_reachable=True` parameter attaches `media-gw` instead and marks
the row so the rewrite answers with the media VIP — the
review-visible form of "IoT devices may reach this app"
(cluster-infra.md §2, physical/gateway.md §4.2). `public_port(…)` is
the raw TCP/UDP analog, and it is the **only** helper that emits an
NLB listener and its security rule (physical.md §1's
derived-not-enumerated principle) — an HTTP route rides listeners the
cluster already has.

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
