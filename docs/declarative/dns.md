# Declarative Design: the `dns` Stack + Co-located Records

How every DNS record — public and split-horizon — is declared. DNS
splits along ownership: a small **`dns` stack** owns the zones and the
base records that belong to no app, while **per-app records stay
beside their apps** (the co-location principle). There is no in-cluster
DNS controller (architecture.md §6.4); the standalone DNSControl repo
([Aetf/dns](https://github.com/Aetf/dns)) is absorbed and retired.

> **Status**: designed 2026-08-22, after a survey of Aetf/dns's
> dnsconfig.js (6 zones; roughly half the records are not
> cluster-related). Declared since 2026-08-25:
> `src/kluster/components/dns/` holds the record model and the block
> (`record.py`), the base records (`base.py`), the app records the
> legacy VPS still serves (`legacy.py`, transitional — §6), the
> resolvers' rule list with the derivations that produce its rows
> (`rewrites.py`), the rest of what the resolvers are configured with
> (`resolver_settings.py`, `aliases.py`, `blocklists.py`), and the two
> components that turn data into resources (`zone.py`, and `resolver.py`
> over the custom provider in `src/kluster/providers/adguard/`). The
> route rows themselves are a convention,
> `src/kluster/conventions/routes.py`, because `apps` and `dns` both
> read them (README.md §2). **This stack writes the
> zones**, from its first `up` on, the zones' cutover
> (physical/gateway-cutover.md §8.2). Their state came in by import and
> the cutover's `up` applied it, deleting each record the import took
> that the declaration leaves out. Aetf/dns, the DNSControl program it
> replaces, is retired and archived. Row R1 of
> [sources-of-truth.md](../sources-of-truth.md) holds the readings and
> how a change is applied until the deploy chain applies it on merge,
> and its note N1 holds the records the cutover deleted and the
> emergency way back. The resolvers' configuration is applied by the
> deploy chain: its first write was the merge of Aetf/kluster#454, row
> R2's flip.

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
`physical → dns → k8s-base → apps`, with `k8s-base` and `apps` each
after `dns` whatever its result. A migration's cut hands a name from
`dns` to a cluster stack in one run, and neither stack can adopt the
other's record while Cloudflare refuses a CNAME beside another record of
the same name, so the delete has to come first; a failed `dns` stops
neither (framework/ci.md §3). `apps` references `dns` for zone IDs and
`dns` references `physical` for anchor IPs.

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
    no pin invented for it.** jiahui.id's mail is forwarded by its
    registrar, so what serves the zone's names, and which authority a
    service there uses, is decided outside this installation, and the
    operator keeps the zone unpinned (`kluster-ops#703`). In the crt.sh
    reads of 2026-10-29 (§1.2), what was issued for it after 2026-08-08
    is the edge's alone: the pair — apex and `*.jiahui.id` in one
    certificate — from `pki.goog` and `sectigo.com`, since the edge mints
    one for every zone it hosts, proxied or not. A pin of the edge's set
    would admit that, and refuse any other authority, which a service the
    zone's names point at might use. It carries no CAA, so it keeps none,
    and no CAA is not a regression from no CAA.

Both tags are written out: `issuewild` does not inherit from `issue`,
and the LAN-only names are covered by per-zone wildcards (§4).

The zones as classified: unlimited-code.works, unlimitedcodeworks.xyz,
peifeng.phd, ucw.phd and jiahui.love hold proxied names and take the
Cloudflare set; jiahui.id takes none. It is declared as such in
`conventions` (`ZONE_EXTERNALLY_ISSUED`), with that reason beside it, and
`ZONE_ISSUERS` leaves it out, pointing at that declaration.

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
its JSON when it does, so a count in the thousands is itself a finding.
From the root of the checkout:

    Z=<zone>; out=$(curl -fsS "https://crt.sh/?q=$Z&match=ILIKE&output=json&deduplicate=Y") \
      && mise x -- jq -r --arg z "$Z" --arg since <previous read> '[.[]
          | select(.not_before >= $since)
          | .name_value |= (split("\n") | map(select(. == $z or endswith("." + $z))) | join(" "))
          | select(.name_value != "")]
          | (.[] | [.id, .issuer_name, .name_value, .not_before] | @tsv), "\(length) rows"' <<<"$out"

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

**A run that does not end on its `<n> rows` line is no reading.** An
empty answer passes, and it is also what a failed fetch would hand
`jq`, so the fetch is kept apart from the count: `curl -f` fails on
an HTTP error rather than passing an error page on, `jq` runs only on
what `curl` fetched, and what fails prints its error in place of the
count line. Such a run is taken again, and an answer of no rows is one
that ends on `0 rows`.

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
(§1.1).

**A finding** is a row whose issuer is outside the zone's set, or whose
name the declaration does not carry. Findings are filed as issues in
the ops repository (dispatch.md §4), one per distinct issuer-and-names
pair rather than per row, since a renewal repeats the row every cycle:
the issue names the issuer and the names and lists its rows' crt.sh ids
and not-before dates. It is never resolved by widening the set to fit:
whether the set is behind who legitimately issues, or the certificate
is one nobody here asked for, is the issue's question.

### 1.3 A zone's DS, and taking it away

The stack signs each zone (`ZoneDnssec`, in `components/dns/zone.py`);
what makes the signature count is the DS at the zone's parent, which is
in no stack: the zone's registrar puts it there. A DS that matches the
zone's key-signing key makes every answer from the zone validated;
**a DS that matches no key fails the whole zone for every validating
resolver**, until it is removed and the parent's TTL for it has run
out. So the removal is the way back from any DS change, the first
entry included, and it is also the first step of anything that stops
signing a zone.

**Who enters and removes the DS is the registrar's question.**
jiahui.id is registered at Namecheap, Cloudflare Registrar not offering
`.id`, and its DS is entered and removed there by hand. Every other
zone of `conventions.ALL_ZONES` is registered at Cloudflare Registrar,
which manages the DS of a zone it both registers and signs:

-   **Cloudflare enters the DS.** It publishes a signed zone's DS in the
    zone itself, as `CDS` and `CDNSKEY` records, and the registrar scans
    those at regular intervals and sends what they carry to the
    registry. The first DS after signing is turned on takes one to two
    days, and its timing cannot be controlled
    ([Enable DNSSEC](https://developers.cloudflare.com/registrar/get-started/enable-dnssec/)
    in Cloudflare's Registrar documentation;
    [DNSSEC](https://developers.cloudflare.com/dns/dnssec/), under
    "Roll back DNSSEC").
-   **Turning DNSSEC off withdraws it, and does not stop the signing.**
    A zone whose DNSSEC status is set to `disabled` goes to
    `pending-disabled`: Cloudflare still signs it and serves its
    `DNSKEY`, and sets its `CDS` and `CDNSKEY` to the deletion signal of
    RFC 8078 §4, which tells the registrar to take the DS away. The zone
    reads `disabled` once the DS is gone from the registrar
    ([DNSSEC states](https://developers.cloudflare.com/dns/dnssec/dnssec-states/)).
    Cloudflare's rollback steps give that disable, for a domain at
    Cloudflare Registrar, in place of removing the DS at the registrar,
    and keep the zone signed until the DS's TTL has run out at the
    parent ([DNSSEC](https://developers.cloudflare.com/dns/dnssec/),
    under "Roll back DNSSEC"). The dashboard has the switch under the
    domain's **Configuration** in **Manage Domains**
    ([Enable DNSSEC](https://developers.cloudflare.com/registrar/get-started/enable-dnssec/)).
    How long the registrar takes to act on the deletion signal is not
    established: the documentation gives no figure for it. The parent's
    own server shows when the DS is gone, below.

**Removing a DS** is, for jiahui.id, the record deleted on the domain's
Advanced DNS page at Namecheap, its DNSSEC section, and for a zone at
Cloudflare Registrar, DNSSEC turned off as above. Either way the zone
stays signed and goes back to unvalidated. Validating resolvers recover
once the parent's TTL for the removed record has run out, which the
parent's own server prints beside the record:

```sh
z=<the zone>
tld=${z##*.}
pns="$(drill NS "$tld" | awk '/^;/ {next} $4 == "NS" {print $5; exit}')"
drill DS "$z" @"$pns"      # before: the record and its TTL; after: no DS in the answer
```

**The order never stops signing first.** What stops it is the deletion
of the zone's DNSSEC records, Cloudflare's `deleted` state, in which the
zone serves no `DNSKEY` and no signature
([DNSSEC states](https://developers.cloudflare.com/dns/dnssec/dnssec-states/)).
A delete of the zone's `ZoneDnssec` ends there: the provider sets the
status to `disabled` and then deletes the records
(`internal/services/zone_dnssec/` in the Terraform provider that
pulumi-cloudflare bridges). A zone whose parent holds a DS keeps
signing until the removal's TTL has run out: stopping the signing first
leaves a DS that no key matches, and that is the same outage as a wrong
one. Cloudflare refuses the deletion until the zone reads `disabled`,
which its state table ties to the DS being gone from the registrar and
not to the parent's TTL for it having run out; its own guidance waits
one and a half times that TTL after the removal before deleting the
records
([Troubleshooting](https://developers.cloudflare.com/dns/dnssec/troubleshooting/),
under "Delete remaining DNSKEY records after disabling DNSSEC").
Entering a DS waits the other way round, for the zone's server to
answer the key it is for, which at Cloudflare Registrar the scan of the
zone's own records ensures. physical/gateway-cutover.md §8.4 enters
jiahui.id's DS in that order, and reads the DS in place for any zone.

## 2. Naming hierarchy (formalizing the existing conventions)

-   **`*.hosts.<zone>` is the anchor namespace** — already the live
    convention (`archvps.hosts`). **IP literals exist only here**, with
    low TTLs, and one clause names where an address may sit outside it:
    **where the name cannot be an alias**. That is structural rather than
    an exception granted: a zone apex cannot be a CNAME, so the web
    origin every website zone carries is an apex `A` and so is
    jiahui.love's. `base.py` says the same beside the literals. A
    website's `Apex` row in the exposure census (§5) publishes no record
    of its own: its record is this web origin, and the census holds each
    apex row's zone to one. New
    anchors:
    `kluster.hosts` → the NLB, an A *and* an AAAA because the load
    balancer is dual-stack and serves every port it forwards on both
    families (architecture.md §3.2, physical.md §1), and
    `vip1.hosts` → the dedicated VIP (operator convenience; hath itself
    needs no DNS). Both read `physical`'s outputs rather than literals:
    `cluster_endpoint` and `cluster_endpoint_v6` for the balancer,
    `vip1` for the VIP. The VIP is A-only by construction — it is a
    reserved public IPv4 that OCI 1:1-NATs onto a secondary private
    address, and that mechanism has no IPv6 counterpart. Dropped at
    import: `abacus.hosts` (the machine no longer exists — its
    dependents, the Abacus overlay entry and the jupyter/mc records, went
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
    else in this stack does, and each address is checked where it is
    read: one `physical` has not published, or one that is not an
    address of its record's family, stops the run naming the output, in
    a preview and an update alike. All three names are exported by
    `physical`, so what stands between the anchors and a value is
    `physical`'s own apply, and `dns` runs after it.
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
    `physical` is applied. The block sits under the primary, which is
    the network's managed-DNS search domain (physical/gateway.md §2.7),
    so on a macOS or Android device that holds the overlay's tunnel and
    applies its resolver a member's bare label completes as
    `<label>.zt`, and on Windows and Linux the member is reached at its
    full name (physical/gateway.md §2.7); alice and bob answer such a
    name from a rule of their own (§3), one per roster entry, derived
    from the same entry as the record, so the rule and the public record
    are one name and one address whichever of them answers. A home
    client, whose resolver is alice or bob for every name, gets the
    rule's answer too, and every other device resolves the block
    through the public records.
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
    application's row is published in the primary unless it names
    another zone, and naming one is what says that zone serves the
    application too; its records fan across whatever the row names
    (§5).
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
-   **A record published direct says why, at its declaration.** Every
    record of a type Cloudflare's proxy can front — A, AAAA and CNAME —
    is proxied unless it carries a reason not to be, in the record's
    `unproxied` field, and `proxied` is derived from that rather than
    written beside it. The reasons are the census's, not lore in
    comments: large uploads (photos), ports and protocols the proxy does
    not carry (matrix, syncthing), an address the edge cannot reach (the
    overlay block), and the anchors, each handing out an origin's address
    that the names aliased to it resolve through — a name is proxied when
    any record on its CNAME chain is, so a direct name is direct only
    while its anchor is. The zone component sends
    nothing for the reason's wording, and the dashboard's explanation
    stays the record's comment.

## 3. The resolvers: AdGuard, both instances, no sync

LAN clients must resolve split-horizon apps to their `lan` VIPs,
never the cloud path. Which clients the rewrites steer is defined in
architecture.md §3.4: those whose resolver is alice/bob for the name
asked — every LAN lease, and an overlay member that holds the tunnel and
applies its resolver, which is a home client by then (physical/gateway.md
§2.7). There is one view: the overlay is a path into the home audience
and not an audience of its own (architecture.md §3.3), so no rewrite
answers an overlay client differently from a home one. The AdGuard pair
(alice/bob) lives on the UDM, and this stack declares each instance's
whole configuration, the split-horizon rewrites among it.

-   **Mechanism: dynamic resources over the administration API**,
    applied **directly to both instances**
    (`src/kluster/providers/adguard/`). Each instance gets its
    first-run setup, `AdGuardSetup`, and six kinds that each own one
    subsystem's endpoints: the rule list, the DNS server with its
    protection switch and access lists, the filtering switches with
    safe search and blocked services, the filter lists, the persistent
    clients, and the query-log and statistics settings. Every key the
    API can set belongs to exactly one of them, apart from those left
    undeclared below. A `diff` calls no
    instance, so a preview costs neither resolver anything; a `read`
    dials, and reports what the instance holds as the inputs, so a
    refreshing preview renders a hand edit as a property diff; an
    update writes only the sections that differ, whole. What a dynamic
    provider is, and the rules a `diff` here obeys — among them that its
    two property bags are not symmetrical, so the comparison names its
    keys instead of walking a bag — are
    [framework/pulumi.md](../framework/pulumi.md) §5. The instances'
    login is that section's provider credential: `adguardUsername` and
    `adguardPassword` on this stack, read in the provider's own
    `configure` and declared by no resource, so no resource carries it
    into state. It is an admin login because AdGuard has no scoped API
    token, and that residual is on record as M6
    (cluster/security-audit.md). A rotation is visible all the same —
    every resource is stamped with the door it was written through, the
    endpoint and a short digest of the login — so the preview names
    what changed. Each instance is classified once per `pulumi` process,
    before any resource sends it the login, so a wrong password costs
    one refused login per instance rather than one per resource,
    against an instance that blocks the caller for fifteen minutes
    after five.
-   **No two instances restart their DNS listeners at once.** A write
    of the DNS server's settings restarts that instance's listener, so
    on every update it waits for the same write on the instance before
    it in `conventions.gateway.RESOLVERS`: the LAN keeps resolving
    through whichever instance is not restarting, which is the rule
    `physical` keeps for the resolvers' machines too.
-   **The rule list is generated, and written whole.** No rule on
    either instance is written by hand: every line of the list is
    rendered by `rewrites.user_rules`, a header saying the stack
    declares it and then each block under a comment naming its source —
    the exposures' split-horizon rewrites, the overlay members (§2), the
    gateway's own names at its proxy, the legacy names until their
    application migrates (§4 item 3), the aliases to device-plane names
    (§4 item 1), and the hosts one client may not resolve. One
    `AdGuardUserRules`
    resource per instance owns the list and replaces it in one request,
    so a line added, edited or reordered in the UI is drift. A rewrite
    takes the exact anchor, `|name^`, so a bare label answers for
    itself and not for a top-level domain of its spelling; its record
    type is its answer's; and no rule narrows the query type, so a
    rewritten name answers every other type with an empty `NOERROR` and
    no query for it leaves the instance. The **rewrite list**, the
    instances' other mechanism, is not used: the declaration holds it
    empty and switched off, a row added by hand is drift, and the list
    is refused before it is written while such a row stands.
-   **What stays undeclared, and why.** The statistics and the query
    log are what the instance measures (their settings are declared);
    TLS is unused, since caddy terminates TLS for the instances' names
    (§4 item 4); DHCP is off, since every VLAN's leases are the
    controller's (§4 item 1); the UI's language, theme and session
    settings change no answer; and what the instance generates — list
    contents, ids, client UIDs, runtime clients — is regenerated by
    running it. The admin account is not a setting at all: no endpoint
    lists, changes or deletes one, and only the first-run setup makes
    one.
-   **Owned by this stack, not by `apps`.** Split-horizon is DNS, and
    the AdGuard pair is on the UDM: putting the resolvers'
    configuration here keeps the LAN reachability requirement — the
    ZeroTier join, the host route to each instance its join installs,
    and their availability as a dependency of every CI run that touches
    the stack — off the busiest stack in the repo and on the quietest
    one. `apps` then needs no LAN access at all.
-   **No synchronizer runs between the instances.** With every
    API-settable key written to both, a synchronizer carries nothing the
    stack does not write, and it would be a conflict source: it would
    overwrite bob's configuration with whatever alice held
    ([sources-of-truth.md](../sources-of-truth.md), note N2).
-   **A rebuilt instance is set up by this stack.** An instance whose
    state directory holds no `AdGuardHome.yaml` starts in **first
    run**: it answers no DNS, serves only its setup wizard, on port
    3000, and holds no account. Only the wizard's `install/configure`
    makes an account, so `AdGuardSetup` calls it with the stack's own
    login and the listen addresses it declares, and the kinds that
    configure a running instance then write the configuration. A refreshing run reads a first-run
    instance as holding none of its resources, so its refresh drops
    them and its update re-creates them, the setup first; a plain `up`,
    which is CI's deploy, reads no instance and so cannot notice one,
    and the `ci-dns` identity reaches the API's port alone
    (physical/gateway.md §2.3). The run that sets an instance up
    therefore goes from the checkout that holds `.credentials/`
    (physical/gateway.md §3). `physical` gives a resolver machine no
    configuration file (physical/gateway.md §1), so a new or rebuilt
    instance is in first run when it first starts, and every setting it
    is configured with comes from this stack.
-   **The account is the setup's, so a rotation is a reset.** An
    instance keeps the account its setup made, and no endpoint changes
    it: rotating the login, or recovering an instance whose account is
    not the stack's, takes a reset of each instance in turn — its
    `AdGuardHome.yaml` moved aside, its machine restarted — in the
    passes physical/gateway.md §3 gives, and no `up` may record a new
    login before the last of them. A rotation recorded without the
    reset is refused at each setup, one refused login per instance, and
    nothing is written.
-   **Placement**: rewrites are emitted automatically for every row
    with a home side — `lan`, answered at the LAN VIP, or `media`,
    answered at the media VIP, with or without a public side, and a raw
    port's VIP host (§5) — so an app cannot forget its rewrite because
    it never writes it by hand. Crossing a stack boundary does not
    weaken that: an app's exposure is **plain data** in a module both
    stacks import (`kluster.conventions.routes`), so `apps` builds its
    routes from it and `dns` builds the rewrites from the same rows. A rewrite's
    answer is an address, or a name on the device plane answered as a
    CNAME, which the instance resolves through the gateway's own
    resolver without applying its rules again: a target outside
    `home.arpa` cannot be built, and no target may be a name a rule
    answers. One edit, two stack diffs, both previewable — rather than
    one edit and a second stack to remember. LAN ULA AAAAs are
    emitted alongside, and the ULA has to answer, since a client may
    dial it first (architecture.md §1.3).
    The overlay host block (§2) is rewritten the same way from the
    other census it is derived from: one rewrite per entry of
    `conventions.overlay.ROSTER`, `<label>.zt.<primary>` answered with
    the member's overlay address — never a `lan` VIP, since these names
    are hosts and not routes — and IPv4 alone, because the overlay is
    IPv4-only and the block's public record is an A.
-   **A pod reaches the portal's name where a home client does.** An
    OpenID Connect client's server calls the portal by its public name
    to finish a sign-in, since the issuer it validates is that name. The
    cluster's DNS holds no rewrite: it answers a pod's lookup of a
    Service by its name in the cluster, and passes a public name on to
    the public resolvers, Talos' defaults on the worker, which a pod at
    home reaches only through the uplink, as it does the proxy's edge
    their answer names, since the portal's row is proxied. So the portal's
    row has a home side on `lan`, without which its component refuses
    it (`conventions.sso.portal_row`), and every pod that serves a client
    carries a host alias of the issuer's name at the answer this stack's
    rewrite gives the name, the `lan` VIP in both families
    (`conventions.sso.issuer`, `kluster.components.apps.issuer_aliases`).
    The alias follows the rewrite through the cut: while the legacy VPS
    holds the portal's name, neither answers it.
-   **Where each instance is reached is derived, not configured.** The
    stack reaches the instance over plain HTTP at its address on the
    container VLAN: the API at the port
    `conventions.gateway.ADGUARD_API_PORT` names — the same constant the
    caddy vhost that proxies the instance, the setup's listen address,
    and the overlay flow rule that admits a `dns` run all meet on — and
    the setup wizard at `conventions.gateway.ADGUARD_SETUP_PORT`, the
    appliance's own. The overlay carries no route to that address: a run's join
    installs one through the gateway for itself (framework/ci.md §2).
    There is no endpoint key to set, and therefore nothing for a
    stale one to disagree with; `Pulumi.dns.yaml` carries the AdGuard
    login and nothing else about the pair. Dialing the instance's own
    public name instead would have the runner resolve a name that only
    a split-horizon rewrite answers, which is the thing the run is
    declaring.
-   **A resource is identified by its instance, not by its address.**
    `dns` declares one `ResolverConfiguration` component per entry of
    `conventions.gateway.RESOLVERS`, named after that entry and handed
    the one configuration both instances share, and every resource
    id and logical name under it is built from the entry's name —
    never from the address. Re-addressing an instance is then an
    update that writes nothing, rather than a replacement of every
    resource at the old address. The general rule is
    [style/pulumi.md](../style/pulumi.md)'s.

## 4. LAN DNS: three name planes

LAN-side naming is absorbed into the same design, split by what each
plane names:

1.  **Device plane — `home.arpa` (main VLAN) and `iot.home.arpa` (IoT
    VLAN)**: device hostnames, DHCP-derived and served by the UDM's
    resolver as today. Inherently dynamic — not Pulumi-managed; any
    *static* host entries that prove necessary go through the unifi
    provider. An alias to a device's name (`nas`) is not a static
    entry: it is a CNAME rewrite in the resolvers' rule list (§3), so
    it follows the device across a new lease, and the resolvers forward
    `home.arpa` to the UDM's resolver to answer its target.
2.  **Service plane — public-zone names, resolved via AdGuard**:
    every LAN-reachable service uses its *public* hostname
    (`<app>.<zone>`); AdGuard rewrites (§3) steer LAN clients to the
    `lan` VIP, a device holding the overlay's tunnel among them
    (architecture.md §3.4). The home-only names sit directly under the
    primary, the domain the tunnel's resolver is scoped to on a desktop
    (physical/gateway.md §2.7).
    **LAN-only services are rewrite-only**: the row yields
    the AdGuard rewrite (from `dns`, §3) and the `lan-gw` route (from
    `apps`, §5) but *no* Cloudflare record — public resolvers see
    NXDOMAIN, while cert-manager DNS-01 still issues real certificates
    for the name (challenge records don't require the name itself to be
    published). Issuance for these names is **per-zone wildcard
    certificates**, not per-name: every issued certificate lands in
    public Certificate Transparency logs, and per-name issuance would
    republish exactly the LAN-only service census that rewrite-only just
    hid. The zones the cluster issues for are its **served zones**: the
    primary, served by definition, since the cluster's own endpoints are
    names under it, and every zone a route or an apex of the exposure
    census names, held or not (`conventions.routes.served_zones`). Each has one certificate,
    for the apex and the wildcard together, from the cluster's one
    issuer, so a row in a new zone is a new certificate in the preview
    that shows the row (cluster-infra.md §2). The DNS-01 token
    cert-manager answers the challenges with is minted for the same set
    of zones ([credentials.md](../credentials.md) §3).
3.  **`lan.ucw.phd` retires.** Its two historical roles are both
    superseded: split-horizon duplicates collapse into the rewrites on
    the public names, and LAN-only names become rewrite-only names in
    the public zones (with proper TLS, which `lan.` names never had
    cleanly). The zone's entries are dropped one-by-one as each app
    migrates, like the archvps repointing. Until then each row of
    `conventions.gateway.LEGACY_VHOSTS` is answered by a rule in the
    resolvers' list (§3), its name under the zone at the proxy's
    address, and the bare label with its `home.arpa` form beside it
    where the proxy answers the bare label too; a rule leaves with its
    row. A name under the zone with no row gets the public answer,
    NXDOMAIN, rather than reaching the proxy.
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

## 5. The exposure declaration, and its one helper

> **The declaration, its helper and the records are built.**
> `conventions.routes` holds the types and the census, `EXPOSURES`, and
> the `dns`, `physical` and `k8s-base` stacks read it. `expose`, on the
> `apps` stack's application base, emits everything below. The one
> component the `apps` program wires is the echo server, the front
> door's exit criterion
> ([rfc-007](../rfc/rfc-007-k8s-base-and-apps.md) §9.2), whose row is
> published direct.

**An application has one `Exposure`, and it has two dimensions** —
the two questions deploying an application answers
(cluster/architecture.md §3.6). *Which Gateway* is three peer fields,
`internet`, `lan` and `media`, the same fields on every row where they
mean something. *How* is the row's type:

| Row | What it is | `internet` | `lan` | `media` |
| --- | --- | --- | --- | --- |
| a `Route` | HTTPS by name, in each zone it names | `Proxied`, with an optional gate, or `Direct`, with its reason | `Rewrite`, or `Bare` | `Rewrite` |
| a `Passthrough` | TLS the pod terminates, on 443, by name, in the primary | `Direct` | `Rewrite` | `Rewrite` |
| a `PortRedirect` | HTTPS on 443, by name, in the primary, answered with a redirect to the same name at a raw port of its exposure | `Direct` | `Rewrite` | `Rewrite` |
| an `Apex` | a website's apex, in each zone it names | set by the type, its address the web origin's (§2) | none | none |
| a `Port` | a raw port, at a Gateway's address | `Balancer`, with the names it is dialed at, `Dedicated` or `SiteForward` | `Vip`, with its name, or `Heard` | `Vip`, or `Heard` |

Setting a field chooses that Gateway, and the field's type carries what
only that Gateway and that way of reaching it mean, so a combination
that means nothing cannot be written: a reason to publish direct is a
value of `internet` alone, a gate a field of what `internet` takes
behind the proxy, a bare name a value of `lan` on a `Route`, a heard
side a value of a home field on a `Port`, and one Gateway on a row is
one field. The rule behind the shape is style/pulumi.md's ("Data").

**What a type cannot say is refused where the row is constructed**, so
the module holding a census that breaks it does not load: a row with no
Gateway set, and an `Exposure` with nothing in it; `lan` and `media` on
one row, since a name has one rewrite and the media VIP already answers
every home network the LAN VIP does; two `Route`s in one `Exposure`, one
on `internet` alone and one on a home Gateway alone, held or not, which
is one service under two names; a `device` beside anything but
`Route`s, held or not, since its relay forwards to one port; a heard
side on a port not served on UDP alone, since a packet is what is
heard; a `PortRedirect` whose port is no TCP `Port` of its `Exposure` that
serves every Gateway the redirect sets at the redirect's host — at the
balancer for `internet`, at a `Vip` of that host for a home side —
since its client would be sent to an address that does not serve the
port; an empty reason, an empty zone set, and a host that is empty or
`@`.

**What is a relation between rows is an invariant of the census**, held
in `tests/test_conventions.py` against the census and against a census
that breaks it: each zone a row names is declared by the installation;
a port is claimed once on each front, the Gateways' ports
(`conventions.GATEWAY_PORTS`) counted on the balancer and on both VIPs;
an apex's zone holds the web origin's address; a row is held exactly
while a legacy record holds its name (below); and **one host, one
answer** — rows that share a host publish one record and one rewrite,
so they agree on both. A raw port's `Host` shares a name only with a
row published `Direct`, since a proxied record would hide the raw port,
every home side on the name answers at one VIP, and any other shared
name is refused. The discovery server's `syncapi` is the case it
exists for: the peer port, the discovery server's `Port` and its
`PortRedirect`, one direct record, the redirect's, and one rewrite to the
LAN VIP. **The census with every `Held` taken off holds too**: a held
name publishes nothing, so two exposures that will both publish it pass
every other check until the cut, and the census tests take each wrapper
off and hold that census to one exposure per name and one answer.

**A service reachable from home and from outside has one name.** Outside
it resolves to the public address; at home the rewrite resolves the same
name to the VIP of the home Gateway its row sets, and the client never
sees the difference, which is what a rewrite is for. A row on `internet`
alone takes the public path at home under the same name. A `Route` has
one `host`, and its Gateways are fields of that row, so one name is what
a row says by default; a second name takes a second row, and the
constructor refuses the pair. No row keeps a `lan.ucw.phd` name (§4).

**A client that names a raw HTTPS port's server with no port is
answered on 443, by name, with a redirect to that port.** That is a
`PortRedirect` row beside the `Port`: on each Gateway it sets, the
Gateway ends TLS on the zone's wildcard certificate and answers every
request with a `308` to the same name at the port, path and query kept,
forwarding to nothing and asking no gate. A client certificate is
presented on the raw port's leg alone, where the pod asks for it. It is
how the discovery server keeps the address every device holds,
`https://syncapi.<primary>` with no port
([rfc-008](../rfc/rfc-008-workload-migration.md) §4.1,
[Aetf/kluster-ops#723](https://github.com/Aetf/kluster-ops/issues/723)).
The row stands in for a `Passthrough`, which Cilium cannot serve beside
the zone's wildcard listener, and becomes one when the pinned Cilium can
(the comment on its pin in `Pulumi.yaml`).

**A gate belongs to `internet`, since the home has none.** It is the
`gate` of `Proxied` — the portal's session, or HTTP Basic for a client
that cannot follow a redirect — declared on the row, so the census says
which names are gated, and a gated name's home side is answered ungated
(cluster-infra.md §2).

**A held row is the legacy VPS's until its cut.** `Held` wraps a
`Route`, a `Passthrough`, a `PortRedirect`, or a raw port's `Host` while a
record of this stack still points its name at the legacy VPS: by the
VPS's address or its anchor (`legacy.IP_ARCHVPS`,
`legacy.ANCHOR_ARCHVPS`), in an application's block of `legacy.py` or
in the web origin's pair. A held
row's route attaches to its Gateways and publishes no record and no
rewrite, and neither does a VIP host that a held row shares, so the
name keeps resolving where the legacy VPS publishes it while the new
application is verified (§6). It still counts toward the served zones,
since that verification needs the zone's certificate. The relation is
the invariant above in both directions: a row on `internet` is held if
and only if such a record holds its host in one of its zones, and a row
on a home Gateway alone publishes no record to collide with and is
never held. An `Apex` publishes no record and has no held state. The
wrapper leaves with the last such record, and the class, the branch in
each derivation that reads it, and its invariant go with it, with no
row changing shape.

**Every rewrite derives from the rows** (`rewrites.rewrites`, §3). A
row by name with a home side is rewritten to that
Gateway's VIP in each zone it is published in, and `Bare` adds the bare
label, which `lan-gw` redirects to the name. A raw port's `Vip`
rewrites its host in the primary to its Gateway's VIP, and a heard side
has no host and no rewrite: its client never dials a name, and a relay
on the host carries what it hears to the VIP
([rfc-008](../rfc/rfc-008-workload-migration.md) §4.1). Both families
are written.

**Every record but the apex's derives from the rows** too
(`conventions.routes.public_names`): a row on `internet` publishes a
CNAME to the cluster anchor in each zone it names, proxied, or direct
with the reason `Direct` carries, and a raw port's `Host` publishes one
direct, in the primary, needing no reason of its own since a raw port
cannot cross the proxy. A row published direct, and every record it
aliases, holds the chain relation of §2, and the website co-host
carries no application name of the primary's, from a row as from a
block (`tests/test_dns_records.py`). The certificates follow
`served_zones` (§4): the zones of every `Route` and `Apex`, held or
not, and the primary; a `Passthrough` and a `Host` take none, and a
`PortRedirect` is answered on the primary's.

**`expose(exposure)` on the app component base is the one helper**
(`kluster.components.apps`), and it takes the application's `Exposure`
from the census, not a host string a reader would have to match against
the census by eye — so an application publishing a name `dns` never
rewrites cannot be written. From that it emits the coherent set: an
`HTTPRoute` per Gateway each `Route` sets (`kluster.lib.k8s.route`),
`internet`'s gate on that one alone, and a redirect for a bare name at
`lan-gw`'s listener for the label; an `HTTPRoute` per Gateway each
`PortRedirect` sets (`kluster.lib.k8s.redirect_routes`), whose one rule
redirects to its port, with no backend;
an `HTTPRoute` at `internet-gw`'s apex listeners for an `Apex`;
and for each `Port`, a Service for each Gateway it sets, at that
front's addresses, with one allow for the world on its port and
transports. A held row's routes attach all the same. It refuses a
`Passthrough` by name, as the Gateways do: Cilium drops a passthrough
listener together with the zone's wildcard listener on the same port
(cluster-infra.md §2), so such a pod is reached at a raw port, and by
name through a `PortRedirect` to it (above).
For each name the exposure publishes (`public_names` over that
exposure alone) it declares one CNAME to the cluster anchor in the
name's zone, proxied exactly where its row is, through the `apps`
stack's Cloudflare provider and in the zone whose identifier the `dns`
stack exports as `zone_ids`
(`kluster.components.apps.public_records`). Each application's
exposure declares its own, so two exposures one program wires never
publish one name: the program refuses them, naming it
(`kluster.components.apps.published_once`). The records are not
`route`'s, which sees a row with its `Held` wrapper taken off and never
a raw port's `Host`; a component of another stack that publishes a row
calls `public_records` beside `route`. It never writes a rewrite, which
is the split §3 describes.

**There is one helper, because the declaration already states the
exposure.** A helper per kind would make the call a second statement of
the same thing, and a call that can disagree with its row forces the
helper to rule on which of the two wins. "IoT devices may reach this
application" is a `media` side on its row, decided once on the census
row a reviewer reads (cluster-infra.md §2, physical/gateway.md §4.2).

**A raw port's listener is `physical`'s.** `apps` holds no OCI
credential (credentials.md §3), so the balancer's listener and backend
set for a `Balancer` port, one of each per family it holds, are
declared in `physical` from the same row (physical.md §1), and the
site's forward and pinhole for a `SiteForward` port likewise
(physical/gateway.md §4.2); adding such a port is a census row and a
`physical` apply before it is a Service in `apps`.

## 6. Migration shape

Per-app cutover falls out of the anchor design: an app's records point
at `archvps.hosts` until the app is cut, then its component declares
the same names against `kluster.hosts`, and its `legacy.py` block is
deleted (below). DNSControl holds no entry by then: Aetf/dns retired
at the zones' cutover, before any application migrates.

**An application moves in two pull requests, its land and its cut**
(cluster/migration.md §0 rule 1). Verification has to run against what
is deployed, and a merge is what deploys, so one merge cannot both
deploy the application and give it the name.

-   **The land** wires the component into the `apps` program and adds
    its rows to `conventions/routes.py`, a row on `internet` held
    (§5): the route attaches to the Gateways while the name still
    resolves where the legacy VPS publishes it, and the application is
    verified at the cluster's front door. A home-only row publishes no
    record to collide with, so it lands published, and where a rule on
    the resolvers' declared list already rewrites its name, the land
    deletes that rule, so the row's rewrite is the only one.
-   **The cut** deletes every legacy declaration of the name — its
    block in `legacy.py`, the web origin's address for an apex, a rule
    on the resolvers' declared list, a row of the gateway proxy's
    `LEGACY_VHOSTS` — and takes the row out of `Held`, so `apps`
    publishes the record and `dns` the rewrite. The record changes
    hands in one run of the deploy chain: `dns` deletes before `apps`
    creates (§1). A window's applications cut together, in one pull
    request.

**`legacy.py` is one block per application**, because the unit of every
remaining edit to it is a cut: each block is deleted whole when the
application named in its header is cut, and the module empties block by
block through Wave F, with no row of a shared tuple to pick out. A block whose
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
retired with a pointer commit at the zones' cutover and is archived
(the old-tracker rule, migration.md §0; sources-of-truth.md, row R1).
