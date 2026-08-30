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

**A census lives where it is decided: conventions or stack
configuration.** A component receives the census it acts on; it does
not hardcode the roll inside and accept a mapping it then ignores. If a
component requires specific entries, the requirement is in its
parameter types or validated loudly at its boundary — not implied by
which keys it happens to look up.

## Resources and their contents

**Runtime behavior belongs to the runtime.** Pulumi declares the
desired state; what happens after the apply is the target system's job,
expressed in that system's own mechanism — not reconstructed in Pulumi
declaration order, resource `depends_on`, or glue scripts. Pulumi's
dependency graph orders *declaration*, nothing else. The canonical
case: dependencies and start order between systemd units are declared
in the units (`After=`/`Requires=`/`BindsTo=`), never in the order
resources happen to be created or in boot scripts.

**Adopted resources graduate to declared.** `import` is step one of
adoption; the end state is an explicit declaration whose fields are
owned, with `ignore_changes` shrunk to what genuinely belongs to
another owner. A resource that stays fully ignored has no owner.

**Rendered configuration comes from files** — see
[python.md](python.md)'s long-literals rule; the loading mechanism is
shared, one per repository, not re-invented per component.

## Review questions

The architecture reviewer's standing questions, for the review stage
(AGENTS.md):

-   Is every new config key read at the right layer, and is every new
    constant a decision in the right home?
-   Does every new resource hang off the right component, with
    providers inherited rather than re-plumbed?
-   Would the diff's names survive the "no metaphor, one term per
    concept" test?
-   Do the comments say anything the code already says?
