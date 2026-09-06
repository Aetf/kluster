# Pulumi Architecture & Style

How the Pulumi programs are organized: layering, components,
providers, and where data lives. The mechanics of the framework itself
(async inputs, `Component`, testing tiers) are `docs/framework/`'s
topic; this page is about using them well.

## Layering

**Every reusable unit of resources is a component, and the tree is the
architecture.** A stack program (`stacks/*.py`) is wiring: it reads
stack configuration, builds the top-level components, exports outputs —
and declares no resource of its own. Components compose child
components down to leaf resources. If a set of resources has a name in
the design (the gateway, a container, an app), it is a component, not a
function that scatters resources into someone else's.

**Configuration is read at the layer that owns the concept.** A stack
program reads the keys that parameterize its domain and pushes values
down as constructor parameters. A component never reads stack
configuration for a concept that belongs to its parent, and a parent
never reaches into a child's implementation detail to configure it. The
test: if two parents could plausibly pass different values, it is a
parameter; if no parent has an opinion, it is the component's own
business and no key should exist.

**Providers follow the same ownership rule.** A provider that is an
implementation detail of one component is constructed inside it and not
visible outside. A provider several components share is constructed by
the stack program and set on each of them: built inside any one of them
it would be reached into by the rest. Child resources inherit the
provider through component `opts` — never re-plumbed per resource; an
invoke inherits only through a parent, so it names one. Connection state
(host, credentials) lives on the provider, not on every resource that
uses it. Custom providers are code of their own kind and live in their
own subpackage, apart from the declaration logic that uses them.

**Every provider is explicit, and its credential is read at the line
that builds it** — wherever that line is, and by nothing else. A
provider and the secret that opens it are one thing, and separating them
means a reader has to hold two files in their head to answer "what does
this authenticate as". This is the one category of value a component may
read out of stack configuration for itself: a credential that configures
a provider and is read by nothing else. Everything else still arrives as
a parameter, and no such credential reaches any component's signature.
A program that follows this rule disables default providers for the
packages it builds providers for, which turns a forgotten one into an
error rather than a silent fallback. See
[rfc-002](../rfc/rfc-002-src-layout-and-the-gateway.md) §8.

**Which store that credential comes from is the credential's own
design.** Stack configuration is the usual answer, and it is the store
the paragraph above licenses a component to read for itself. A
credential that is deliberately not escrowed — escrow is
[credentials.md](../credentials.md) §2.2, and only durable roots are
escrowed — comes from the environment instead, so that a machine which
does not already hold it cannot apply that stack at all: today the
`github` stack's account-root token
([framework/github.md](../framework/github.md) §1). The store is a
property of the credential; the read site is the rule above, and does
not move with the credential.

**Cross-component facts flow through parameters; cross-stack decisions
flow through `conventions`.** StackReference is the exception and each
use needs a recorded reason (today: the dns stack reading the cluster
anchors).

## Data: conventions, configuration, censuses

**`conventions` holds decisions and identities; stack configuration
holds operator-supplied values that can change between applies.** A
value this repository chooses (an address plan, a role, a port) is a
convention. A stable identity of something the design names — a
device's node id, a compartment OCID — is also a convention: an entry
that cannot be matched to the one thing it names is not a census entry.
Configuration is for the values an operator supplies or rotates:
credentials, knobs, measurements of the moment.

**Related constants are one structure, not a flat namespace** — the
[illegal-states rule](README.md) applied to data: group them so that
using one without its siblings does not type-check or does not parse.

**A census lives with the programs that read it.** Count them — a
stack program or a script alike, and regardless of whether each
declares a resource from the table. One: the table is data in that
program's own area, beside the component that receives it and never
inside it. More than one: it is a convention, because `conventions` is
the only package a stack program and a script can both import. Which
program turns the table into resources does not enter into it. A roll
an operator supplies or rotates is stack configuration, not a census.

**A component receives the census it acts on**; it does not hardcode
the roll inside and accept a mapping it then ignores. If a component
requires specific entries, the requirement is in its parameter types or
validated loudly at its boundary — not implied by which keys it happens
to look up.

**A census parameter has no default.** Making the roll the default value
of the parameter that receives it satisfies the rule above to the letter
and defeats it: the signature reads as though the caller decides, while
a caller that passes nothing gets the table the component chose, and the
review question "is this table beside the component that receives it" is
answered yes by a component that behaves no. So the parameter is
required. A component with nothing to declare is handed an empty roll
explicitly; one whose roll no caller would ever vary keeps neither the
parameter nor the mechanism behind it, because an unused mechanism
driven by a table nobody can change is dead code rather than an
extension point.

**A census is declared in the terms of this installation, not of the
provider it is pushed to.** Where a table's natural statement is "these
things, in these places", the unit is that statement — the group and the
named set it applies to — and the per-member form the provider takes is
derived by one function in front of the component. Writing the table the
provider's way instead splices shared groups into every member, hides
members inside loops that fill them in, and leaves the decisions about
which member gets what sitting in the wiring. The test: can a reader
name the set a row belongs to by reading the row? The DNS record blocks
are one instance, the overlay roster (one entry per member, not one per
network object) another, and `Exposure` a third — it says what an
application's reachability *is* rather than which two resources it
produces.

