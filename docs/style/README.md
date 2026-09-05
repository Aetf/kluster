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

**A count is written only where the members it counts are in view** —
in the same paragraph, or in the list or the subsections immediately
below it. There the number is a reading aid the reader verifies on the
spot, and anyone who grows the list is already looking at it: a set
that grows is not the problem, distance is. Everywhere else the count
is a liability, and a set counted anywhere other than immediately above
its own members is named without a number — "a decision that moved
during construction", not "one decision moved". Dropping it turns a
sentence that goes **false** as the set grows into one that goes
**incomplete**, and incomplete fails open, because a reader who follows
it finds more than was promised instead of hunting for something that
is no longer there.

**A count that belongs to another document is never restated**,
wherever that document keeps it and however close its own members sit
to it there. The two failures are not the same failure: a stale count
of one's own set leaves the sentence incomplete, while a copied count
can lose its referent altogether — a reader sent to find "the two
criteria" in a document that now states three has nothing to match the
sentence against, and no way to tell which one went missing. Naming the
members as anchors is fine, since a list left short fails open the way
any incomplete sentence does; it is the quantifier that may not travel.
Cite the document and leave the number where the members are.

**When a change makes a claim false, sweep for the claim, not for the
identifier that moved.** Searching for the symbol finds call sites; the
sentence that asserted something about them is prose, and only a search
for the claim itself finds it. Three things keep that search from being
open-ended:

-   **Anchor the pattern on the claim's nouns, not on its predicates.**
    A predicate is where a document's voice varies — "comes from", "is
    read at", "lives in" — while the nouns of a technical claim are
    this repository's own fixed terms. A proximity match between two
    nouns, with no verb in it at all, is the form that survives
    whatever voice the target happens to be written in.
-   **Make a second pass for closure operators near those nouns** —
    "nowhere else", "only", "never", "no other", "and nothing else". A
    change that adds an exception leaves the affirmative form merely
    incomplete but falsifies the closed form outright, and the closed
    form shares almost no vocabulary with the affirmative one. The
    variant hardest to reach by matching predicates is the one whose
    staleness costs the most.
-   **A sweep that found nothing names the patterns it used**, in the
    pull request or the commit message. "It appears nowhere else in the
    repository" is not a result a reviewer can falsify.

**Every pass is newline-insensitive.** Prose here is soft-wrapped, so
the break falls wherever the wrap puts it, and the line a line-oriented
matcher sees holds only half the claim: the match returns nothing and
the sweep reports clean. Collapse whitespace before matching — a few
lines of Python over the files, which does the proximity matching above
in the same pass — or run a matcher in a mode that is not line-oriented
where it has one, or search for the rarest single word in the phrase
and read the hits. Under-reporting is the failure mode that matters
here, because it is silent and looks exactly like a clean sweep.

**Docs layer like the code.** `docs/framework/` documents mechanisms
(how this repo does Pulumi, CI, testing, and how work is dispatched)
and names no kluster design decision; `docs/declarative/`, `docs/physical/` and `docs/cluster/` own
the design; `docs/rfc/` holds the accepted proposals those documents
were changed by, as history rather than as reference; this directory
owns how things are written. What that leaves a sweep free to edit is
[framework/rfc.md](../framework/rfc.md) §5.3: an accepted RFC keeps the
words its decision was made in.

## Review gate

Every pull request passes an independent review against these rules
before merge; how that review is run and by whom is
[framework/dispatch.md](../framework/dispatch.md) §3. Major structural
changes go the other way around: an RFC in [`docs/rfc/`](../rfc/)
states the desired end state, names the design documents its content
must land in once built, and is approved before implementation starts.
