# RFC 003: The `dns` and `github` Stacks Under the Style Rules

*   **Status:** Proposed, 2026-08-29. Nothing below is built; the slices of §18
    are cut from this text once the operator approves it.
*   **Created:** 2026-08-29
*   **Authority:** the style rules (`docs/style/`) and the design documents are
    what this document obeys. Where they are silent, a rule proposed here is
    marked **new rule**.
*   **Companion:** [rfc-002](rfc-002-src-layout-and-the-gateway.md) is the
    shape, and it is also where most of what is applied here was decided. Its
    precedents are cited at each use rather than re-argued, and its
    measurements are cited rather than re-run.
*   **In scope:** the internal organization of the two stacks rfc-002 moved
    into the new tree and otherwise left alone — the `dns` record censuses, the
    route model, the AdGuard rewrite provider, the forge's declarations, both
    stack programs' configuration reading and shape, and the vocabulary of
    both.
*   **Out of scope:** which names each zone carries — no record is added,
    dropped or repointed here; the `apps` stack's route helpers (dns.md §5),
    of which this document fixes only the census they consume; the shared test
    machinery; and the `k8s-base` stack, which is unwritten.

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

**One document for two stacks**, because the two halves apply the same handful
of rules and the second half is small: the `github` stack is one program
declaring two repositories' worth of settings against one account. Splitting it
into a document of its own would mean restating §3's census rule, §12's
provider rule and §18's sequencing to say four things with them. What the two halves do not share is
sequencing, so §18 keeps them apart there.

**One difference from rfc-002 runs through everything below.** rfc-002 could
rename freely because the `physical` stack had no state. **Both stacks here
have one.** `dns` holds the zones, their DNSSEC state and the records pushed on
2026-08-26; `github` holds what was applied on 2026-08-25 (github.md §2). A
logical name or a parent that moves in either stack is a replacement of a live
resource, and the zones and both repositories carry `protect`, so such a
replacement is refused rather than performed — the same trap github.md §3.1
records for an unparented import.

That makes "does this move a live URN" a question every slice below has to
answer rather than a property either stack grants for free:

*   **The `dns` renames do not move one, by construction.** A resource's URN is
    its type, its parent and its logical name, and none of the three moves.
    `ManagedZone` stays in `zone.py` under its own name, so the component's
    type token is untouched, and the module renames of §10 — `zones.py`,
    `model.py` and `adguard.py` — carry no resource class between them. The
    zone and record logical names are built from the zone name and the record's
    state key, neither of which changes, and the published `zt` label stays on
    the wire (§10). The rewrites are the one place a logical name changes
    (§7.3), and there is no rewrite in state to move: the route census is
    empty. **Still a done-condition, not an assumption**: each `dns` slice is
    finished when its preview shows no replacement and no delete, and any
    rename found to move a live URN either ships an `aliases` option or is
    dropped.
*   **The `github` component does move URNs** (§13), because introducing a
    parent changes every child's. That slice ships aliases, and its
    done-condition is the same preview.

--------------------------------------------------------------------------------

## 2. What is inherited, and what is decided here

Everything in the left column is settled; this document only says where it
lands in these two stacks.

| Precedent | Applied here |
| --- | --- |
| rfc-002 §8.1 — every provider explicit, credential read at the line that builds it | the forge provider, which is ambient today (§12); the Cloudflare provider's namespace (§8) |
| rfc-002 §7.4 — a dynamic provider carries no connection state | the AdGuard rewrite provider (§7) |
| rfc-002 §5.3 — a census is a table; a declaration is what a component takes | the route census (§5) and the rewrites (§6) |
| rfc-002 §10.1 — related constants are one structure | the forge census (§11) |
| rfc-002 §10.3 — account facts are conventions, account secrets are configuration | the Cloudflare account (§8) |
| rfc-002 §12 — one shape per role, no `declare_*` | `declare_rewrites` (§6), the `github` program (§13) |
| rfc-002 §3 — one term per concept, descriptive over metaphorical | the vocabulary of §10 |

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
today the forge census instead has two copies held equal by a test (§11).

Applied to every table in these two stacks:

