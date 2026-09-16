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
program reads the keys that parameterize the stack and pushes values
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

**Stack configuration is where a provider credential lives**, and it is
the store the paragraph above licenses a component to read for itself.
Every one of them is there, however the value was obtained: minted from
a seed, or made by hand in a console because the platform publishes no
API that makes one — the `github` stack's admin token
([framework/github.md](../framework/github.md) §1) is that second case,
and it is a config secret like the rest. How a credential is *obtained*
is the credential's own design and belongs in
[credentials.md](../credentials.md) §3; where it is *read* is the rule
above, and neither moves with the other.

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

**A census is the source of truth, and no test restates it.** A check
that holds one reader of a census against another agrees with whatever
the census says; a check that holds the census against a copy typed in a
test can fail only for whoever edits the census, and goes green again as
soon as the copy is moved to match ([testing.md](testing.md)). What a
census's tests hold are its **invariants** — relations between entries
that a row's type cannot carry — and its **derivations**, whose expected
outputs are literals precisely because a derivation can move with no new
value typed anywhere. A row's coupling to the world — the dataset
already at its mount, the leases already pointing at its address, the
port the appliance already binds — is stated on the row itself, and
proven at the tier that can consult that world.

**A seam test names the side it holds still.** The seam is wherever the
census is not the other side's source: a file no import reaches (a
workflow, a rendered configuration, the transcript of what a device
serves), or another program's own spelling of the same decision. Those
can disagree with the census, which is what makes them able to catch it,
and which rows they catch is a property of the rows — the map the
`credentials` command pushes from spells `ZEROTIER_PHYSICAL` and
`ZEROTIER_DNS` out, so renaming the `dns` Environment reddens its check
and renaming `apps` does not. An assertion whose other side turns out to
be the census itself is deleted rather than left reading as a guard.

**A census's invariants and seam tests live in
`tests/test_conventions.py`** — the suite that mirrors the package the
censuses are declared in — whatever program reads the census, and never
in a suite named for one of those programs. A reader looking for what
holds a census still then does not have to know which program reads it,
and the check does not sit behind that program's fixtures: a
module-scoped `autouse` fixture errors every case in its file when the
program fails to run, so a check kept there is out of reach at exactly
the moment it is wanted, which is when that program's own cases are
failing. The invariants sit beside the seam tests because they answer
the same question about the same table: is it still what it says it
is.

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

**A child's logical name carries its component's `name`.** A URN
qualifies a logical name by the chain of parent *types*, never by a
parent's name, so two components of one type that each declare a child
of one type under one name — two `ManagedRepository`s each holding a
`BranchProtection('main')`, two repositories each with an Environment
of one name — register one URN twice. The engine refuses the second,
and the repair is a rename, which is a delete and a create: for a
`RepositoryEnvironment` one that discards its secrets and forces a
`credentials derived sync`, for a `BranchProtection` an unprotected
window. So a child's logical name carries the `name` of the component
the URN places it under — the nearest component above it on the parent
chain, whatever code declared it: `name` alone for the resource the
component *is*, `f'{name}-…'` for the rest. That value is chosen, so
the rule above is satisfied, and applied at every level it makes every
name unique across instances of the whole type chain. A name is not
what keeps two resources off one place on a target: the URN qualifies a
name by parent types, so two components of different types can each
declare one device path, endpoint or record and register cleanly —
one-place-one-resource is an invariant over the declared inputs,
asserted on the program. It is about *logical* names only: a singleton
whose physical name comes from `conventions` with autonaming disabled
keeps that physical name. Landing the rule on a child that state
already holds under the old name is `aliases=[pulumi.Alias(name=<old>)]`
on that child, dropped in a later change once state carries the new
URN.

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
-   Does every case that holds a census against something have a side
    the census is not the source of, and does no test restate a
    census's typed content ([testing.md](testing.md))?
-   Does every new resource hang off the right component, with
    providers inherited rather than re-plumbed?
-   Would the diff's names survive the "no metaphor, one term per
    concept" test, and is every logical name built only from values
    that cannot move?
-   Does every child's logical name carry the `name` of the component
    the URN places it under?
-   Do the comments say anything the code already says?