**A census both sides read is pinned by content.** A check that holds
one reader of a census against another agrees with whatever the census
says: a renamed row moves both sides in the one edit, and the check
passes by construction. It does not go red — it stops being *able* to go
red, while the ledger still shows a guard, which is worse than having
none. So a census carries a case that writes its content out — the
names, and the fields decisions are derived from — as literals typed at
the test and read from nowhere. That literal is the second source the
comparison needs to have something to be wrong about, and editing the
census then costs a second line: a reader deciding, deliberately, that
the pin moves with it.

**A seam test keeps only what still crosses a seam.** The seam is
wherever the census is not the other side's source: a file no import
reaches (a workflow, a rendered configuration, the live one on a
device), or a value some reader spells as a literal of its own. Those
keep biting, and which rows they bite on is a property of the rows
rather than of the test — the map the `credentials` command pushes from
spells `ZEROTIER_PHYSICAL` and `ZEROTIER_DNS` out, so renaming the `dns`
Environment reddens its check and renaming `apps` does not. So a seam
test names the side it is holding still, and an assertion whose other
side turned out to be the census is moved to the pin rather than left
where it reads as a second guard.

**A pin is at the census, not wherever the value happens to be
covered.** A census value is often reachable from a golden file or a
rendered artifact a few suites away, and that guard is real — but it
belongs to its own subject, it moves when that subject moves, and it
names no census. So a case kept because the ground is covered elsewhere
says where, and a case that can say nowhere is either given literals or
deleted with its reason recorded where the next reader meets it:
otherwise "deleted because it could not fail" and "deleted by mistake"
leave the same diff behind.

**A seam that reaches every row stands in for the content pin — for the
fields it carries, and no others.** It has to be content itself: the
other side written out by hand rather than derived from the census, as
the gateway's services are, held name by name and address by address
against the configuration the device serves. Where one does stand in,
the census's entry in `tests/test_conventions.py` says which suite
carries it and which fields, so that finding a census's pin stays one
lookup and so a golden file — which goes when its own subject goes —
takes a written claim with it instead of orphaning one silently. A field
no substitute carries is written out here whatever else covers the row:
a table with one field pinned and the field beside it bare reads as
covered and is not.

**A census's pins and invariants live in `tests/test_conventions.py`** —
the suite that mirrors the package the censuses are declared in —
whatever program reads the census, and never in a suite named for one of
those programs. A reader looking for what pins a census then does not
have to know which program reads it, and the pin does not sit behind
that program's fixtures: a module-scoped `autouse` fixture errors every
case in its file when the program fails to run, so pins kept there are
out of reach at exactly the moment they are wanted, which is when that
program's own cases are failing. The invariants go with the pin because
they answer the same question about the same table: is it still what it
says it is.

## Resources and their contents

**Runtime behavior belongs to the runtime.** Pulumi declares the
desired state; what happens after the apply is the target system's job,
expressed in that system's own mechanism — not reconstructed in Pulumi
declaration order, resource `depends_on`, or glue scripts. Pulumi's
dependency graph orders *declaration*, nothing else. The canonical
case: dependencies and start order between systemd units are declared
in the units (`After=`/`Requires=`/`BindsTo=`), never in the order
resources happen to be created or in boot scripts.

**A logical name is chosen, never derived from a value that can
move.** A resource's logical name is half of the URN its state is keyed
by, so renaming one is a delete and a create — not a rename. A name
built out of an address, an endpoint, a hostname or any other value the
target can be given a new one of therefore turns relocating that target
into a delete and a create of everything declared against it, all at
once, for a change the target itself never noticed. Name a resource
after the thing it is declared against as the census identifies that
thing, and let the movable value be an ordinary input: the instance,
not the address it currently answers on. The same holds for anything
else a name is spliced from — a value that is configuration is a value
someone may edit.

**Adopted resources graduate to declared.** `import` is step one of
adoption; the end state is an explicit declaration whose fields are
owned, with `ignore_changes` shrunk to what genuinely belongs to
another owner. A resource that stays fully ignored has no owner.

**Rendered configuration comes from files** — see
[python.md](python.md)'s long-literals rule; the loading mechanism is
shared, one per repository, not re-invented per component.

## Review questions

The architecture reviewer's standing questions, for the review stage
([framework/dispatch.md](../framework/dispatch.md) §3):

-   Is every new config key read at the right layer, and is every new
    constant a decision in the right home?
-   Is every new table written in this installation's terms, with the
    provider's per-member form derived rather than written out, and does
    every census parameter arrive without a default?
-   Is every new census pinned by literals at
    `tests/test_conventions.py`, and does every case that holds a census
    against something still have a side the census is not the source
    of?
-   Does every new resource hang off the right component, with
    providers inherited rather than re-plumbed?
-   Would the diff's names survive the "no metaphor, one term per
    concept" test, and is every logical name built only from values
    that cannot move?
-   Do the comments say anything the code already says?