| Table | Decided by | Home |
| --- | --- | --- |
| the base records, per zone | `dns` alone | `components/dns/base.py` (§4) |
| the app records still on the VPS | `dns` alone | `components/dns/legacy.py` |
| the CAA issuer sets | `dns` alone | `components/dns/base.py` |
| the application routes | `apps` and `dns` | `conventions/routes.py` (§5) |
| the AdGuard endpoints | four declarations | `conventions/gateway.py` (§7.3) |
| repositories and Environments | the `github` stack and the credentials command | `conventions/forge.py` (§11) |

So the record tables stay where they are, and the two tables a second program
reads move. The record tables are the interesting half of that verdict, and §4
gives the argument in full rather than leaving it to the table above.

--------------------------------------------------------------------------------

# Part A — the `dns` stack

## 4. The record tables

### 4.1 They stay, and why that is not an exemption

`ESTATE` and `LEGACY` are censuses by any reading: one row per record, read top
to bottom, asserted on by tests. They stay in `components/dns/` because no
second program decides anything from them. `apps` publishes CNAMEs to an
anchor whose *name* is a convention (`conventions.ANCHOR_CLUSTER`) and never
reads a row of these tables; `physical` reads neither. What crosses is the
zone list, the zone sets and the anchor labels — and those are already in
`conventions.dns`, which is exactly the split this document ratifies.

The content is the other half of the argument. A DKIM public key, a site
verification token and a mail exchanger are not decisions this repository
makes; they are facts about services outside it, transcribed. `conventions` is
where a value *this program must agree with itself about* lives, and inflating
it with 300 lines that one program reads once would make the layer every
script imports the home of the estate's mail configuration.

**What the rule does require is already true**: `ManagedZone` receives the
records it declares and holds none of its own, and the stack program is what
hands them over.

### 4.2 The composition leaves the stack program

What a zone carries is assembled in the program today, in a list comprehension
with two conditional arms: the base records, the legacy records, the overlay
block *if the zone is a full mirror*, and the anchors *if the zone is the
primary*. Those two conditions are the mirror rule and the anchor rule — design
decisions from dns.md §2, sitting in the wiring. The tests reconstruct the same
composition to assert on it, which is the usual sign that it is stated in the
wrong place.

One function in `base.py` owns it:

```python
def zone_records(zone: str, *, anchors: Sequence[Record]) -> tuple[Record, ...]: ...
```

It returns everything the zone carries: the base records, the legacy records,
the overlay block for a full mirror, and the anchors for the primary. The
stack program builds the anchors — they are the machine facts that reach across
the StackReference, and only they — and passes them in. The mirror rule and the
anchor rule are then written once, and the census tests assert on the same
function the program calls instead of rebuilding it beside it.

`base.py` imports `legacy.py` for this, which fixes the import direction
between them as well: the legacy VPS's address and its anchor name move into
`legacy.py`, the module that empties at Wave F, and `base.py` reads them from
there. Today the direction is the other way round and the address of the thing
being retired lives in the module that outlives it.

### 4.3 The apex is the exception the anchor rule needs

dns.md §2 states that IP literals exist only under the anchor namespace, and
the census breaks it in two places. The web origin every full mirror carries is
an apex `A`, and so is jiahui.love's; **a zone apex cannot be a CNAME**, so a
name at the apex is either an address or nothing. And jiahui.id's apex and
`www` both address a site served entirely outside this estate, which publishes
no name of ours for them to alias.

Neither is an oversight and neither can be fixed, so the rule gains the two
clauses it is missing: an address may sit outside the anchor namespace where
the name cannot be an alias, and where there is no name of this estate's to
alias. That lands in dns.md §2, and `base.py` says the same beside the
literals. Nothing in the census changes.

### 4.4 The duplicate-key refusal stays

`ManagedZone` raises when two records in one zone share a state key, and a test
holds the same invariant over the declared tables. rfc-002 §10.2 retired
exactly this kind of doubling for the roster, on the ground that static code
cannot break at runtime an invariant a test already checks.

It stays here, and the difference is which side of the boundary the check is
on. The roster's validation was a function over one table this repository owns.
This is a component's refusal about the argument it was handed, and the
component is generic: it declares whatever records it is given, and it has no
way to know its caller's table is under test. The test covers the census; the
refusal covers the component. Neither makes the other redundant.

--------------------------------------------------------------------------------

## 5. The route census

### 5.1 It moves to `conventions`

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
component that declares from it (§6).

