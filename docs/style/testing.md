# Testing Style

What a test may assert, and what it may write down in order to assert
it. [framework/testing.md](../framework/testing.md) answers the other
question — how a test is *run*: the tiers and what each can know, the
doubles and fakes, and the recipe for proving a case fails without the
change it ships with. A rule about the machinery belongs there; a rule
about what a case is entitled to claim belongs here.

## A test holds a value still only against a second source

A test writes a value as a literal only where the assertion has a side
the edit under review cannot move. Three sides qualify:

-   **The production value is computed** — derived, spliced, defaulted,
    or minted by a framework — so it can change with the new value
    appearing in no diff. The site's /64s are the case: they are
    numbered off their IPv4 subnets, and the rule that numbers them can
    be rewritten without a single prefix being typed anywhere. What
    earns the literal is the *rule* being able to move on its own, not
    the fact that a computation happened: a value spliced out of
    constants that are themselves typed moves only when one of them is
    edited, so a copy of the result mirrors both.
-   **The other side is an artifact of independent origin the test
    reads** — a transcript of what a device serves today, a workflow
    file no import reaches, another program's own spelling of the same
    decision. Those can drift from the declaration, which is exactly
    what lets the comparison fail for someone other than the editor.
-   **The literal transcribes a contract a third party holds** — a wire
    format, an upstream parser's parameter names, a resource type token
    the URNs in state are keyed by.

A literal copied from a constant typed at its own declaration is a
**mirror**. Every event that can redden it is a deliberate edit already
visible in the constant's own diff; the same hand updates both copies
in the same change; a right new value and a wrong one pass equally. It
detects edits rather than defects, and charges a fix cycle for every
legitimate change. The mechanical test is **whether the check can fail
for anybody other than the person editing the value**. A class rename
moves a type token its author never types — a literal catches that. A
table row's field is edited at the field, under that field's own
documentation — a literal beside it only says the same thing twice.

**"The value has a second source out in the world" is necessary and not
sufficient.** In an infrastructure repository every constant has one
eventually: a dataset already at the path, a bookmark already on the
name, leases already pointing at the address. What decides is whether
the test can *reach* that party. Where it cannot, the coupling is a
constraint on the constant's own declaration, where whoever edits it is
guaranteed to be looking, and agreement with the outside party is
proven at the tier that can consult it.

So a mirror is deleted rather than weakened, and what it was guarding
moves to where it works: a relation is asserted symbolically — "the
default is the primary zone alone", not the domain the primary zone
happens to be — and a loop left with nothing to visit has its vacuity
guarded rather than its membership restated.
