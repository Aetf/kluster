# Style Rules

How code and prose in this repository are written, whatever the
language. The review gate holds every change to it
([framework/dispatch.md](../framework/dispatch.md) §3). Topics:

-   **[python.md](python.md)** — general Python readability, on top of
    the Google Python Style Guide.
-   **[pulumi.md](pulumi.md)** — architecture and style for the Pulumi
    programs: layering, components, providers, data placement.

The rules below apply to everything.

## Illegal states are unrepresentable

The design goal behind most rules here: a mistake should fail to
type-check, fail to parse, or be impossible to write — not be caught at
apply time, and not depend on a reviewer noticing. Values only correct
together are declared together; an API that must be called a certain
way accepts only that way; a state machine that has no "half done"
carries no flag that says so.

## Naming

**Descriptive over metaphorical.** A name states what the thing is, not
what it is like. No jargon-for-flavor when a plain compound says it:
device-services beats estate, initial-state-file beats seed. Rename
cost is paid once; decoding cost is paid by every reader.

**One term per concept, everywhere.** When two similar things coexist
(the ZeroTier overlay, the LAN networks), every class, method, and
variable says which one it means; a word that could mean either is
renamed until it cannot.

**Same shape for same role.** Modules exposing the same kind of entry
point use the same form and name. A reader should be able to guess a
module's API from its role.

## Comments and docs

**Plain speech, direct.** Say the constraint, then stop. No rhetorical
detours, no jargon where a plain word exists. If a comment needs three
sentences of scene-setting, the code or the name is wrong; fix that
instead.

**Generic code never names specific use-cases.** A reusable class
documents its contract; who uses it today belongs to the caller or the
design doc.

**Comments state constraints the code cannot show** — not history, not
what the next line does, not where an idea came from. Every artifact is
as-built: comments, commit messages, docs describe what is, without the
story of how it got there.

**A count is written only where it introduces a closed enumeration in
the same paragraph.** There the number is a reading aid the reader
verifies on the spot, and anyone who grows the list is already looking
at it. Everywhere else it is a liability: a set that later work appends
to, and any set counted anywhere other than immediately above its own
members, is named without a number — "a decision that moved during
construction", not "one decision moved". Dropping the count turns a
sentence that goes **false** as the set grows into one that goes
**incomplete**, and incomplete fails open: a reader who follows it
finds more than was promised instead of hunting for something that is
no longer there. The outward-facing half of the same rule: another
document's count is never restated at all, whatever shape it has
there — cite the document and leave the quantifier where the members
are.

**When a change makes a claim false, sweep for the claim, not for the
identifier that moved.** Searching for the symbol finds call sites; the
sentence that asserted something about them is prose, and only a search
for the claim itself finds it. **That sweep is
newline-insensitive.** Prose here is soft-wrapped, so a multi-word
claim is split across lines wherever the wrap happens to fall, and a
line-oriented match for the phrase returns nothing and reports clean —
the pattern would have to anticipate the break, which the writer of the
pattern cannot do.
Collapse whitespace before matching, in a few lines of Python over the
files, or search for the rarest single word in the phrase and read the
hits. Under-reporting is the failure mode that matters here, because it
is silent and looks exactly like a clean sweep. **A sweep that found
nothing names the patterns it used**, in the pull request or the commit
message: a negative result nobody can reproduce is not a result.

**Docs layer like the code.** `docs/framework/` documents mechanisms
(how this repo does Pulumi, CI, testing, and how work is dispatched)
and names no kluster design decision; `docs/declarative/`, `docs/physical/` and `docs/cluster/` own
the design; `docs/rfc/` holds the accepted proposals those documents
were changed by, as history rather than as reference; this directory
owns how things are written.

## Review gate

Every pull request passes an independent review against these rules
before merge; how that review is run and by whom is
[framework/dispatch.md](../framework/dispatch.md) §3. Major structural
changes go the other way around: an RFC in [`docs/rfc/`](../rfc/)
states the desired end state, names the design documents its content
must land in once built, and is approved before implementation starts.