### 5.2 The exposure model is ratified

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

### 5.3 A rewrite's answer is an address

`Rewrite.answer` is a string today, and the provider recovers the address
family from it by looking for a colon. The addresses it is built from are typed
(`conventions.LAN_POOL.default_vip.v4` and `.v6`), so the type is thrown away
at the boundary and guessed back afterward. `answer` becomes
`IPv4Address | IPv6Address`, with the family a property of the row, and the
string spelling happens where the resource input is built.

### 5.4 The census has invariants, and they are a test

The roster's precedent (rfc-002 §10.2) applies unchanged: a static table's
invariants are checked once, in a test, because nothing can break them at
runtime that the test did not already catch. For routes:

*   every zone a row names is a zone `conventions.ALL_ZONES` declares;
*   no two rows publish the same host in the same zone;
*   a row's host is a DNS label, not a fully qualified name.

The first is the one that matters. A typo in a zone name today produces
rewrites for a domain nobody serves and no Cloudflare record at all, silently.

### 5.5 What `apps` inherits

The census being static is what lets an application's route be a *reference*
rather than a repetition, which is rfc-002 §5.3's rule for the gateway's
services applied here: a declaration holds its census entry instead of naming
it. So the app-side helper of dns.md §5 takes the row —
`public_route(conventions.routes.PHOTOS)` — rather than taking a host string
that a reader has to match against the census by eye. An application that
publishes a name `dns` never rewrites then cannot be written.

That is a contract this document fixes and the `apps` stack implements; dns.md
§5 is where it lands, and no slice here builds it.

--------------------------------------------------------------------------------

## 6. The rewrites are a component per instance

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
which is what makes §7.3's derivation the only spelling of the endpoint — and
it takes the rewrites as a parameter, because deriving them from `ROUTES`
inside would be a component reaching for a census instead of receiving one.

The stack program builds one per entry of `conventions.gateway.RESOLVERS`,
unconditionally. With an empty route census the component declares nothing,
which is the same outcome as today's `if entries:` and one branch fewer in the
wiring — and, because no dynamic resource exists, the provider process never
starts, and the credential is never read (§7.3).

--------------------------------------------------------------------------------

## 7. The AdGuard rewrite provider

### 7.1 What it is today

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

### 7.2 The shape

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
    positive form of the defect in §7.1: the method exists, and what it does is
    exactly nothing to the instance.

One correction rides along inside this module and goes no further. Its own
documentation names the accepted residual behind the credential as L11; the
audit's list ends at L10, and the finding is M6, which is how dns.md and ci.md
already spell it. Only this module's line is this document's to fix.

### 7.3 The endpoint, the identity, and the credential

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

### 7.4 `diff` does not read the instance, and that is deliberate

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

### 7.5 What a live drill must show

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

## 8. What the `dns` stack reads

| Key | After |
| --- | --- |
| `cloudflare:apiToken` | `kluster-py:cloudflareApiToken`, read at the provider line |
| `cloudflareAccountId` | `conventions.providers.CLOUDFLARE_ACCOUNT` |
| `adguardEndpoints` | retires, derived (§7.3) |
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

**The account identifier is a fact, so it is a convention.** rfc-002 §10.3
split account facts from account secrets — the OCI region and tenancy OCID and
the B2 region became `conventions.providers` while the keys stayed
configuration. The Cloudflare account identifier is on the same side of that
line, and `conventions/providers.py` already names it in passing as an
identifier a committed file may carry in the clear. It joins the module as a
`CloudflareAccount`, leaving the `dns` stack's own configuration as exactly
three secrets, two of which only the provider reads.

--------------------------------------------------------------------------------

## 9. The `dns` stack program

After §4.2, §6 and §8 the program is wiring and nothing else: it builds the
zones provider at the line that reads its token, builds one `ManagedZone` per
zone from `zone_records`, builds one `ResolverRewrites` per resolver, and
exports the zone identifiers in one block.

Two things it keeps. `_anchors` and `_address` stay private helpers of the
program: they turn the one StackReference this stack is allowed into records,
which is a read the component cannot do for itself, and rfc-002 §12 keeps
exactly that kind of helper. And the StackReference stays the recorded
exception it already is — the cluster anchors, and nothing else.

--------------------------------------------------------------------------------

