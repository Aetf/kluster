# Declarative Design: the `dns` Stack + Co-located Records

How every DNS record — public and split-horizon — is declared. DNS
splits along ownership: a small **`dns` stack** owns the zones and the
base records that belong to no app, while **per-app records stay
beside their apps** (the co-location principle). There is no in-cluster
DNS controller (architecture.md §6.4); the standalone DNSControl repo
([Aetf/dns](https://github.com/Aetf/dns)) is absorbed and retired.

> **Status**: designed 2026-08-22, after a survey of the live
> dnsconfig.js (6 zones; roughly half the records are not
> cluster-related). Declared since 2026-08-25:
> `src/kluster/components/dns/` holds the record model and the block
> (`record.py`), the base records (`base.py`), the app records the
> legacy VPS still serves (`legacy.py`, transitional — §6), the
> derivation from routes to rewrites (`rewrites.py`), and the two
> components that turn data into resources (`zone.py` and `rewrites.py`
> again, the latter over the custom provider in
> `src/kluster/providers/adguard_rewrites/`). The route rows themselves
> are a convention, `src/kluster/conventions/routes.py`, because `apps`
> and `dns` both read them (README.md §2). **The retiring DNSControl
> program is authoritative until cutover**: the zones exist at
> Cloudflare and their state is imported rather than applied, so this
> stack's first `up` is a cutover step and is not to be run while that
> program still owns the zones, or the two write over each other. Every
> change here is judged by a preview until that day.

## 1. Why a fourth stack

The DNSControl census: ZeroTier host records, Google Workspace mail
(MX/SPF/DKIM/DMARC per zone), site verifications, external hosts
(abacus), two family zones (jiahui.id / jiahui.love), and two parked
zones (peifeng.phd, ucw.phd) that this installation holds and does not
serve (§2). unlimitedcodeworks.xyz is a website co-host: it answers for
the same apex and `www` as the primary (unlimited-code.works), from the
same instance, and carries mail, site verifications and three names of
its own on top. It publishes no application name and is not going to
(§2). None of that has an app to co-locate with — so it gets its own
stack with its own (rare) change cadence, and non-cluster DNS edits
never touch a cluster stack. Ordering:
`physical → dns ∥ k8s-base → apps`; `apps` references `dns` for zone
IDs and `dns` references `physical` for anchor IPs.

The `dns` stack owns: zone resources, the base records above, the
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
data (`dns/base.py`, `ZONE_ISSUERS`) rather than something derived
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
    no pin invented for it.** jiahui.id is a Google Site, and the log
    (§1.2) shows several authorities issuing for it rather than one: the
    pair — apex and `*.jiahui.id` in one certificate — from whichever of
    the edge's partners its rotation draws, since the edge mints one for
    every zone it hosts, proxied or not, beside an apex-only certificate
    from Let's Encrypt. It carries no CAA, so it keeps none. A pin that
    current issuance does not satisfy is an outage at the next renewal,
    and no CAA is not a regression from no CAA.

Both tags are written out: `issuewild` does not inherit from `issue`,
and the LAN-only names are covered by per-zone wildcards (§4).

The zones as classified: unlimited-code.works, unlimitedcodeworks.xyz,
peifeng.phd, ucw.phd and jiahui.love hold proxied names and take the
Cloudflare set; jiahui.id takes none.

### 1.2 The Certificate Transparency read

What holds `ZONE_ISSUERS` to the world is not a test: the edge set is a
copy of a list nothing here can see, and a test that fetched it would
put a network call in the gate. What can be held offline is held there
(`tests/test_dns_records.py`); what those tests and the edge's renewal
notification leave — a third party issuing under a zone this
installation holds — is this read, run by hand at each milestone's
review checkpoint beside the documentation audit
([dispatch.md](../framework/dispatch.md) §3.1). **Nothing in the
repository automates it**: no test, script or workflow queries a log, so
the checkpoint issue is its only moment. The read's outcome and the date
it ran go on that issue in one line, because the next read's window is
set from it: the window starts a week before the previous read's date,
since crt.sh ingests with a lag and a certificate issued before a read
but logged after it would otherwise fall between two windows (the ids
make the overlap harmless). A first read has no previous date and takes
the certificates valid on the day it runs — `&exclude=expired` goes on
the URL for that read alone, which crt.sh filters on
`not_after >= now()`, and `since` is the empty string, which the window
comparison lets everything through (the read's own date lets nothing
through and records a clean read from nothing) — because that is the
question a read with no window asks; every later read takes what was
issued since, expired or not. Every read is two runs
of each query with the ids compared, and the union is the answer: the
endpoint sometimes returns a strict subset of its own rows, with nothing
in the response saying so.

**What is read.** For every zone in `conventions.ALL_ZONES`, every
certificate a public log holds for the apex or any name under it, from
crt.sh's JSON endpoint — which caps an identity search at 10,000
matched identities, a few thousand certificates, and says nothing in
its JSON when it does, so a count in the thousands is itself a finding:

    Z=<zone>; curl -s "https://crt.sh/?q=$Z&match=ILIKE&output=json&deduplicate=Y" \
      | jq -r --arg z "$Z" --arg since <previous read> '.[]
          | select(.not_before >= $since)
          | .name_value |= (split("\n") | map(select(. == $z or endswith("." + $z))) | join(" "))
          | select(.name_value != "")
          | [.id, .issuer_name, .name_value, .not_before] | @tsv'

`match=ILIKE` is a substring match over each certificate's identities,
so it reaches the apex and every name under the zone; left unset, crt.sh
infers the mode from the term, and a term with a hyphen — the primary —
gets a full-text match that finds the pair and not a name under the
zone. The `jq` suffix guard is what makes a substring match exact.
`%25.<zone>` is the same search either way, since crt.sh drops a leading
`%.` before matching. `deduplicate=Y` folds the two entries one issuance
leaves in a log into one row. `exclude=expired` is not passed on a
windowed read: a certificate issued and expired between two checkpoints
is exactly what the read exists to see. The window is applied locally on
`not_before`, because the endpoint's `minNotBefore` does not apply to an
identity search. The columns are the crt.sh id, the issuer's name, the
certificate's names under the zone — crt.sh lists the identities the
search matched, not the whole subject alternative name set, which is why
the guard runs over what it lists — and the not-before date.

**The expected answer** for a zone in `ZONE_ISSUERS` is that every row's
issuer is a member of that zone's set, and every name is a name the
declaration carries, in the shape its asker mints: the pair — apex and
`*.<zone>` in one certificate — from any permitted issuer, since the
edge mints one for every zone it hosts and the cluster for every zone in
its scope (§4) and the log does not say which asked; the wildcard
*alone* for each zone the gateway's ACME scope names
(`derived.GATEWAY_ACME_ZONES` — today the primary, whose
`*.<ZONE_PRIMARY>` covers the console and resolver vhosts, and
`ZONE_SHORT` for `*.<ZONE_LEGACY>` while the legacy census has a row,
which is the zone the scope sheds at the end of Wave D), since the
gateway asks for the wildcard and never the apex (§4,
`gateway/container.py`); and a single name a record block
(`dns/base.py`, `dns/legacy.py`) or a route row (`conventions/routes.py`)
declares, which is what the legacy VPS obtains for a name it serves off
the proxy. A lone `*.<zone>` from a permitted issuer for a zone outside
that scope is as much a finding as a name nothing declares — only a
token scoped like the gateway's produces that shape. The log shows
issuance, not use: a permitted
issuer's certificate for a declared name is expected even when nothing
serves it, and the read draws no conclusion from one. The issuer column
is a distinguished name and the set holds CAA identities, so the
comparison is to the domain the authority publishes as its CAA identity,
and membership is by the domain before any parameter
(`pki.goog; cansignhttpexchanges=yes` is `pki.goog`). The set's members,
as crt.sh prints them:

-   `O=Let's Encrypt` is `letsencrypt.org`.
-   `O=Google Trust Services` and `O=Google Trust Services LLC` are
    `pki.goog`.
-   `O=Sectigo Limited` is `sectigo.com`, and so is
    `O=COMODO CA Limited`, the same authority under its former name —
    why its former identity `comodoca.com` is not in the set and not a
    finding, and why the set is narrower than what the primary's
    responses carry, is
    [rfc-003](../rfc/rfc-003-dns-and-github-stacks.md) §4.4.
-   `O=SSL Corp` is `ssl.com`.

An intermediate named for its customer maps to its operator's identity,
not the customer's. An issuer not listed here is resolved from its
Certification Practice Statement, or from the Cloudflare
certificate-authorities page `base.py` cites where it is one of the
edge's. A zone absent from `ZONE_ISSUERS` has no set to compare against;
its rows say who issues for it, which is what a pin would have to name
(§1.1) and is the reason it carries none.

**A finding** is a row whose issuer is outside the zone's set, or whose
name the declaration does not carry. Findings are filed as issues in
the ops repository (dispatch.md §4), one per distinct issuer-and-names
pair rather than per row, since a renewal repeats the row every cycle:
the issue names the issuer and the names and lists its rows' crt.sh ids
and not-before dates. It is never resolved by widening the set to fit:
whether the set is behind who legitimately issues, or the certificate
is one nobody here asked for, is the issue's question.

## 2. Naming hierarchy (formalizing the existing conventions)

-   **`*.hosts.<zone>` is the anchor namespace** — already the live
    convention (`archvps.hosts`). **IP literals exist only here**, with
    low TTLs, and two clauses name where an address may sit outside it:
    **where the name cannot be an alias**, and **where there is no name
    of this installation's to alias**. Both are structural rather than
    exceptions granted: a zone apex cannot be a CNAME, so the web origin
    every website zone carries is an apex `A` and so is jiahui.love's;
    and jiahui.id's apex and `www` address a site served entirely outside
    this installation, which publishes no name of ours for them to alias.
    `base.py` says the same beside the literals. New anchors:
    `kluster.hosts` → the NLB, an A *and* an AAAA because the load
    balancer is dual-stack (architecture.md §3.2), and
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
    rebuild moves one record instead of one per zone. The legacy VPS
    anchor `archvps.hosts` is in the primary zone alone for the same
    reason: every CNAME that names the VPS, in every zone, targets that
    one record, and it retires with the machine.
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
-   **`*.zt.<zone>`** — the overlay host block, unchanged as a
    convention (private IPs in public DNS, deliberate and existing
    practice). The label stays `zt` because it is a published DNS name
    and ZeroTier is the target system's own word; what this repository
    calls it — `conventions.OVERLAY_LABEL`, and the two functions that
    build the block — is our own. It is not a table: it is one A record
    per entry of the
    overlay roster, `conventions.overlay.ROSTER`, which is the same table the
    `physical` stack declares the membership from (physical/gateway.md
    §2.1). A device joins the overlay and gets its name here by one
    declaration, and a device that leaves loses both — so the legacy
    VPS's record goes when its roster entry does, in Wave F. The block
    is declared in the primary zone alone: every overlay name a
    configuration file in this installation holds is the primary's, so
    a copy in another zone would republish a private address nothing
    reads. Nothing here crosses the StackReference: an entry carries
    the member's name and its overlay address together, so the whole
    block is code, and it is declared identically before and after
    `physical` is applied. The
    gateway is the one member the roster may not carry yet, and `udm.zt`
    appears with its entry (physical/gateway.md §2.5). The block's name
    as a client sees it is `conventions.dns.OVERLAY_DOMAIN`, and it has
    a second reader: the `physical` stack pushes it to the overlay's
    members as the network's managed-DNS search domain, with alice and
    bob as the resolvers for it (physical/gateway.md §2.7). A member
    that opts in resolves `*.zt` at home and nothing else there, and
    the resolvers answer such a name today by forwarding it upstream,
    which returns this block's record; a member that does not opt in,
    and every Linux member, resolves the block through the public
    records as before. Application names are outside the pushed domain
    by design, so the push changes nothing about how any of them
    resolves.
-   **Apps are CNAMEs to anchors**: `<app>.<zone>` → `kluster.hosts.…`
    declared inside the app component. A node rebuild or VIP re-home
    touches exactly one anchor record, previewed in `dns`.
-   **A public record needs a listener and a certificate.** A public
    record is published in a zone only where a listener and a
    certificate answer for the name. A route's zone set is what serves
    the application, and the default is the primary; a name that
    resolves is a promise the origin has to keep. **No zone mirrors
    another**, and an application could not be mirrored even with DNS
    and a certificate in place: every forward-auth application shares
    one SSO cookie domain and one portal URL, every OIDC application
    registers its redirect URIs against a hostname, several hold an
    absolute origin of their own, and a federated matrix server's name
    is immutable. A second hostname for any of them is a login that
    loops or is refused.
-   **The unit is the block, and the per-zone view is derived.** A block
    is the records that appear together, in every zone of one set, and
    the zone set is the block's first column rather than a field on each
    record — a mail exchanger that had wandered into a different zone set
    is a state a per-record field would make writable, and a block header
    does not. What one zone carries is `zone_records(zone, blocks)`,
    derived in front of the zone component rather than inside it, because
    Cloudflare's API is per zone and the component is generic. It is the
    same principle as the overlay roster (one entry per member, not one
    per network object) and `Exposure` (what reachability *is*, not which
    resources it produces), and it is stated for tables generally in
    [style/pulumi.md](../style/pulumi.md).
-   **Zone sets, not copy-paste**: `conventions` names the zone sets a
    route row is declared against and a record block names in its header.
    `PRIMARY_ONLY` is the primary alone — every route's default, both
    anchor blocks, the overlay block, and every application name the
    legacy VPS still serves. `WEB_ZONES` is the zones whose apex and
    `www` are served: the primary and the website co-host.
    `PARKED_ZONES` is the pair held and not served (below).
    `ZONE_FAMILY` is the family pair, taxonomy only, so that
    `ALL_ZONES` — every zone the stack declares, which is what the
    program loops over — can be written as a union of named sets. A zone
    set only one block names is declared beside that block instead:
    `MAIL_ZONES`, the two Google Workspace domains, is in `base.py`. An
    app hands its census row to `route` and gets the primary; naming
    another zone in the row is what says that zone serves the
    application too, and the helper fans the records across whatever the
    row names.
-   **A parked zone is held and not served.** peifeng.phd and ucw.phd
    hold nothing of this installation's: what resolves in either is a
    copy of one of the primary's names addressed at the legacy VPS,
    answered by that machine's own catch-all with a redirect to the
    primary, and no certificate has ever been issued at an origin for
    one. Parked is a state and not a stage — ucw.phd outlives its last
    record, because it is the zone the gateway's proxy holds
    `*.lan.ucw.phd` under (§4), so its CAA is load-bearing for the
    gateway's renewals long after its last record goes; peifeng.phd is
    held because a domain in hand is cheaper than a domain reacquired.
    What each carries is the apex and `www` and the CAA set, and the
    apex and `www` retire with the machine they address rather than
    being repointed, since neither zone holds anything for the cluster
    to serve. **The end state is that both zones are dark**: an old
    link that used to land on the primary's front page stops resolving
    rather than arriving somewhere else. Nothing in any repository,
    configuration file or certificate references either name.
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
    every rewrite is stamped with the door it was written through — the
    endpoint, and a short digest of the login — so the preview names
    what changed.
-   **Owned by this stack, not by `apps`.** Split-horizon is DNS, and
    the AdGuard pair is on the UDM: putting the rewrites here keeps the
    LAN reachability requirement — the ZeroTier join, and its
    availability as a dependency of every CI run that touches the stack
    — off the busiest stack in the repo and on the quietest one. `apps`
    then needs no LAN access at all.
-   **adguardhome-sync retires.** With Pulumi dual-writing the dynamic
    config, the sync service is redundant *and* a conflict source (it
    would overwrite bob's Pulumi-written rewrites), and one standing
    service leaves the homelab host. What the `physical` stack's
    `ResolverService` takes over is the static half (listeners,
    upstreams) as an **initial state**, not as live state: it declares
    one `AdGuardHome.yaml` per instance, delivered under the machine's
    `initial-state/` directory rather than into its state, and the
    device's `40-machines.sh` copies that directory onto the state
    directory the instance reads only while the state directory is
    empty — before the first start, or after a wipe, and at no other
    time (physical/gateway.md §1.1). What keeps the seed apart from the
    live file is where it sits, not a second name.
-   **An initial state, because the file is the instance's own.**
    AdGuard Home keeps its whole configuration in one YAML file that a
    running instance rewrites whenever it accepts a change through its
    API, and it has no include or multi-file mechanism:
    `upstream_dns_file` externalizes the upstream list and nothing
    else, while the rewrites above land in `filtering.rewrites` inside
    that same file. Declaring the live file would therefore delete this
    stack's rewrites on every apply. The two instances start identical
    because both initial-state files come from one template (only the
    listen address differs), and their dynamic halves stay identical
    because Pulumi writes both; a change made in one instance's web UI
    afterward is reconciled by nothing. See
    `components/gateway/container.py` for the device's side of this.
-   **Placement**: rewrites are emitted automatically for any app
    with a LAN-side gateway attachment — split-horizon (both
    gateways), LAN-only (`lan-gw`), or IoT-reachable (`media-gw`,
    rewrite targeting the media VIP) — an app cannot forget its
    rewrite because it never writes it by hand. Crossing a stack
    boundary does not weaken that: an app's route declaration is
    **plain data** in a module both stacks import
    (`kluster.conventions.routes`), so `apps` builds its HTTPRoutes from
    it and `dns` builds the rewrites from the same rows. A rewrite's
    answer is an address and never a name, so what it steers a LAN
    client to cannot depend on some other rewrite existing to resolve
    it. One edit, two stack diffs, both previewable — rather than
    one edit and a second stack to remember. LAN ULA AAAAs are
    emitted alongside (RFC 6724 caveat noted, architecture.md §1.3).
-   **Where each instance is reached is derived, not configured.** A
    rewrite is written over plain HTTP to the instance's address on the
    container VLAN, at the port `conventions.gateway.ADGUARD_API_PORT`
    names — the same constant the caddy vhost that proxies the instance,
    the initial state that tells it where to listen, and the overlay
    flow rule that admits a `dns` run all meet on. There is no endpoint
    key to set, and therefore nothing for a stale one to disagree with;
    `Pulumi.dns.yaml` carries the AdGuard login and nothing else about
    the pair. Dialling the instance's own public name instead would have
    the runner resolve a name that only a split-horizon rewrite answers,
    which is the thing the run is declaring.
-   **A rewrite is identified by its instance, not by its address.**
    `dns` declares one `ResolverRewrites` component per entry of
    `conventions.gateway.RESOLVERS`, and both the resource id and the
    logical name are built from that entry's name — never from the
    address. Re-addressing an instance is then an update that dials
    somewhere new and writes the same rows in the same place, rather
    than a delete of every rewrite at the old address and a create of
    every one at the new. The general rule is
    [style/pulumi.md](../style/pulumi.md)'s.

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
    `lan` VIP. **LAN-only services are rewrite-only**: the row yields
    the AdGuard rewrite (from `dns`, §3) and the `lan-gw` route (from
    `apps`, §5) but *no* Cloudflare record — public resolvers see
    NXDOMAIN, while cert-manager DNS-01 still issues real certificates
    for the name (challenge records don't require the name itself to be
    published). Issuance for these names is **per-zone wildcard
    certificates**, not per-name: every issued certificate lands in
    public Certificate Transparency logs, and per-name issuance would
    republish exactly the LAN-only service census that rewrite-only just
    hid.
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
    a device secret, cluster-infra.md §1.1) rather than
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

`route(conventions.routes.PHOTOS)` on the app component base **takes
the application's census row**, not a host string a reader would have
to match against the census by eye — so an application publishing a
name `dns` never rewrites cannot be written. From that row it emits the
coherent set: the HTTPRoute, attached to the gateways the row's
`Exposure` selects per the routing matrix (cluster/architecture.md
§3.6) — `internet-gw`, `lan-gw`, `media-gw`, or both public and LAN —
and the CNAMEs across the zone set the row names, for the rows that
publish a public record at all, which is what `Route.public` says and
which no LAN-side exposure does (§4). It is the same row `dns` writes
the AdGuard rewrite from, read by both stacks rather than emitted by
either; the helper never writes a rewrite itself, which is the split §3
describes.

**There is one route helper, because the row already states the
exposure.** Splitting it into a public one and a LAN-only one would
make the call a second statement of the same thing, and a call that can
disagree with its row forces the helper to rule on which of the two
wins. Nothing else would distinguish the two: they emit the same kinds
of resource, and each would differ only in which subset of `Exposure`
values it accepts, which is a check rather than a difference in what is
emitted. `Exposure.IOT` is a LAN-side value like `LAN_ONLY`, differing
only in attaching `media-gw` so that the rewrite answers at the media
VIP and the IoT VLAN reaches the app; "IoT devices may reach this app"
is therefore decided once, on the census row a reviewer reads, and the
helper has no say in it (cluster-infra.md §2, physical/gateway.md
§4.2).

`public_port(…)` is the raw TCP/UDP analog, and it stays a helper of
its own because it emits something no HTTP route does: it is the
**only** helper that emits an NLB listener and its security rule
(physical.md §1's derived-not-enumerated principle) — an HTTP route
rides listeners the cluster already has.

## 6. Migration shape

Per-app cutover falls out of the anchor design: an app's records point
at `archvps.hosts` until the app migrates, then its component declares
the same names against `kluster.hosts` (and the DNSControl entry is
deleted).

**`legacy.py` is one block per application**, because the unit of every
remaining edit to it is a migration: each block is deleted whole when the
application named in its header migrates, and the module empties block
by block through Wave F. A migration is therefore three files in one
pull request — the block deleted from `legacy.py`, the row added to
`conventions/routes.py`, and the `route` call added to the app's
component — with no row of a shared tuple to pick out. A block whose
header names no application is a block no migration will ever delete,
publishing a name no component will ever claim, so it is dropped rather
than carried. Two blocks are kept whose owner exists but whose component
does not yet: `mon`, the monitoring dashboard, which is in use, and `bt`,
which the host qbittorrent claims when it migrates. Each names that owner
in its header, which is what says it is waiting rather than orphaned.

Base records import wholesale into the `dns` stack early — note the
**base records that themselves reference the VPS**: the shared web origin
block — the apex and `www` of unlimited-code.works,
unlimitedcodeworks.xyz, peifeng.phd and ucw.phd — plus `jiahui.love`'s
apex, which is the same record declared in the family zone rather than a
fifth member of the block. The served pair repoints to `kluster.hosts`
when the site behind it moves; the parked pair's records go with the
machine instead (§2). None may still reference `archvps.hosts` when the
VPS retires (migration.md Wave F checks this). The DNSControl repo
retires with a pointer commit (the old-tracker rule, migration.md §0).
