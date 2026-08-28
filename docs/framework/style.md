# Code Style & Architecture Canon

How code in this repository is organized and written. Distilled from the
2026-08-28 physical-stack review (ops#87); the review gate in AGENTS.md
holds new code to it. Rules here are about *structure and style*;
what the system does is the declarative/ and physical/ docs' topic.

## 1. Abstraction layers

**Every reusable unit of resources is a Component, and the tree is the
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
visible outside. A provider shared across components is constructed by
the owner of the connection it represents and passed in. Child
resources inherit the provider through component `opts` — never
re-plumbed per resource. Connection state (host, credentials) lives on
the provider, not on every resource that uses it.

**Cross-component facts flow through parameters; cross-stack decisions
flow through `conventions`.** StackReference is the exception and each
use needs a recorded reason (today: the dns stack reading the cluster
anchors).

## 2. Data: conventions, configuration, censuses

**`conventions.py` holds decisions; stack configuration holds facts the
operator observed or a device minted.** A value this repository chooses
(an address plan, a role, a port) is a convention. A value read off the
world (a node id, an image digest) is configuration. A fact that the
program re-declares as static becomes a decision on the next edit and
moves to conventions.

**Related constants are one structure, not a flat namespace.** Values
that are only correct together are declared together — a `dataclass`, a
frozen mapping, an enum — so that using one without its siblings does
not type-check or does not parse. Design goal: make illegal states
unrepresentable and misuse visible at review time, not at apply time.

**A census lives where it is decided: conventions or stack
configuration.** A component receives the census it acts on; it does
not hardcode the roll inside and accept a mapping it then ignores. If a
component requires specific entries, the requirement is in its
parameter types or validated loudly at its boundary — not implied by
which keys it happens to look up.

## 3. Resources and their contents

**No inline configuration blobs.** Rendered configuration (FRR, caddy,
unit files beyond a few lines) lives in template/static files beside
the component, loaded by a shared mechanism — the analog of a
`ConfigMap` from a file. String literals in Python are for strings the code owns
(names, one-liners), not for another program's config language.

**Runtime ordering belongs to the runtime.** Dependencies and start
order between systemd units are declared in the units
(`After=`/`Requires=`/`BindsTo=`), not reconstructed in Pulumi
declaration order or boot scripts. Pulumi's dependency graph orders
*declaration*, not *boot*.

**Adopted resources graduate to declared.** `import` is step one of
adoption; the end state is an explicit declaration whose fields are
owned, with `ignore_changes` shrunk to what genuinely belongs to
another owner. A resource that stays fully ignored has no owner.

## 4. Naming

**Descriptive over metaphorical.** A name states what the thing is, not
what it is like. No estate, seed, or similar jargon when
device-services, initial-state-file say it plainly. Rename cost is paid
once; decoding cost is paid by every reader.

**One term per concept, everywhere.** When two networks exist (the
ZeroTier overlay, the LAN networks), every class, method, and variable
says which one it means; a word that could mean either is renamed until
it cannot.

**Same shape for same role.** Modules exposing the same kind of entry
point use the same form and name (`declare(...)` vs `declare_x(...)`
vs a class — pick one per role and hold it). A reviewer should be able
to guess the API of a module from its role.

## 5. Comments and docs

**Plain speech, direct.** Say the constraint, then stop. No rhetorical
detours, no jargon where a plain word exists. If a comment needs three
sentences of scene-setting, the code or the name is wrong; fix that
instead.

**Generic code never names specific use-cases.** A reusable class
documents its contract; which app or room uses it today belongs to the
caller or the design doc.

**Comments state constraints the code cannot show** — not history, not
what the next line does, not why the change was correct (that was the
PR's job). Fact-doc discipline applies to every artifact: comments,
commit messages, docs.

**Docs layer like the code.** `docs/framework/` documents mechanisms
(how this repo does Pulumi, CI, testing) and names no kluster design
decision; `docs/declarative/` and `docs/physical/` own the design.

## 6. Review gate

Every PR passes an independent review against this canon before merge —
see AGENTS.md "Review stage". The reviewer's standing questions:

- Is every new config key read at the right layer, and is every new
  constant a decision in the right home?
- Does every new resource hang off the right component, with providers
  inherited rather than re-plumbed?
- Would the diff's names survive the "no metaphor, one term per
  concept" test?
- Do the comments say anything the code already says?