## 10. Vocabulary

| Today | Becomes | Why |
| --- | --- | --- |
| `ESTATE`, "the estate records" | `BASE_RECORDS`, "the base records" | below |
| `MIRRORED_ESTATE` | `MIRRORED_BASE` | the same word, the same reason |
| `zones.py` | `base.py` | it holds the base records; `zone.py` holds the component |
| `model.py` | `record.py` | it holds `Record` and its constructors |
| `adguard.py` | `rewrites.py` | named for what it declares, beside `Rewrite` |
| `ZT_LABEL` | `OVERLAY_LABEL` | the value stays `zt` |
| `zt_records`, `zt_label` | `overlay_records`, `overlay_label` | one term per concept |
| `declare_rewrites`, `instance_label` | *(deleted)* | §6, §7.3 |

**"Estate" leaves the `dns` package.** rfc-002 §3.2 kept the word alive in one
meaning — the DNS records that belong to no application — and left the decision
about even that one to this document. Two rules decide it against the word.
*Descriptive over metaphorical*: what the table holds is the records a zone
carries before any application publishes into it, and "base" says that where
"estate" is a figure of speech that has already meant four different things in
these documents. *One term per concept*: the whole-deployment sense of the word
is still in use across `docs/credentials.md`, the README, the import contract
and several components' own documentation, and that sense is the one with no
plain replacement.

**Sequenced with the pending ruling on that other sense.** What becomes of the
deployment-wide meaning is an operator decision still open, and the rename here
holds either way: if "estate" is ratified as the deployment-wide word, the DNS
sense must move regardless; if it is retired there too, this is one sweep
instead of two. So the slice waits for that ruling rather than depending on its
outcome.

**`zt` stays on the wire.** `*.zt.<zone>` is a published DNS label, and
renaming it renames live records for a code-hygiene reason. The rule rfc-002
applied to "seed" applies here: a word survives where it is the target system's
own. What changes is our own identifiers for it: the constant holding the
string, and the two functions that build the block.

--------------------------------------------------------------------------------

# Part B — the `github` stack

The half of this document that says what actually needs to move. The stack is
small, it was applied on 2026-08-25, and most of it is conformant already
(§14). Three things are not, and one thing a workflow depends on is missing
from it entirely (§13).

## 11. The forge census

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
    #: rather than made by hand (§13).
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

## 12. The provider, explicitly

The `github` stack is the last program in the repository with an ambient
provider: it builds none, so every resource takes the default one, which
configures itself from `GITHUB_TOKEN` in the environment. `Pulumi.github.yaml`
carries no `pulumi:disable-default-providers` either, so nothing would notice.

The provider is built in the stack program — both repositories' trees declare
against one account, which is rfc-002 §8.1's test for what the program owns
rather than a component — with `owner` set from the census and the token read
at that line. `Pulumi.github.yaml` gains
`pulumi:disable-default-providers: [github]`.

**New rule.** §8.1 says a provider credential is read at the line that builds
the provider, *out of stack configuration*. This one is not in stack
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

## 13. `ManagedRepository`

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

## 14. What is already conformant

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
    they are. §13's aliases are the same mechanism applied to a URN that moves.

--------------------------------------------------------------------------------

## 15. What does not propagate

rfc-002 is a large document, and most of it is about a machine neither of these
stacks touches. What is deliberately not applied:

*   **The template mechanism** (§9). Neither stack renders another program's
    configuration. The long literals in `base.py` — a DKIM public key, site
    verification tokens — are *record values*, not a configuration language:
    the rule they would fall under exists so that a rendered file is a file,
    and a TXT record's value is the record.
*   **The `versions:` namespace** (§11.1). Nothing in either stack is pinned to
    a build.
*   **A drift-reading `diff`** (§7.2). The device-file provider dials in
    `diff`; the AdGuard provider does not, and §7.4 is the argument.
