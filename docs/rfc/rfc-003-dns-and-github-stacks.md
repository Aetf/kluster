# RFC 003: The `dns` and `github` Stacks Under the Style Rules

*   **Status:** Accepted, 2026-09-03. Nothing below is built; the slices of
    §19 are cut from this text.
*   **Created:** 2026-08-29
*   **Updated:** 2026-09-03, revised under review. §4 inverts the record
    tables: the unit is a block of records naming the zone set it appears in,
    and the per-zone view the provider takes is derived. §5 is new — nothing
    mirrors, so the fan-out set retires, the copies it produced are dropped,
    and a public record is published only in a zone that answers for the name.
    The two zones nothing of ours serves are parked: what resolves in them is
    copies of the primary's records, which retire with the machine they
    address (§5.4). §6 gains the field that keeps an
    application's own records in its census row, and the seam test that
    compares record keys. §1 and §19 say that the `dns` stack's state is
    imported rather than applied and that the retiring DNSControl program is
    authoritative until cutover. §11 records the settled vocabulary, §20 is
    new, and §§6–19 are renumbered from §§5–18.
*   **Updated:** 2026-09-04. §4.2's sketch of `base.py` writes `MAIL_ZONES`
    unqualified, because §5.2 places the set beside the one block that names
    it rather than in `conventions`, where every set a second program agrees
    on lives.
*   **Authority:** the style rules (`docs/style/`) and the design documents are
    what this document obeys. Where they are silent, a rule proposed here is
    marked **new rule**.
*   **Companion:** [rfc-002](rfc-002-src-layout-and-the-gateway.md) is the
    shape, and it is also where most of what is applied here was decided. Its
    precedents are cited at each use rather than re-argued, and its
    measurements are cited rather than re-run.
*   **In scope:** the internal organization of the two stacks rfc-002 moved
    into the new tree and otherwise left alone — the `dns` record censuses and
    the shape they are written in, what each zone is for and which zones a
    record is published in, the route model, the AdGuard rewrite provider, the
    forge's declarations, both stack programs' configuration reading and shape,
    and the vocabulary of both.
*   **Out of scope:** the records this document keeps — nothing is added and
    nothing is repointed, and the rows it drops are enumerated in §5.3; the
    `apps` stack's route helpers (dns.md §5), of which this document fixes only
    the census they consume; the shared test machinery; and the `k8s-base`
    stack, which is unwritten.

--------------------------------------------------------------------------------

## 1. Context & Problem Statement

rfc-002 rebuilt the `physical` stack around the style rules and left the other
two implemented stacks where they were: they moved into the new tree with the
rest and are otherwise as they were written. What that leaves is not a defect a
test catches. It is a set of small disagreements with rules the rest of the
repository now follows — a census in a package that no longer matches where the
census rules put it, a dynamic provider carrying its credential as a resource
input, a stack program that declares every resource itself, and one word this
repository has retired everywhere else.

It also leaves one thing that is not a matter of style. The `dns` tables are
written the way Cloudflare's API takes them, one entry per zone, so a set of
records that four zones share is a tuple four entries splice in and two of the
six zones are not in the dict literal at all. Nobody can answer "what does this
zone carry" without executing the code, and nobody can answer "where does this
block appear" without reading every entry (§4).

**One document for two stacks**, because the two halves apply the same handful
of rules and the second half is small: the `github` stack is one program
declaring two repositories' worth of settings against one account. Splitting it
into a document of its own would mean restating §3's census rule, §13's
provider rule and §19's sequencing to say four things with them. What the two
halves do not share is sequencing, so §19 keeps them apart there.

**Both stacks here have state, and the stack rfc-002 rebuilt had none.** `dns`
holds the six zones, their DNSSEC settings and their records; `github` holds
what was applied on 2026-08-25 (github.md §2). A logical name or a parent that
moves in either stack is a replacement of a live resource, and the zones and
both repositories carry `protect`, so such a replacement is refused rather than
performed — the same trap github.md §3.1 records for an unparented import.

