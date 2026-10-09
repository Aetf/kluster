# Testing Style

What a test may assert, and what it may write down in order to assert
it. [framework/testing.md](../framework/testing.md) answers the other
question — how a test is *run*: the tiers and what each can know, the
doubles and fakes, and the recipe for proving a case fails without the
change it ships with. A rule about the machinery belongs there; a rule
about what a case is entitled to claim belongs here.

## Tests verify behavior

A test exists to fail when the program's behavior breaks. **A
change-detector assertion is not written**: an assertion whose only way
to fail is someone changing the value it restates, when that change
could well be correct. It catches no regression, since the event that
reddens it is an edit already visible in the diff under review, which
the same hand updates on both sides in the same change. And it breaks
on every legitimate rename and rewording, charging a fix cycle for each.
The name and the argument against it are from
[*Change-Detector Tests Considered Harmful*](https://testing.googleblog.com/2015/01/testing-on-toilet-change-detector-tests.html)
(Google Testing Blog, 2015). The shapes it takes here:

-   a constant pinned to a literal;
-   a table copied from a document or from the implementation;
-   an exact message or wording;
-   a call shape — which function was called, with which arguments;
-   a wiring assertion symbolic on both sides.

**What decides is what can make the assertion fail.** An assertion
earns its place when it fails because behavior broke: an output wrong
for its input, two parts of the code that stopped agreeing, an external
contract no longer met, or a disagreement with a source independent of
the code, such as a vendor's documentation or a recorded response. An
assertion only an edit to the value it restates can redden is a change
detector, however much it reads like a guard.

**Protecting something sensitive from accidental change belongs in the
program's design**, which makes the change conspicuous in review, and
not in a test that pins its value. Pulumi's `protect` is the model: a
resource holding data is protected by the attribute on the resource,
not by a test that it still exists. In the same way, a value whose
change matters outside the program says so at its declaration,
where whoever edits it is looking, and a posture that must not loosen is
written restrictive by default, each exception carrying its reason, so
that a loosening is a line in the diff.

### Worked examples

Each shape is answered the same way: the pin is deleted, and what it
seemed to guard moves into a test of behavior or into the design.

-   **Which fields are secret, listed in a test.** The list mirrors the
    types it names and misses the next secret field until someone
    remembers to add it. The fact belongs to the field, as a marker in
    its production type (`Annotated[str, Secret]` or similar). A case
    then holds every marked field to `repr=False, compare=False` with no
    list, and the checks that a secret does not leak take their fields
    from the marker.
-   **A name already in state or in the world, pinned to its literal.**
    The pin fails only for whoever renames the constant, and a rename
    that brings its migration is correct. The cases test behavior with
    the constant or a fake name: that minting and retirement address the
    same name, say. That the name is remembered outside the program is
    stated at its declaration ("renaming needs a migration"), or the
    code handles the migration.
-   **A requirement written in a document, copied as a table.** The copy
    agrees with the document until one of them is edited, and then
    fails for the editor. A security posture that must not regress is
    protected by the program's structure instead: restrictive by
    default, with each exception carrying its reason.
-   **A fragment of a message, telling two branches apart.** A match on
    the wording fails on a rewording, and passes when the wrong branch
    raises a similar sentence. Branches are told apart by exception type
    or by a structured field; where two branches share a type, the code
    gives each its own.
-   **A wiring assertion symbolic on both sides**, such as an accessor's
    result held to its own body written again. It is deleted. Crossed
    wiring, an IPv4 value where an IPv6 one belongs, is prevented by the
    types or caught by a test of the wired behavior.
-   **Prose pinned verbatim**, such as a help text or a rendered
    paragraph. It is deleted, and what stays are relations: every
    register row appears in the rendered help.

## A test holds a value still only against a second source

This is the rule above applied to a literal on an assertion's expected
side. A literal is a change detector unless something other than the
edit under review can move the side it is compared with: the sides
below are the ones that can, and the **mirror** this section names is
the change detector in its literal form. The mechanical test it gives,
whether the check can fail for anybody other than the person editing
the value, is the definition above asked of one literal.

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
for anybody other than the person editing the value**. No edit to the
program moves the type a stack's state holds a component under, so a
literal of that type catches a token changed without its alias. A
table row's field is edited at the field, under that field's own
documentation — a literal beside it only says the same thing twice.
A census row's **state** is a value like any other: recorded or not
yet, present or absent. A test that inherits the live state as its
premise goes red on the day a site fact is recorded, which is an edit
and not a defect, so a test that needs a particular state sets it up.
So does a test whose assertion can fail in only one of the states: the
live state can weaken it without ever turning it red.

**That test asks for a second source, not a second site.** Read alone it
licenses restating a function: an assertion holding an accessor's result
to a rewrite of its body fails for whoever edits the accessor, which is
a different site from the constant the accessor reads, so it seems to
pass. It is the same mirror with the hand moved from the declaration to
the definition — both sides still move in one edit by one person, and a
right new body and a wrong one pass equally. What qualifies an expected
side is that it is **independently derivable**: from what the outside
party does, from an artifact of its own origin, or from a requirement
stated somewhere the code under test is not. A value merely written a
second time is not that. The resolvers' interface endpoint is one
assertion carrying both: the address and the port halves of
`f'http://{address}:{port}'` are the accessor's own body written again,
symbolic on both sides, so they hold the accessor to the census's
constants and would pass any value those constants took; while the
scheme and the shape — plain HTTP, the census address rather than the
vhost, that port, no path — are what the appliance answers and the
overlay flow rule admits, which an endpoint drifting from them fails
against nowhere before a live apply. That half is the earned content.

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