*   **The connection structure** (rfc-002 §7.4's `Connection`). One URL is not
    a host, port, user and pinned key, and there is nothing to pin: the session
    is plain HTTP to a LAN address over a flow-rule-confined path. That its
    credential is a full admin login rather than a scoped token is the residual
    the audit records as M6, and not this document's to change.
*   **The vocabulary of the device** — gateway, device, UDM, overlay, site.
    Only the overlay half reaches these stacks, in §10.
*   **Aliases**, in the `dns` half — not because that stack is empty, which it
    is not, but because none of its renames moves a live URN (§1). The
    condition is checked in each slice's preview rather than assumed.

## 16. What the tests look like after

*   **The census tests assert on the composition rather than rebuilding it**
    (§4.2). `_records()` in the record tests is the same expression the stack
    program uses; both become `zone_records`.
*   **The route census gains an invariants test** (§5.4), which is what the
    roster has.
*   **The rewrite tests re-cut per instance.** The declaration cases move from
    a helper's cross product to one component per resolver, and the endpoint
    assertions read `conventions.gateway` instead of a literal URL.
*   **The provider suite gains the stamp cases**, which
    `tests/test_device_files.py` already has in the shape to copy: a rotation
    is a change nobody declared, two credentials are two fingerprints, an
    update returns the bag state keeps, and an unchanged run asks the instance
    for nothing.
*   **The forge equality test shrinks** (§11) to the one claim that survives
    the census move, and stops importing a stack module inside a test body.
*   **The `github` stack tests keep every assertion they make.** The settings
    do not move; only where they are declared from does.

## 17. The documents this content lands in

| Document | What lands there |
| --- | --- |
| declarative/dns.md | §2's apex exception; §3's endpoint derivation and the retired key; the base-records vocabulary; the rewrite component's shape; §5's helper taking a census row; the status header, which still calls the stack unapplied |
| framework/github.md | §3's declaration list as components; the provider and where its token is read; the labels a workflow reads |
| declarative/README.md | the census discriminator of §3, in both directions |
| style/pulumi.md | the three new rules: the census discriminator (§3), the credential store (§12), and that a logical name is never derived from an address (§7.3) |
| framework/pulumi.md | §5.2 gains nothing new; it is cited here, not extended |
| cluster/security-audit.md | nothing; M6 stands as written |

Per AGENTS.md, each of those edits ships inside the slice that makes it true,
not as a follow-up.

## 18. How we get there

Six slices. The two halves are independent and can run in parallel; within each
half the order keeps anything from being moved twice.

**The `dns` half:**

1.  **The route census moves to `conventions`** (§5.1–§5.4): `Route`,
    `Exposure` and `ROUTES`, the typed answer, and the invariants test. Nothing
    declares from it yet, so the diff is mechanical. One line rides along:
    dns.md's status header still says the stack is not yet applied, and it is —
    the zones and their records went in on 2026-08-26.
2.  **The record tables** (§4, §10): the module renames, `zone_records`, the
    import direction between base and legacy, the overlay identifiers, and the
    apex exception in dns.md. **Sequenced after the pending ruling on
    "estate"**, so that one sweep settles the word. It is the slice with the
    most renames in it and the stack is applied, so its done-condition is a
    preview with no replacement and no delete (§1).
3.  **The Cloudflare provider's namespace and account fact** (§8): a
    configuration move and a constant. The encrypted value moves to its new key
    unchanged, as slice 5's did; no resource is touched, so the preview is
    empty.
4.  **The rewrite provider and its component** (§6, §7): the stateless
    provider, `configure`, the two stamps, the re-stamping update, the instance
    identity, the endpoints from `conventions`, and `ResolverRewrites`. This is
    the one slice with a live drill (§7.5). It renames the rewrite resources,
    which exist in no state today; the same preview condition applies, and if
    the census is no longer empty when it lands, the renames ship aliases.

**The `github` half:**

5.  **The forge census** (§11): `conventions/forge.py`, both readers pointed at
    it, the invoke retired, the equality test shrunk. No resource moves and no
    input changes, so no alias is needed — the recorded user identifier is the
    one the invoke resolves today, and a preview proves it by showing nothing.
6.  **The provider and the component** (§12, §13): the explicit provider,
    `disable-default-providers`, `ManagedRepository`, the `expect-changes`
    label it adopts, and the aliases that keep every existing URN. Verified by a preview from the operator's machine
    showing no replacement and no delete.

Both stacks are applied, so no slice here is free by default. What makes the
`dns` half a set of renames rather than migrations is that none of them moves a
live URN, and every one of those slices proves it with a preview rather than
asserting it. Slice 6 is the one that does move URNs, and the alias is what
keeps it a rename there too.