**The `dns` stack's state was imported, not applied, and it is not yet the
source of truth.** At Cloudflare's authoritative servers the primary zone
answers a set of CAA values the census does not declare, the other public zones
answer none, the cluster anchor does not resolve, and `unlimitedcodeworks.xyz`
carries neither the overlay host block nor the copy of the legacy VPS anchor
that the declaration gives it. The retiring DNSControl program
([Aetf/dns](https://github.com/Aetf/dns)) still owns these zones. So this
stack's first `up` is not merely unrun, it is **not to be run** while that
program is authoritative, or the two write over each other. Two things follow,
and they run through §19: the first `up` is a cutover step and no slice's
done-condition, and the imported state is disposable — if it is in the way, the
zones are deleted from state and imported again.

That still leaves "does this move a live URN" a question every slice below has
to answer rather than a property either stack grants for free:

*   **The `dns` renames do not move one, by construction.** A resource's URN is
    its type, its parent and its logical name, and none of the three moves.
    `ManagedZone` stays in `zone.py` under its own name, so the component's
    type token is untouched, and the module renames of §11 — `zones.py`,
    `model.py` and `adguard.py` — carry no resource class between them. The
    zone and record logical names are built from the zone name and the record's
    state key, neither of which changes, and the published `zt` label stays on
    the wire (§11). The rewrites are the one place a logical name changes
    (§8.3), and there is no rewrite in state to move: the route census is
    empty. **Still a done-condition, not an assumption**: each `dns` slice is
    finished when its preview shows no replacement and no delete it did not
    intend, and any rename found to move a live URN either ships an `aliases`
    option or is dropped.
*   **The `github` component does move URNs** (§14), because introducing a
    parent changes every child's. That slice ships aliases, and its
    done-condition is the same preview.

--------------------------------------------------------------------------------

## 2. What is inherited, and what is decided here

Everything in the left column is settled; this document only says where it
lands in these two stacks.

| Precedent | Applied here |
| --- | --- |
| rfc-002 §8.1 — every provider explicit, credential read at the line that builds it | the forge provider, which is ambient today (§13); the Cloudflare provider's namespace (§9) |
| rfc-002 §7.4 — a dynamic provider carries no connection state | the AdGuard rewrite provider (§8) |
| rfc-002 §5.3 — a census is a table; a declaration is what a component takes | the record blocks (§4), the route census (§6) and the rewrites (§7) |
| rfc-002 §10.1 — related constants are one structure | the forge census (§12) |
| rfc-002 §10.3 — account facts are conventions, account secrets are configuration | the Cloudflare account (§9) |
| rfc-002 §12 — one shape per role, no `declare_*` | `declare_rewrites` (§7), the `github` program (§14) |
| rfc-002 §3 — one term per concept, descriptive over metaphorical | the vocabulary of §11, and the zone sets of §5.2 |

--------------------------------------------------------------------------------

## 3. Where a census lives

Two rules meet on the `dns` tables, and read alone the first is easy to
over-apply:

*   style/pulumi.md: *a census lives where it is decided: conventions or stack
    configuration; a component receives the census it acts on.*
*   declarative/README.md §2: *a table two stacks decide from belongs in
    `conventions` even when only one of them declares resources for it* — the
    overlay roster being the case, admitted by `physical` and published by
    `dns`.

The second is the discriminator, and the first is about the failure it exists
to prevent: a component that keeps the roll inside itself and then accepts a
mapping it ignores. A table one program decides *and* declares from, handed
to its component as a parameter, breaks neither rule by living in that
program's own area.

**New rule.** The discriminator is stated once, for both directions:

> A census that more than one program must agree on lives in `conventions` —
> whether the second program is another stack or a script, and even where the
> second declares no resource from the table. A census one program decides and
> declares from lives in that program's own area, as data beside the component
> that receives it, never inside that component.

The extension is the word *program*. `conventions` is the only home a stack
and a script can share: a script may import `conventions` and `lib` and
nothing else (AGENTS.md's import contract), so a table both a stack program
and the credentials command must agree on has exactly one legal home, and
today the forge census instead has two copies held equal by a test (§12).

**What decides is how many programs must agree, not which program derives from
the table.** That distinction is the one this rule is easiest to get wrong on,
and §6.4 is where it bites: a record only the `dns` stack turns into a resource
still belongs in the shared census if both stacks have to know it is published.

Applied to every table in these two stacks:

| Table | Decided by | Home |
| --- | --- | --- |
| the base record blocks | `dns` alone | `components/dns/base.py` (§4) |
| the app records still on the VPS | `dns` alone | `components/dns/legacy.py` (§4.5) |
| the CAA issuer sets | `dns` alone | `components/dns/base.py` (§4.4) |
| the application routes | `apps` and `dns` | `conventions/routes.py` (§6) |
| the AdGuard endpoints | four declarations | `conventions/gateway.py` (§8.3) |
| repositories and Environments | the `github` stack and the credentials command | `conventions/forge.py` (§12) |

So the record tables stay where they are, and the two tables a second program
reads move. The record tables are the interesting half of that verdict, and §4
gives the argument in full rather than leaving it to the table above.

--------------------------------------------------------------------------------

# Part A — the `dns` stack

## 4. The record tables

### 4.1 They stay in the `dns` area

`BASE_RECORDS` and `LEGACY` are censuses by any reading: one row per record,
read top to bottom, asserted on by tests. They stay in `components/dns/`
because no second program decides anything from them. `apps` publishes CNAMEs
to an anchor whose *name* is a convention (`conventions.ANCHOR_CLUSTER`) and
never reads a row of these tables; `physical` reads neither. What crosses is
the zone list, the zone sets and the anchor labels — and those are already in
`conventions.dns`, which is exactly the split this document ratifies.

The content is the other half of the argument. A DKIM public key, a site
verification token and a mail exchanger are not decisions this repository
makes; they are facts about services outside it, transcribed. `conventions` is
where a value *this program must agree with itself about* lives, and inflating
it with 300 lines that one program reads once would make the layer every
script imports the home of this installation's mail configuration.

**What the rule does require is already true**: `ManagedZone` receives the
records it declares and holds none of its own, and the stack program is what
hands them over.

### 4.2 The unit is a block, and the per-zone view is derived

Where the tables live is settled by §3. Which way round they are written is
the defect, and it is what makes a zone unreadable.

Today `zones.py` is a mapping from zone name to what that zone carries. A set
of records four zones share is a tuple those four entries splice in; two of the
six zones are absent from the dict literal entirely and arrive through a loop;
the CAA rows are appended by a comprehension outside every per-zone function;
and the same pair of zones is spelled a second time as a literal in
`legacy.py`. On top of that the stack program assembles the final answer in a
comprehension with two conditional arms — the overlay block *if the zone is a
mirror*, the anchors *if the zone is the primary* — which are design decisions
from dns.md §2 sitting in the wiring, and which the tests rebuild beside it in
order to assert on them.

**The unit becomes the block: records that appear together, in every zone of
one set.**

```python
@dataclass(frozen=True)
class Block:
    """Records that appear together, in every zone of one set."""

    zones: tuple[str, ...]
    records: tuple[Record, ...]


def zone_records(zone: str, blocks: Iterable[Block]) -> tuple[Record, ...]:
    """What one zone carries: the provider's per-zone view, derived."""
    return tuple(
        record for block in blocks if zone in block.zones for record in block.records
    )
```

The zone set belongs to the block rather than to the record, which is the
illegal-states rule applied to data: a mail block in which one exchanger had
wandered into a different zone set is a state a per-record field would make
writable, and a block header does not.

`base.py` is then a table read top to bottom, one row per block, with the zone
set in the first column:

```python
BASE_RECORDS: tuple[Block, ...] = (
    # The zones that answer for a website: the apex and www, served by a web
    # server rather than by an app.
    Block((*conventions.WEB_ZONES, *conventions.PARKED_ZONES), (
        a('@', legacy.IP_ARCHVPS, proxied=True, comment='web origin; repoints to kluster.hosts at migration'),
        cname('www', legacy.ANCHOR_ARCHVPS, proxied=True),
    )),
    # The overlay host block, one record per roster member.
    Block(conventions.PRIMARY_ONLY, overlay_records()),
    # The mail zones: the exchangers, SPF, the in-cluster DKIM key and DMARC,
    # identical in both.
    Block(MAIL_ZONES, WORKSPACE_MAIL),
    # What only the primary carries of its own: its Workspace DKIM key and its
    # site verifications.
    Block(conventions.PRIMARY_ONLY, (...)),
    Block(('unlimitedcodeworks.xyz',), (...)),
    # jiahui.id: a Google Site, mail forwarded by the registrar. Nothing of
    # ours in it.
    Block(('jiahui.id',), (...)),
    # jiahui.love: everything is the apex; the labels alias it, so a repoint is
    # one record.
    Block(('jiahui.love',), (...)),
    # CAA: per zone, by who issues for it. One block per issuer set.
    *caa_blocks(ZONE_ISSUERS),
)
```

**`ManagedZone` is untouched.** It still takes the records of one zone.
Cloudflare's API is per zone, the component is generic, and a component that
took the blocks and filtered them itself would be exactly the failure §3's rule
names — receiving a table it then ignores most of. The inversion is resolved by
`zone_records` in front of the component, not inside it.

The stack program keeps its loop and loses both conditional arms:

```python
blocks = base.blocks(anchors=_anchor_addresses(physical))
zones = {
    zone: ManagedZone(zone, zone=zone, account_id=account,
                      records=zone_records(zone, blocks), opts=on_cloudflare)
    for zone in conventions.ALL_ZONES
}
```

**No state key moves.** A record's URN is the zone plus `{zone}-{key}`, and the
key comes from the record constructors: the inversion changes which Python
structure a row sits in and nothing about the row. Registration order is not
part of a URN, so the reordering costs nothing. Three places a builder could
move a key without meaning to, which the slice's brief names: the site
verification keys are positional, so each zone's tokens keep their order; the
per-zone Workspace DKIM record keeps `key='dkim-google'`, because a tidier
`dkim-google-ucw` would be a replacement; and the anchor constructors change
file without changing their calls.

`base.py` imports `legacy.py` for the web origin's address and the legacy
anchor's name, which fixes the import direction between them as well: those two
values move into `legacy.py`, the module that empties at Wave F, and `base.py`
reads them from there. Today the direction is the other way round, and the
address of the thing being retired lives in the module that outlives it.

**New rule**, which is the census rule stated for the shape of a table rather
than for its home:

> A census is declared in the terms of this installation, not of the provider
> it is pushed to. Where its natural statement is "these records, in these
> zones", the unit is the block and the named zone set, and the per-member form
> the provider takes is derived by one function.

It is the third instance of the same principle in this repository, after the
overlay roster — one entry per member, not one per network object — and
`Exposure`, which says what an application's reachability *is* rather than
which two resources it produces.

### 4.3 The blocks, category by category

The zone sets in the second column are §5.2's.

| Block | Zone set | Form |
| --- | --- | --- |
| web origin | `WEB_ZONES` and `PARKED_ZONES` | block |
| overlay host block | `PRIMARY_ONLY` | block, derived from the roster |
| Workspace mail | `MAIL_ZONES` | block; the two zones are identical, which a per-zone parameter blurred |
| Workspace DKIM key | one zone each | zone-specific: one key is issued per domain, so it is the one mail record that is per-zone by nature |
| site verifications | one zone each | zone-specific, as singleton blocks |
| the two family zones | one zone each | zone-specific |
| CAA | by issuer set | zone-first, and stays so (§4.4) |
| cluster anchors | `PRIMARY_ONLY` | block; the shape moves into `base.py` and the program supplies three addresses |
| legacy VPS anchor | `PRIMARY_ONLY` | block, in `legacy.py` |
| the names the VPS still serves | `PRIMARY_ONLY`, one block per application | block, in `legacy.py` (§4.5) |
| the website co-host's own names | its own zone | block, in `legacy.py` |

A singleton block — a zone set of one — is how a per-zone row is written in the
same grammar rather than in a second one. The family zones are singletons for
that reason and not because they are special: `jiahui.love` puts everything at
the apex and aliases it with labels, so its `www` targets its own apex rather
than the web origin's anchor, and it shares no block with anything.

### 4.4 CAA stays zone-first

A zone's CAA set says which certificate authorities may issue for any name
under its apex, and who those are is decided per zone by what the zone is used
for. It is the one table where "which zones" is the *answer* rather than the
premise, so `ZONE_ISSUERS` stays a zone-keyed table and `caa_blocks` groups its
rows by issuer set: the CAA row then reads like every other row of §4.3 while
the decision stays where it is made. Inverting it would work today only because
the five pinned zones agree, and a second issuer set would send the next
builder straight back to a per-zone table. The cost is two grammars in one
file, and it is the honest one.

**It cannot be derived from the zone-set vocabulary either.** The sets of §5.2
group zones by what serves them, and issuance does not follow that grouping —
the family pair holds one zone that takes the edge's whole partner set beside
one pinned to nothing at all. Nor can it be derived from the records: the edge
issues for the zone rather than for a record, minting an apex and a wildcard
for every zone it hosts, proxied or not; and the names the cluster and the
gateway issue certificates for carry no records at all by design, because
LAN-only names are rewrite-only precisely to keep them out of public DNS
(dns.md §4). A union taken over the records would omit exactly the issuers
whose renewal then fails silently.

**The first apply writes this installation's first CAA records.** Not one of
the six zones holds a CAA record of its own: the values the primary answers
with are the edge's, injected into the response and absent from the record set,
and the other five answer nothing. The retiring DNSControl program never
declared CAA either, so nothing of ours ever put a value there. There is
therefore nothing to import and nothing to collide with — eight creates on the
primary, and eight on each of the other pinned zones.

**The declared set is narrower than what the primary answers, deliberately.**
The injected values name `comodoca.com` and `digicert.com`; the declaration
names neither. Dropping them is checked rather than assumed: Certificate
Transparency holds no certificate from `digicert.com` for any of the six zones,
and `comodoca.com` is the former issuer domain of the authority the declaration
already names as `sectigo.com`, which still honors the old value.

**A zone's set is what serves it, and one zone has a second stage.** The two
parked zones (§5.2) serve nothing of this installation's and keep the edge's
set anyway: they stay on the edge's DNS, and the edge mints an apex and a
wildcard for a zone it hosts, proxied or not, so the set authorizes the
certificate that is actually issued. `ucw.phd` has a standing
reason of its own on top — it is the zone the gateway's proxy holds
`*.lan.ucw.phd` under (dns.md §4) — and it costs the zone no extra row, because
`letsencrypt.org` is a member of the edge's set and one set authorizes both.
**The second stage arrives when a parked zone leaves the edge**, not when its
last record does: `ucw.phd`'s set becomes `CLUSTER_ISSUERS` — `letsencrypt.org`,
with `issuewild` the tag that matters, since the gateway asks for the wildcard
alone and never the apex — for as long as the gateway's ACME scope names the
zone, and none afterward; `peifeng.phd` has nothing of ours issuing for it and
would carry no CAA at all, by the same rule that leaves the Google Site's zone
unpinned. Nothing in this document reaches that stage.

**What keeps the table from going stale is not a test.** The pinned edge set is
a copy of somebody else's list, and nothing in this repository can see that
list; a test that fetched it would put a network call in the gate. What
notices a partner authority this repository does not authorize is the edge
itself, whose Universal SSL notification reports a validation or renewal
failure before the certificate expires — the one guard here that is not a human
remembering, and it is enabled on the account. Two invariants can be held offline and are worth a test: every
zone with a proxied record carries the edge set, and every zone in the
gateway's ACME scope authorizes `letsencrypt.org` for `issuewild`. The
remainder — a third party moving certificate authority under a zone this
repository does not serve — is a periodic read of Certificate Transparency per
zone, which belongs in each milestone's review checkpoint beside the
documentation audit, written as a procedure in dns.md.

### 4.5 `legacy.py` is one block per application

`legacy.py` holds the names the retiring VPS still serves, and every row in it
is deleted when its application migrates and declares the same name against the
cluster anchor (dns.md §6). Today it is three tuples keyed by which zones a row
appears in, with the application named informally in a comment column. The unit
of every remaining edit to the module is an application — migration.md's waves
are enumerated by application — so the module is cut **one `Block` per
application**, each deleted whole at that application's migration. The comment
column becomes structure, and the module empties block by block through Wave F.

A migration is then three files in one pull request:

```diff
 # components/dns/legacy.py
-    # immich -- Wave C
-    Block(conventions.PRIMARY_ONLY, (cname('photos', ANCHOR_ARCHVPS, comment='immich; unproxied for large uploads'),)),

 # conventions/routes.py
+PHOTOS = Route('photos', exposure=Exposure.SPLIT, proxied=False)

 # components/apps/immich.py
+        self.public_route(conventions.routes.PHOTOS)
```

**The set the old tuples were keyed by never exists.** The DNSControl program
this replaces declared the primary zone once and copied its application names
into two of the other zones with a loop; those copies are dropped rather than
carried (§5.3), so there is no surviving set of zones the VPS published a name
in, and every application block that remains is the primary's own. The website
co-host's three names are its own and always were.

**The cut names the rows no application owns**, which is what it is for: as
rows in a sixteen-row tuple they were invisible, and as blocks with no
application in the header they are obvious. Six of them — `login`, `k8s`,
`test`, `files`, `mcmap` and `archvps.stats` — are no longer used and are
deleted from the module. Each deletion unpublishes a name, so the slice states
them as deletions in their own right rather than as tidying.

Two more keep their blocks, because a block with an owner only wants an address
while a block with none is a drop at the census. `mon` is the monitoring
dashboard and is in use. `bt` answers nothing on the VPS today, and is kept for
the other half of the same reason: a component will declare the name after
migration. Both carry their owner in the comment. Where `mon` lands follows
from a classification the monitoring design owes an answer to (§20), and the
ordering is easy either way, because monitoring is rebuilt fresh in Wave B
rather than migrated — the name's new home exists before its old one is
deleted.

### 4.6 The apex is the exception the anchor rule needs

dns.md §2 states that IP literals exist only under the anchor namespace, and
the census breaks it in two places. The web origin every website zone carries
is an apex `A`, and so is `jiahui.love`'s; **a zone apex cannot be a CNAME**, so
a name at the apex is either an address or nothing. And `jiahui.id`'s apex and
`www` both address a site served entirely outside this installation, which
publishes no name of ours for them to alias.

Neither is an oversight and neither can be fixed, so the rule gains the two
clauses it is missing: an address may sit outside the anchor namespace where
the name cannot be an alias, and where there is no name of this installation's
to alias. That lands in dns.md §2, and `base.py` says the same beside the
literals. Nothing in the census changes.

### 4.7 The duplicate-key refusal stays

`ManagedZone` raises when two records in one zone share a state key, and a test
holds the same invariant over the declared tables. rfc-002 §10.2 retired
exactly this kind of doubling for the roster, on the ground that static code
cannot break at runtime an invariant a test already checks.

It stays here, and the difference is which side of the boundary the check is
on. The roster's validation was a function over one table this repository owns.
This is a component's refusal about the argument it was handed, and the
component is generic: it declares whatever records it is given, and it has no
way to know its caller's table is under test. The test covers the census; the
refusal covers the component. Neither makes the other redundant. The inversion
of §4.2 makes the refusal cover one case more, since two blocks naming
overlapping zone sets can now collide where two entries of a per-zone mapping
could not.

--------------------------------------------------------------------------------

## 5. What each zone is for

### 5.1 Nothing mirrors

The design this stack inherited treats four zones as mirrors of one another:
they share a block of base records, and an application name fans out across the
set by default. Measured against what the zones actually answer with, the
premise does not hold.

*   **`peifeng.phd` and `ucw.phd` serve nothing of this installation's.**
    Every name in either zone is a copy of one of the primary's, published by
    the retiring DNSControl program and addressed at the legacy VPS. A request
    for any of them reaches that machine's catch-all, which answers with a
    redirect to the primary — so the zones behave, and none of the behavior is
    theirs. No certificate has ever been issued at the origin for a name in
    either: the only certificates naming those zones are the edge's own and the
    gateway's `*.lan.ucw.phd`. Every application name published in the two
    zones is dead, because what answers it is the catch-all rather than the
    application. Neither zone is referenced anywhere outside this stack's own
    declaration and its test fixtures.
*   **`unlimitedcodeworks.xyz` answers for a website and nothing else.** Its
    apex and `www` serve the same site, from the same instance, as the
    primary's. It carries none of the application names, has never carried
    them, and holds three names of its own.
*   **The applications could not be mirrored even with DNS and certificates in
    place.** Every forward-auth application shares one SSO cookie domain and
    one portal URL; every OIDC application registers its redirect URIs against
    a hostname; several hold an absolute origin of their own; and the matrix
    server's own name is immutable once it has federated. A second hostname for
    any of them is a login that loops or is refused. The names with no
    structural objection to a second zone are the website's, and they are
    already published in both zones that serve it.

**So no zone is a mirror, and the fan-out set retires.** A route's zone set
becomes what serves the application, and the default is the primary alone.

This reverses the earlier ruling that `unlimitedcodeworks.xyz` would become a
real mirror and gain application names as applications migrate. Under it every
migration would have published a working name in the primary beside a broken
one in the mirror — permanently, for every application, into a zone with no
cookie domain of its own, no registered redirect URI and no application-side
origin. The zone is a website co-host instead: its apex and `www`, its two game
names, its own mail and its own site verifications, and no application names at
all. Whether it is kept at all is a larger question than this document's, since
it carries live mail (§20).

**New rule**, for dns.md §2, replacing the bullet that says membership of the
fan-out set means full mirror:

> A public record is published in a zone only where a listener and a
> certificate answer for the name. A route's zone set is what serves the
> application, and the default is the primary; a name that resolves is a
> promise the origin has to keep.

The alternative was to declare the wider fan-out and defer the serving
question. Deferring it does not defer the cost — it *is* the cost: an origin
certificate per zone, a listener per zone, a separate login per zone because
sessions are per cookie domain, a second registered redirect URI per OIDC
application, and an absolute-origin decision for every application that stores
one. All of it for names no configuration in any repository references, and
until the work is done a mirror name is a link that resolves, presents a valid
certificate, and then serves the wrong thing.

### 5.2 The zone sets

Every block and every route row is declared against the same words:

| Name | Meaning |
| --- | --- |
| `PRIMARY_ONLY` | the primary alone: every route's default, both anchor blocks, the overlay block, and every application name the VPS still serves |
| `WEB_ZONES` | the zones whose apex and `www` are served: the primary and the website co-host |
| `PARKED_ZONES` | the two zones this installation holds and does not serve: what resolves in them is copies addressed at the legacy VPS, and when those retire each carries only the CAA set §4.4 leaves it |
| `MAIL_ZONES` | the two Workspace domains; it lives beside the one block that names it |
| `ZONE_FAMILY` | the two family zones — taxonomy only, so that `ALL_ZONES` reads; no block names it |
| `ALL_ZONES` | every zone the stack declares, which is what the program loops over |

The web origin block names `WEB_ZONES` and `PARKED_ZONES` together, because
all four zones carry the same apex and `www` and something answers for all
four. The sets are separate because what answers differs and one of the two is
retiring: the served pair is answered by the website, the parked pair by the
legacy VPS's catch-all, which goes with the machine (§5.4).

§5.1's rule holds without exception either way. What it forbids is publishing a
name nothing answers for, and the names in the parked zones that nothing
answers for are exactly the ones §5.3 drops.

**Parked is a state, not a stage.** `PARKED_ZONES` does not name zones on their
way out. What the set says is that nothing of this installation's has ever
served them: the names that resolve there are copies of the primary's, and they
retire with the legacy VPS they address. `ucw.phd` outlives all of them — it is
where the gateway's proxy holds `*.lan.ucw.phd`, so its CAA is load-bearing for
the gateway's renewals long after its last record goes (§4.4); `peifeng.phd` is
held because a domain in hand is cheaper than a domain reacquired.

Three names retire. `PUBLIC_ALL` retires as a fan-out set, since nothing
legitimately fans out to four zones; `ZONE_MIRRORS` retires because a
primary-excluding set is declared against by nothing; `ALIAS_ZONES` retires
with the loop that was its only job, the one that kept two zones out of the
dict literal. In prose "mirror", "full mirror" and "alias zone" go with them:
zones differ by what serves them, and the sets say which. The trap the old
vocabulary created — one zone in the fan-out set for base records and absent
from it in application names, visible only by noticing which tuple a function
handed it — stops existing rather than being documented.

### 5.3 What is dropped, enumerated

This is the one place this document changes what a zone carries, so the change
is a list rather than a principle:

*   **The overlay host block becomes `PRIMARY_ONLY`.** Its copies in the two
    parked zones are deleted, and the copy the declaration would create in
    the website co-host is not created. Nothing anywhere references a copy —
    every overlay name used in a configuration file names the primary's — and
    private addresses in public DNS are published once instead of three times.
*   **The copy of the legacy VPS anchor becomes `PRIMARY_ONLY`.** The copies in
    the parked zones are deleted and the one in the co-host is not created.
    Nothing targets a copy: the legacy CNAMEs all name the primary's.
*   **The application names in the two parked zones are deleted** — sixteen
    labels and the identity SRV that rides with them, in each zone. They are
    dropped in one change rather than one at a time as their applications
    migrate: what authorizes the deletion is the measurement in §5.1, which is
    a single finding about both zones, and spreading it across the whole
    migration would make each wave re-derive it. A migration afterward is one
    delete and one create, in one zone.

Nothing else is added, dropped or repointed by this document. The first two
items are also creates the first `up` would otherwise perform, so they land
before it (§19).

### 5.4 What the parked zones carry, and when it goes

There is nothing declared at the edge for either parked zone — no ruleset, no
rule, nothing but records. What resolves in them is copies: the apex, `www`,
the sixteen application labels, the overlay block and the legacy VPS anchor,
all published by the retiring DNSControl program and all addressed at the same
machine. **The redirect a visitor gets is that machine's own catch-all, not the
edge's.** The proxy rewrites the `Server` header on everything it passes
through, so a response carrying the edge's name says nothing about where the
response was made; every observation of these two zones is the origin
answering and the proxy relaying it.

So the parked zones need no special treatment, and they get none. Their
contents go the way every other legacy record goes:

*   **The application labels are dead** — no application answers them, only the
    catch-all — and §5.3 drops them in one change rather than one per wave,
    because the measurement that condemns them covers both zones at once.
*   **The overlay block and the anchor copy** become primary-only in the same
    change (§5.3): nothing references a copy.
*   **The apex and `www` are the only names left doing what they were published
    to do**, and they retire with the machine they address. Wave F's check —
    that nothing still references the legacy VPS anchor — is what removes them,
    on the plan §4.5 and dns.md §6 already carry. Neither is repointed at the
    cluster, because there is nothing in either zone for the cluster to serve.

**The end state is that both zones are dark**, and it is worth saying before it
happens rather than after: an old link that used to land on the primary's front
page stops resolving rather than arriving somewhere else. Nothing in any
repository, configuration file or certificate references either name, so
nothing visibly breaks — but a redirect that answered is not the same as a name
that does not exist. Each zone keeps the CAA set §4.4 leaves it, and nothing
else. That is what `PARKED_ZONES` names: held, and not served, which dns.md
states because the records alone do not carry it.

--------------------------------------------------------------------------------

## 6. The route census

### 6.1 It moves to `conventions`

`ROUTES` is the definition of a table two stacks decide from: `apps` builds
HTTPRoutes and public records from a row, `dns` builds the split-horizon
rewrites from the same row, and the whole point of the design is that one edit
produces both (dns.md §3). It lives in `components/dns/routes.py` today, which
means the `apps` area reaches into the `dns` area for the rows it authors.

`Route`, `Exposure` and `ROUTES` move to `conventions/routes.py` and are
re-exported from `conventions`, like most of that package's surface. The
module is `routes` rather than an addition to `conventions.dns` because a route
is an application's exposure — which gateways serve it, in which zones — and
DNS is one of the two things read out of it.

`Rewrite` and the `rewrites()` function that derives one set from the other do
**not** move. Only `dns` derives rewrites, so the derivation lives beside the
component that declares from it (§7) — which is also where it goes: after the
move `components/dns/routes.py` would hold no `Route`, so the remnant is
absorbed into `rewrites.py` rather than left in a module named for what it no
longer holds.

### 6.2 The zone set is what serves the application

`Route.zones` defaults to `conventions.PRIMARY_ONLY` (§5.1). A row names any
other zone only by saying so in the open, and there are two kinds of deviation:
a name whose owner wants it in one particular other zone — the co-host's game
names are the case today — and a rewrite-only LAN name, where the zone decides
which wildcard certificate covers the name, so a LAN-only name fanned across
four zones would buy four wildcards for a name no public resolver answers. What
the LAN-only helper's default zone should be is the `apps` stack's question and
not settled here (§20).

### 6.3 The exposure model is ratified

`Exposure` is one field carrying two facts — whether a public record is
published, and which VIP answers on the LAN — and the obvious critique is that
it should be two fields. It should not, and the reason is the illegal-states
rule: two independent fields admit "no public record and no LAN answer", a
route that publishes nothing anywhere, and there is no such thing. The enum
makes that state unwritable, and `public` and `lan_side` read the two facts
back out.

One combination the enum cannot express is worth recording so that it is not
rediscovered as a bug: **a public name answered by the media VIP**. Today
`IOT` implies LAN-only, because the IoT VLAN reaches the media gateway alone
(cluster-infra.md §2) and dns.md §5 offers `iot_reachable` on the LAN-only
helper only. If an application ever needs both, the answer is a fifth value and
not a split into two flags — the state that must stay unwritable is unaffected
by adding a value and is reintroduced by splitting the field.

### 6.4 An application's own records live in its route row

A few applications publish more than a name. The matrix server publishes an
identity SRV beside its CNAME, and a verification token could be next. Those
records belong in the route row, in a field for what is published beside the
name and derived by no helper:

```python
MATRIX = Route('matrix', proxied=False,
               extras=(srv('_matrix-identity._tcp', priority=10, weight=0,
                           port=443, target=SELF),))
```

**Not beside the component**, which is where the reasoning "`dns` derives
nothing from an SRV, so no second program has a claim on it" would put them.
That reads the census rule off the wrong noun: what decides where a census
lives is how many programs must agree on it, not which program derives from it
(§3). A record declared beside its component is a published record that lives
in neither the census nor the `dns` tables and can be found only by reading the
components, and "what is in this zone" would gain a fourth place to look. With the field in
the row it stays at three files: `base.py`, `legacy.py` and the route census.

*   **The component declares it through the same one line every application
    writes**, `public_route(conventions.routes.MATRIX)`. The renderer emits the
    CNAME and the extra records together, with `SELF` resolving to the row's
    own host, so the name is spelled once.
*   **The component still owns what the application serves.** The `.well-known`
    documents the SRV pairs with are served, not published; they stay with the
    application. The census carries what DNS publishes; the component carries
    what the application answers with. That is the line between them.
*   **Not a literal in the `dns` tables either.** The SRV's target is the
    application's own hostname, so the row's host would be spelled a second
    time in another stack, and at migration the application's DNS would become
    a delete here, a move there and a row — co-location broken exactly where
    dns.md §6 promises it holds.
*   **The field is empty on every row but one**, and that row is one a reader
    has to open anyway to learn that the application publishes more than its
    name. What it costs is that a port and a service name sit in the census
    rather than in the component, one hop away — the same hop the CNAME already
    costs.

**The row states the record and the renderer builds it.** `Record` is the `dns`
area's model of what the provider takes, and `conventions` sits below
`components` in the layering contract, so a route row cannot hold one. That is
not only the contract talking: it is §4.2's new rule applied to itself — the
row says what is published, in this installation's terms, and the renderer in
`apps` turns it into the provider's shape. Moving `Record` down into
`conventions` was the alternative, and it loses twice: it would put a
provider-shaped model in the layer every script imports, and it would carry
down the one concession that model makes to Pulumi, the field that admits an
unresolved output, so that the cluster anchors can be declared — which is
what keeps the primary zone from being readable as data.

### 6.5 A rewrite's answer is an address

`Rewrite.answer` is a string today, and the provider recovers the address
family from it by looking for a colon. The addresses it is built from are typed
(`conventions.LAN_POOL.default_vip.v4` and `.v6`), so the type is thrown away
at the boundary and guessed back afterward. `answer` becomes
`IPv4Address | IPv6Address`, with the family a property of the row, and the
string spelling happens where the resource input is built.

### 6.6 The census has invariants, and they are a test

The roster's precedent (rfc-002 §10.2) applies unchanged: a static table's
invariants are checked once, in a test, because nothing can break them at
runtime that the test did not already catch. For routes:

*   every zone a row names is a zone `conventions.ALL_ZONES` declares;
*   no two rows publish the same host in the same zone;
*   a row's host is a DNS label, not a fully qualified name.

The first is the one that matters. A typo in a zone name today produces
rewrites for a domain nobody serves and no Cloudflare record at all, silently.

### 6.7 What `apps` inherits, and the seam between the stacks

The census being static is what lets an application's route be a *reference*
rather than a repetition, which is rfc-002 §5.3's rule for the gateway's
services applied here: a declaration holds its census entry instead of naming
it. So the app-side helper of dns.md §5 takes the row —
`public_route(conventions.routes.PHOTOS)` — rather than taking a host string
that a reader has to match against the census by eye. An application that
publishes a name `dns` never rewrites then cannot be written.

That is a contract this document fixes and the `apps` stack implements; dns.md
§5 is where it lands, and no slice here builds it.

**Both stacks write into the same zones, and the split is by resource.** Three
facts govern the seam:

1.  `dns` exports the zone identifiers and `apps` reads them across the
    StackReference — machine facts, the one crossing dns.md §1 allows. Nothing
    else crosses but the census row itself.
2.  **Cloudflare refuses a second CNAME at a name**, so a migrated name cannot
    be created by `apps` while `dns` still holds the legacy row. The order is a
    delete in `dns` and then a create in `apps`, which is the pipeline's order
    when both land in one merge. **The name is dark between the two applies** —
    immediately for a proxied name, up to the cached TTL for a name the proxy
    does not front.
    That gap is the cost of co-location; it fits inside the per-application
    cutover window the waves already have, and the alternative with no gap is
    state surgery per record per zone.
3.  **A test holds the halves apart.** For every zone, the record keys the
    legacy blocks publish there and the keys every route row renders there are
    disjoint. It compares record keys rather than hostnames because a row
    publishes more than its name (§6.4): a migration that adds the row and
    forgets to delete the legacy SRV is caught by the key comparison and would
    not have been caught by the host. A record sitting beside a component could
    not have been compared at all.

That test is what catches a migration pull request that added the row and
forgot to delete the block, before a preview shows the collision.

--------------------------------------------------------------------------------

## 7. The rewrites are a component per instance

`declare_rewrites` is the last `declare_*` function in the repository, and it
is exactly what rfc-002 §12 retires: a module function that builds resources
and returns a dictionary of them, standing where a component should be. It
becomes one component per AdGuard instance:

```python
ResolverRewrites(
    f'rewrites-{resolver.name}',      # rewrites-adguard-alice
    resolver=resolver,                # a conventions.gateway.BridgedService
    entries=rewrites(conventions.ROUTES),
)
```

**One component per instance rather than one over both**, because the
independence of the two instances is the design (dns.md §3): an instance that
is down fails its own resources and leaves the other's converged. As two
sibling components that is what the resource tree says; as one component with a
cross product inside, it is a property a reader has to derive from the resource
names.

The component takes the census entry of the resolver it writes to, not a URL —
which is what makes §8.3's derivation the only spelling of the endpoint — and
it takes the rewrites as a parameter, because deriving them from `ROUTES`
inside would be a component reaching for a census instead of receiving one.

The stack program builds one per entry of `conventions.gateway.RESOLVERS`,
unconditionally. With an empty route census the component declares nothing,
which is the same outcome as today's `if entries:` and one branch fewer in the
wiring — and, because no dynamic resource exists, the provider process never
starts, and the credential is never read (§8.3).

--------------------------------------------------------------------------------

## 8. The AdGuard rewrite provider

### 8.1 What it is today

`providers/adguard_rewrites/` is the third dynamic provider, and the one
rfc-002 §7.4 named and did not fix: *"its endpoint, username and password are
inputs on every rewrite today. Fixing it belongs to the `dns` document."* Three
consequences follow from that shape, and the second is a defect rather than an
untidiness.

1.  **The credential is on every resource.** It is declared by the stack
    program, passed through the declaration helper, and stored as an input on
    each rewrite — kept out of plain state only by an
    `additional_secret_outputs` option the resource has to remember.
2.  **A rotation empties the stored outputs.** `diff` reports a changed
    password as `changes=True` with no replacement, so the engine plans an
    update — and the provider implements none, so the base class returns
    `UpdateResult()` with no outs. The dynamic-provider host writes `{}` plus
    the provider blob in that case, so the resource's stored properties are
    replaced by nothing, and the next `delete` or `read` has no `domain` and no
    `answer` to work from. It is latent because the route census is empty:
    the stack holds zones and records in state and not one rewrite.
3.  **Every resource is named after an address.** The logical name comes from
    `instance_label(endpoint)`, so `alice-lan-photos.ucw.phd-v4` changes if the
    instance is ever addressed differently — and a changed logical name is a
    delete and a create of every rewrite there is. Nothing is at risk while the
    census is empty, which is exactly why the name is fixed before the first
    row lands.

### 8.2 The shape

The provider takes the shape slice 9 gave the device-file provider, which is
the shape framework/pulumi.md §5.2 now documents for the repository. The
measurements behind it were made once (rfc-002 §7.5, E1–E10) and are cited
here, not re-run: nothing about this provider tests a mechanism claim that one
did not already settle.

*   **The provider carries no connection state.** Attributes are unset in the
    program, `__getstate__` returns an empty bag, and what lands in state is
    the module and class name — inert, identical on every rewrite, unchanged by
    a rotation (E1, E3).
*   **The credential is read in `configure`**, from the stack configuration
    keys it already has (`adguardUsername`, `adguardPassword`), inside the
    plugin's process, where secrets arrive decrypted (E2). No caller declares
    it, no component takes it, and `additional_secret_outputs` goes away with
    the input it protected.
*   **`check` stamps two properties** no caller declares (E8): `session`, the
    endpoint with a short digest of the credential, and `provider_version`, a
    constant bumped by hand — without which an edit to `create` changes not one
    byte of state (E1). A rotation then renders as a property diff rather than
    as nothing.
*   **`diff` names its keys.** The declared inputs plus the two stamps; the two
    bags it receives are not symmetrical, and a provider comparing them
    wholesale reports a change on every run (E7). The current implementation
    already names its keys, and the set changes with the inputs.
*   **`update` is a re-stamp and nothing else.** Every change to a rewrite that
    the instance could notice is a replacement — there is no update endpoint —
    so an update can only ever mean that a stamp moved. It returns the checked
    inputs as the new output bag (E9) and makes no request. This is the
    positive form of the defect in §8.1: the method exists, and what it does is
    exactly nothing to the instance.

One correction rides along inside this module and goes no further. Its own
documentation names the accepted residual behind the credential as L11; the
audit's list ends at L10, and the finding is M6, which is how dns.md and ci.md
already spell it. Only this module's line is this document's to fix.

### 8.3 The endpoint, the identity, and the credential

**The endpoint is a declared input, not provider state.** This is where
rfc-002's own construction moved (its status header records it): a provider
imports no `conventions` and so has no way to reach a caller's decision about
where to dial. Only the credential goes into `configure`.

**The endpoint is derived from `conventions`, and the configuration key
retires.** `conventions.gateway.ADGUARD_API_PORT` already carries the note that
three declarations meet on it — the caddy site that proxies each instance, the
initial state that tells the instance where to listen, and the overlay flow
rule that admits a continuous-integration member to that port. The rewrite endpoint is the fourth, and it is stack configuration today
(`adguardEndpoints`, a key that has never been set), free to disagree with the
three. It becomes one accessor beside the port:

```python
def resolver_api_url(resolver: BridgedService) -> str:
    return f'http://{resolver.address}:{ADGUARD_API_PORT}'
```

Plain HTTP to the container VLAN address, because that is precisely what the
flow rule admits, and because the alternative — the instance's own name on the
caddy proxy — would have the runner resolve a name that only a split-horizon
rewrite answers, which is the thing being declared.

Three things follow. `adguardEndpoints` retires unset, and with it the contract
in routes.py and dns.md §3 that the first LAN-side row must ship the key.
Nothing outside the provider reads a credential, so the conditional read in the
stack program retires too. And `lib/config.strings` gains no new caller here:
the one untyped `require_object` in this stack disappears rather than being
validated.

**A rewrite is identified by its instance, not by an address.** The resource
takes the resolver's census name as an input; the identifier is
`<instance>|<domain>|<answer>` and the replacement triggers are those three.
The endpoint converges the way the device's address does: a new address is
dialed, the same row is written at the same instance, and nothing is deleted at
the old one. The logical name is built from the census name for the same reason
— `rewrites-adguard-alice-photos.ucw.phd-v4` — so an address that moves renames
nothing.

**New rule**, which the style rules imply and nowhere state:

> A resource's logical name is chosen, never derived from a value that can
> move. A name derived from an address or an endpoint makes relocating the
> target a delete and a create of everything declared against it, and a
> logical name is half of the URN that state is keyed by.

### 8.4 `diff` does not read the instance, and that is deliberate

The device-file provider opens a session in `diff`, so an edit made on the box
appears in a preview without a refresh. This provider does not, and the
asymmetry is a decision rather than an omission:

*   **A preview must not need the UDM.** Every pull request touching this stack
    runs `preview (dns)`, and a `diff` that dials fails all of it whenever the
    appliance is unreachable, in a stack where every other resource is at
    Cloudflare. That check is not a required one, so a human can still merge
    past it; what it does block is the unattended path — noop-automerge's
    zero-diff proof is a preview of its own, and a preview that cannot complete
    proves nothing. Either way it inverts ci.md §2's property, which is that an
    unreachable UDM costs these resources and nothing else.
*   **Drift is already covered where it is affordable.** The weekly drift
    matrix runs `preview --refresh`, which calls `read`, and `read` reports a
    rewrite removed in the interface as gone (ci.md §3). What `--refresh` is
    load-bearing for is exactly this class of source.
*   **The cost is per resource.** A dynamic operation sees only its own
    resource (E6), so N rewrites are N requests to the same list endpoint on
    every preview, with nothing able to share the answer.

If the drift window ever proves too wide, the change is a `diff` that dials —
recorded here so that re-opening it is a decision rather than a surprise.

### 8.5 What a live drill must show

The change is provider-facing, so the slice ships a drill transcript
(framework/testing.md §5) or an explicit unproven-live note. What the first
live run must confirm, none of it re-establishing a mechanism E1–E10 settled:

*   `configure` receives both AdGuard keys decrypted, under this project's
    namespace;
*   an unchanged run reports `unchanged`, with no request to either instance;
*   a rotation of `adguardPassword` renders as a `session` diff and touches
    neither instance;
*   a removed rewrite comes back on the next up.

--------------------------------------------------------------------------------

## 9. What the `dns` stack reads

| Key | After |
| --- | --- |
| `cloudflare:apiToken` | `kluster-py:cloudflareApiToken`, read at the provider line |
| `cloudflareAccountId` | `conventions.providers.CLOUDFLARE_ACCOUNT` |
| `adguardEndpoints` | retires, derived (§8.3) |
| `adguardUsername` | read by the rewrite provider in `configure`, by nothing else |
| `adguardPassword` | the same |

**The namespace question, settled: the token moves into this project's
namespace.** Slice 5 built the provider explicitly and left the key where it
was, in the provider's own configuration namespace, deferring the question
here. The answer is the one rfc-002 §10.3 gave for the two ambient namespaces
it retired: they *stop existing* rather than stop being read. A
`cloudflare:apiToken` entry in a stack file is indistinguishable from the
ambient configuration this repository has removed everywhere else, and it
reads as one to anybody who does not also check that default providers are
disabled for that package. Every other provider credential in the repository is
a `kluster-py:` key read at the line that builds its provider; this one joins
them, as `cloudflareApiToken`, beside the account it opens.

The move costs nothing but the edit. **The ciphertext is carried across byte
for byte**, because a passphrase-encrypted configuration value is not bound to
the key it is stored under — which is how the `oci:` and `b2:` namespaces were
retired in slice 5, in one edit each and with no value re-encrypted and no
credential handled. `pulumi:disable-default-providers` keeps listing
`cloudflare` and never becomes `*`: the dynamic rewrites depend on the
`pulumi-python` default provider (rfc-002 §8.1).

**The key's writer moves with the key.** This value is minted by
`credentials derived cloudflare-zones mint`, and the slot map names where the
mint puts it. A key renamed in the stack file alone leaves the command writing
a key the stack no longer reads, and the stack refusing by name for a value
that is present under its old name. The rename is one edit in three places —
the stack file, the slot map, and the mint — and the slice carries all three.

**The account identifier is a fact, so it is a convention.** rfc-002 §10.3
split account facts from account secrets — the OCI region and tenancy OCID and
the B2 region became `conventions.providers` while the keys stayed
configuration. The Cloudflare account identifier is on the same side of that
line, and `conventions/providers.py` already names it in passing as an
identifier a committed file may carry in the clear. It joins the module as a
`CloudflareAccount`, leaving the `dns` stack's own configuration as exactly
three secrets, two of which only the provider reads.

--------------------------------------------------------------------------------

## 10. The `dns` stack program

After §4.2, §7 and §9 the program is wiring and nothing else: it builds the
zones provider at the line that reads its token, composes the blocks, builds
one `ManagedZone` per zone from `zone_records`, builds one `ResolverRewrites`
per resolver, and exports the zone identifiers in one block.

One thing it keeps, and one it loses. The private helpers that turn the one
StackReference this stack is allowed into three addresses stay in the program:
that is a read the component cannot do for itself, and rfc-002 §12 keeps
exactly that kind of helper. The StackReference stays the recorded exception it
already is — the cluster anchors, and nothing else. What leaves is the anchor
records' *shape*: their labels, families, TTLs and comments are census data
that happened to be written in the program, and they move into `base.py` with
the rest (§4.3). What the program supplies is three addresses.

--------------------------------------------------------------------------------

## 11. Vocabulary

| Today | Becomes | Why |
| --- | --- | --- |
| `ESTATE`, "the estate records" | `BASE_RECORDS`, "the base records" | below |
| `MIRRORED_ESTATE` | *(deleted)* | there is no shared block object: a block names its own zone set (§4.2) |
| `PUBLIC_ALL`, `ZONE_MIRRORS`, `ALIAS_ZONES` | *(deleted)* | §5.2 |
| "mirror", "full mirror", "alias zone" | *(retired)* | nothing mirrors (§5.1) |
| `zones.py` | `base.py` | it holds the base records; `zone.py` holds the component |
| `model.py` | `record.py` | it holds `Record` and its constructors |
| `adguard.py` | `rewrites.py` | named for what it declares, beside `Rewrite` |
| `ZT_LABEL` | `OVERLAY_LABEL` | the value stays `zt` |
| `zt_records`, `zt_label` | `overlay_records`, `overlay_label` | one term per concept |
| `declare_rewrites`, `instance_label` | *(deleted)* | §7, §8.3 |

**"Estate" leaves the `dns` package.** rfc-002 §3.2 kept the word alive in one
meaning — the DNS records that belong to no application — and left the decision
about even that one to this document. Two rules decide it against the word.
*Descriptive over metaphorical*: what the table holds is the records a zone
carries before any application publishes into it, and "base" says that where
"estate" is a figure of speech that has already meant several different things
in these documents. *One term per concept*: the word now has exactly one
sanctioned sense in this repository, the operator's personal holdings and their
succession, which `docs/credentials.md` reasons about — and a DNS table is not
that. The deployment-wide sense the word also carried is settled separately and
becomes **installation**, which is the word this document uses for it
throughout.

**`zt` stays on the wire.** `*.zt.<zone>` is a published DNS label, and
renaming it renames live records for a code-hygiene reason. The rule rfc-002
applied to "seed" applies here: a word survives where it is the target system's
own. What changes is our own identifiers for it: the constant holding the
string, and the two functions that build the block.

--------------------------------------------------------------------------------

# Part B — the `github` stack

The half of this document that says what actually needs to move. The stack is
small, it was applied on 2026-08-25, and most of it is conformant already
(§15). Three things are not, and one thing a workflow depends on is missing
from it entirely (§14).

## 12. The forge census

The repositories and the Environments are declared twice: in `stacks/github.py`
as `OWNER`, `DEPLOYMENT_REPO`, `OPS_REPO`, `PREVIEWED_LAYERS`,
`PLAN_ENVIRONMENT` and `APPLY_ENVIRONMENT`, and in `scripts/credentials/slots.py`
as `REPOSITORY`, `OPS_REPOSITORY`, `ENVIRONMENTS` and `DRILL_ENVIRONMENT`. A
test imports the stack module inside its body to hold the two equal, and its
own comment says why it must: a script may not depend on the Pulumi provider
SDKs, and a script may not import a stack at all (AGENTS.md's import contract).

That is §3's new rule in its purest form — two programs that must agree, and
`conventions` the only home they share. The census moves to
`conventions/forge.py`, one structure rather than two sets of constants:

```python
class BranchPolicy(Enum):
    ANY_BRANCH = 'any'          # a pull request's own branch may deploy
    PROTECTED_ONLY = 'protected'

@dataclass(frozen=True)
class Environment:
    name: str
    branches: BranchPolicy
    gated: bool = False

@dataclass(frozen=True)
class Repository:
    name: str
    public: bool
    environments: tuple[Environment, ...]
    #: The labels a workflow branches on, which is why they are declared
    #: rather than made by hand (§14).
    labels: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return f'{OWNER}/{self.name}'

    @property
    def plan_offers_public_features(self) -> bool:
        # Branch protection, rulesets, environment gates and secret scanning
        # are public-repository-or-paid on this account (github.md §2).
        return self.public
```

Three properties of that shape are the point of it. The Environments keep the
order the merge chain runs them in, which is a fact `slots.py` reads. What the
account's plan offers is *derived* from visibility rather than declared beside
it, the way a site network's IPv6 prefix is derived from its IPv4 subnet
(rfc-002 §10.1): asking for secret scanning on a private repository is an API
error rather than a stricter setting, and a derived answer is one nobody can
write out of step with the visibility beside it. And the operator's
GitHub user identifier joins the module as a constant, which retires the
`get_user` invoke — a stable identity that never changes is a convention by the
same ruling that put the overlay node identifiers in the roster (rfc-002
§10.2), and an invoke is the one call that needs a parent named for it to
inherit a provider at all (rfc-002 §8.1).

`slots.py` then reads `conventions.forge`, and the equality test shrinks to the
claim that is left: every Environment a register row pushes into is one that
the census declares.

What stays in the stack program is what only it decides: the required check
names, the repository descriptions, the merge-strategy flags. Those are this
stack's own business and no second program reads them.

## 13. The provider, explicitly

The `github` stack is the last program in the repository with an ambient
provider: it builds none, so every resource takes the default one, which
configures itself from `GITHUB_TOKEN` in the environment. `Pulumi.github.yaml`
carries no `pulumi:disable-default-providers` either, so nothing would notice.

The provider is built in the stack program — both repositories' trees declare
against one account, which is rfc-002 §8.1's test for what the program owns
rather than a component — with `owner` set from the census and the token read
at that line. `Pulumi.github.yaml` gains
`pulumi:disable-default-providers: [github]`.

**New rule.** rfc-002 §8.1 says a provider credential is read at the line
that builds the provider, *out of stack configuration*. This one is not in stack
configuration and must not be: it is an account root held in the personal
estate, materialized into the environment by `mise.toml` from a workstation
slot, and its absence is what stops this stack from being applied by accident
(github.md §1). So:

> The rule is that a provider's credential is read where the provider is
> built. Which store it comes from is the credential's own design — stack
> configuration for most, the provider's own process for a dynamic one
> (rfc-002 §7.4), the environment for one that is deliberately not escrowed.

The read is explicit rather than left to the SDK, and that is not only style.
`pulumi_github` falls back to `GITHUB_TOKEN` and, failing that, **runs
anonymously**: today a missing token is not a refusal but a run that
authenticates as nobody and fails partway through on the first write. Reading
it at the provider line, refusing by name when it is unset, turns that into a
stop before anything is declared — the same conversion `disable-default-providers`
performs for a forgotten provider. The value carries no secret marking of its
own, so it is wrapped as a secret on the way in, or the token lands in state in
the clear as a provider input.

## 14. `ManagedRepository`

The stack program declares every resource itself, which style/pulumi.md
forbids in as many words: a stack program is wiring and declares no resource.
The parenting it does by hand — each Environment, the alerts resource and the
branch protection carrying `parent=repository` — is the shape of a component
drawn without one.

`components/forge/` gains one component, and the stack program builds two of
them:

```
ManagedRepository                 components/forge/__init__.py
├── github.Repository             the settings, protected against destroy
├── RepositoryVulnerabilityAlerts its own resource, not the deprecated field
├── BranchProtection              where the plan offers it, and checks are named
├── IssueLabel × n                the labels a workflow reads
└── RepositoryEnvironment × n     one per census entry, gated per the entry
```

It is the same shape as `ManagedZone` in the other half of this document — a
component owning one upstream object plus the hygiene resources that must come
with it and are invisible until they are needed — and naming it the same way is
the *same shape for same role* rule doing what it is for. The differences
between the two repositories are census fields and parameters, not branches:
visibility, the Environments, and whether required checks are named.

**A label a workflow reads is a declared resource.** `noop-automerge.yml`
branches on `expect-changes`, the escape hatch that opts a pull request out of
the zero-diff proof (ci.md §3), and that label existed in the documentation and
in nobody's repository until it was made by hand. A workflow that reads a label
nothing declares fails in the quietest possible way: the condition is simply
never true, the escape hatch is unavailable at the moment somebody needs it,
and nothing anywhere reports that the mechanism the document describes is not
present. That is precisely the class of drift this stack exists to remove, and
it is why the census carries the labels each repository must have and
`ManagedRepository` declares one `IssueLabel` per entry. `expect-changes` on
the deployment repository is the whole list today — no other workflow reads a
label, and the ops repository's automation reads none — so the value of
declaring it is not its size but that the list can no longer be shorter than
what a workflow depends on.

**Every URN this moves ships with an alias.** Introducing a parent changes the
URN of every child, and this stack is applied: without aliases the preview is
"create the parented one, delete the unparented one", and the delete of a
`protect`ed repository is refused. The slice is done when the preview shows no
replacement and no delete.

## 15. What is already conformant

Named so that a reader of this document does not go looking for work in it:

*   **The stack's posture.** Applied from the workstation, never by CI, and out
    of the drift matrix for the reason github.md §1 gives.
*   **The settings themselves.** Visibility, the rebase-only merge strategy,
    `archive_on_destroy` with `protect`, secret scanning only where the plan
    offers it, the reviewer gate with self-review permitted, the required
    checks and their exclusions. Every one of them carries its reason at the
    line, and none of them moves.
*   **The document's home.** github.md is a design document in
    `docs/framework/`, which is an exception to the layering of the docs —
    declarative/README.md §3 records it deliberately: what the forge stack
    declares is designed alongside the forge it configures. This document does
    not re-open it.
*   **The first-apply story.** github.md §3.1's import of the two repositories,
    and its explanation of why `physical-plan` is not imported, stay exactly as
    they are. §14's aliases are the same mechanism applied to a URN that moves.

--------------------------------------------------------------------------------

## 16. What does not propagate

rfc-002 is a large document, and most of it is about a machine neither of these
stacks touches. What is deliberately not applied:

*   **The template mechanism** (rfc-002 §9). Neither stack renders another
    program's configuration. The long literals in `base.py` — a DKIM public
    key, site verification tokens — are *record values*, not a configuration
    language: the rule they would fall under exists so that a rendered file is
    a file, and a TXT record's value is the record.
*   **The `versions:` namespace** (rfc-002 §11.1). Nothing in either stack is
    pinned to a build.
*   **A drift-reading `diff`** (rfc-002 §7.2). The device-file provider dials
    in `diff`; the AdGuard provider does not, and §8.4 is the argument.
*   **The connection structure** (rfc-002 §7.4's `Connection`). One URL is not
    a host, port, user and pinned key, and there is nothing to pin: the session
    is plain HTTP to a LAN address over a flow-rule-confined path. That its
    credential is a full admin login rather than a scoped token is the residual
    the audit records as M6, and not this document's to change.
*   **The vocabulary of the device** — gateway, device, UDM, overlay, site.
    Only the overlay half reaches these stacks, in §11.
*   **Aliases**, in the `dns` half — not because that stack is empty, which it
    is not, but because none of its renames moves a live URN (§1). The
    condition is checked in each slice's preview rather than assumed.
*   **A rendered copy of the census.** The gateway's rendered configuration is
    checked in and held current by a test, and the same treatment was weighed
    for the zones: one zone file per zone, regenerated and diffed. It loses
    because it is not the same case. The gateway's file is a transcript of what
    a device is given; a rendered zone file would be a second spelling of the
    declaration, checked against nothing external, and two files would change
    for every record edit — including in briefs whose owned paths are in
    `conventions` alone. §4.2 makes the declaration itself readable instead,
    which is what the render was wanted for.

## 17. What the tests look like after

*   **The census tests assert on the composition rather than rebuilding it**
    (§4.2). The expression the record tests reconstruct a zone with is the one
    the stack program uses; both become `zone_records`, and every
    subscript of the old per-zone mapping becomes the same call the program
    makes.
*   **The block invariants replace the mirror-mechanism test.** What the old
    suite asserted — that the shared block is a subset of every mirror zone —
    the shape now makes unwritable otherwise. What is worth asserting in its
    place is what the shape does not enforce: that every zone a block names is
    one `ALL_ZONES` declares, and that no block publishes an application name
    into a parked zone (§5.2).
*   **The route census gains an invariants test** (§6.6), which is what the
    roster has.
*   **The seam between the stacks gains one** (§6.7): per zone, the legacy
    blocks' record keys and the route rows' rendered record keys are disjoint.
*   **The rewrite tests re-cut per instance.** The declaration cases move from
    a helper's cross product to one component per resolver, and the endpoint
    assertions read `conventions.gateway` instead of a literal URL.
*   **The provider suite gains the stamp cases**, which
    `tests/test_device_files.py` already has in the shape to copy: a rotation
    is a change nobody declared, two credentials are two fingerprints, an
    update returns the bag state keeps, and an unchanged run asks the instance
    for nothing.
*   **The forge equality test shrinks** (§12) to the one claim that survives
    the census move, and stops importing a stack module inside a test body.
*   **The `github` stack tests keep every assertion they make.** The settings
    do not move; only where they are declared from does.

## 18. The documents this content lands in

| Document | What lands there | Slice |
| --- | --- | --- |
| declarative/dns.md | §5's zone purposes and the rule that replaces the mirror bullet; who is authoritative until cutover; §4.5's per-application cut; §4.6's apex exception; §4.4's second stage and its Transparency procedure; the base-records vocabulary; §8.3's endpoint derivation and the retired key; the rewrite component's shape; §6.7's helper taking a census row | 2, 3, 5 |
| framework/github.md | §14's declaration list as components; the provider and where its token is read; the labels a workflow reads | 7 |
| declarative/README.md | the census discriminator of §3, in both directions | 1 |
| style/pulumi.md | the census discriminator (§3); a census in this installation's terms (§4.2); a logical name never derived from an address (§8.3); where a provider's credential is read (§13) | 1, 3, 5, 7 |
| framework/pulumi.md | nothing; §5.2 is cited here, not extended | — |
| cluster/security-audit.md | nothing; M6 stands as written | — |

Per AGENTS.md, each of those edits ships inside the slice that makes it true,
not as a follow-up; the third column is where, so that no row belongs to
nobody.

## 19. How we get there

Seven slices. The two halves are independent and can run in parallel; within
each half the order keeps anything from being moved twice.

**The `dns` half:**

1.  **The route census moves to `conventions`** (§6.1, §6.2, §6.3, §6.5, §6.6):
    `Route`, `Exposure` and `ROUTES`, the primary-only default, the field for an
    application's own records, the typed answer, the invariants test, and the
    census discriminator written into declarative/README.md and
    style/pulumi.md. Nothing declares from the census yet, so the diff is
    mechanical. **Done** when the gate passes and the discriminator reads as a
    rule in the style document rather than as a citation of this one.
2.  **What each zone is for** (§5): the zone sets re-cut, the overlay block and
    the legacy VPS anchor made primary-only, the application names deleted from
    the two parked zones, and dns.md's mirror bullet replaced by the rule that
    a public record needs a listener and a certificate — plus what the parked
    zones are and when their remaining records go, and the sentence that the
    retiring DNSControl program is authoritative until cutover. It changes what
    the zones carry, so its preview shows deletes by design. **Done** when the
    preview's deletes are exactly the rows §5.3 enumerates, and it shows no
    replacement.
3.  **The record tables become blocks** (§4, §11): `Block` and `zone_records`,
    `base.py` as a table, the per-application cut of `legacy.py` — six unowned
    names deleted, two kept ones naming their owners in comment — the module
    renames, the import direction between base and legacy,
    the anchor shape moving out of the program, the overlay identifiers, the
    apex exception in dns.md, and the installation-terms rule in
    style/pulumi.md. It is the slice with the most renames in it. **Done** when
    the set of zone-and-key pairs the declaration produces is identical before
    and after — the pull request carries that comparison — and the preview adds
    no replacement and no delete beyond the six names.
4.  **The Cloudflare provider's namespace and account fact** (§9): a
    configuration move, a constant, and the two other places the key is named,
    the slot map and the mint. The encrypted value moves to its new key
    unchanged, as slice 5's did; no resource is touched. **Done** when the
    preview is empty and the mint writes the key the stack reads.
5.  **The rewrite provider and its component** (§7, §8): the stateless
    provider, `configure`, the two stamps, the re-stamping update, the instance
    identity, the endpoints from `conventions`, `ResolverRewrites`, the remnant
    of `routes.py` absorbed into `rewrites.py`, and the logical-name rule in
    style/pulumi.md. It renames the rewrite resources, which exist in no state.
    **Done** when the pull request carries the drill of §8.5 or an explicit
    unproven-live note saying what the first live run must confirm.

**The `github` half:**

6.  **The forge census** (§12): `conventions/forge.py`, both readers pointed at
    it, the invoke retired, the equality test shrunk. No resource moves and no
    input changes, so no alias is needed. **Done** when the preview shows
    nothing, which is also what proves the recorded user identifier is the one
    the invoke resolves today.
7.  **The provider and the component** (§13, §14): the explicit provider,
    `disable-default-providers`, `ManagedRepository`, the `expect-changes`
    label it adopts, the aliases that keep every existing URN, and github.md's
    half of §18. **Done** when a preview from the operator's machine shows no
    replacement and no delete.

**The first `up` is a cutover step, not a slice.** `github` is applied; `dns`
is imported and the retiring DNSControl program still owns the zones (§1), so
no slice here is judged by an apply. What keeps slices 1, 3, 4 and 5 renames
rather than migrations is that none of them moves a live URN, and each proves
it with a preview rather than asserting it. Slice 2 is the one that changes
what the zones carry, and it says which rows. Slice 7 is the one that moves
URNs, and the alias is what keeps it a rename there too. A preview any of them
is judged by will also contain the creates the first `up` will perform — the
CAA and DNSSEC settings on the pinned zones and the cluster anchors among them
— and those are not the slice's doing.

## 20. Open questions

*   **Where the monitoring dashboard's name lands** (§4.5). Whether monitoring
    is cluster infrastructure or an application decides which component
    declares `mon` after migration and therefore which wave deletes its legacy
    block. The monitoring design answers it, and the ordering is safe either
    way because monitoring is rebuilt fresh rather than migrated.
*   **Why only the primary is answered.** None of the six zones holds a CAA
    record of its own (§4.4), and the edge injects a set into that one zone's
    responses alone. What selects that zone is not known, and
    the mechanism usually offered — that the edge adds its rows only to a zone
    already carrying one of its own — cannot be it, since none of them does.
    It changes no decision: the first apply creates the same records either
    way.
*   **The LAN-only helper's default zone** (§6.2). A rewrite-only name buys one
    wildcard certificate per zone it is published in, so the default should be
    one zone; which one is the `apps` stack's design and not this document's.
*   **Whether the website co-host zone is kept at all.** It carries live mail,
    so retiring it is a mail decision rather than a DNS one. §5.1 settles only
    what it carries while it exists.
*   **Whether co-location holds.** This document keeps it: an application's
    records are declared by the application's component, from a row both stacks
    read. The alternative — every published record declared in the `dns` stack
    — would reverse dns.md §§1–2 and §6 and workloads.md §1 together, and is
    those documents' decision rather than a side effect of §6.4.
