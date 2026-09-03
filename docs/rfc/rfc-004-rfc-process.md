# RFC 004: The RFC Process

*   **Status:** Accepted, 2026-09-02. The operator ruled on the three
    questions this document opened, and the answers are written into the text
    rather than left in the thread. The mechanism §3 to §9 states now lives at
    [framework/rfc.md](../framework/rfc.md), which is what a reader follows to
    run an RFC; where this text and that document disagree, the document is
    right. This document keeps the argument, the alternatives weighed and the
    record of what the process replaced. §12's other two slices — AGENTS.md's
    pointer and the `decision/*` label descriptions — are outstanding.
*   **Created:** 2026-09-02
*   **Updated:** 2026-09-02 — three wording points settled as §12's first
    slice moved the mechanism: a promotion gives §5.4's `rfc-NNN slice K:`
    title prefix to slices still open and leaves merged ones with the title
    they merged under (§3.2); a promoted design's **Created** is the date of
    the pull request carrying the document, with the design's own date in the
    Status line (§4.1); and the status word and the index row are written by
    the final push before the merge (§5.1).
*   **Authority:** AGENTS.md, [framework/dispatch.md](../framework/dispatch.md)
    and the style rules (`docs/style/`) are what this document obeys. Where
    they are silent, a rule proposed here is marked **new rule** — which is
    most of it, because the subject is the one mechanism nothing states.
*   **Companion:** [rfc-002](rfc-002-src-layout-and-the-gateway.md)
    established the form; rfc-003 is the first document that carries all of
    §4's sections and is therefore the shape to copy. rfc-002 predates four of
    them — it has no precedent table, no *what does not propagate*, no landing
    table and no open questions — so it is precedent for the argument, not for
    the checklist. Both are cited below rather than re-argued.
*   **In scope:** when an RFC is required and when an ops issue is enough; the
    sections an RFC carries; the lifecycle and the labels that carry it; the
    operator's review gate; amendment after acceptance; numbering, naming and
    the index; and what an RFC owes the style rules and the design documents.
*   **Out of scope:** the dispatch protocol itself — path ownership,
    worktrees, concurrent dispatchers, the two-angle review every pull request
    gets, the board — which is dispatch.md's and is cited here, not restated;
    the ops repository's labels other than `decision/*`, which dispatch.md §4
    and the labels' own descriptions carry; and the roadmap and what any
    milestone contains, which the roadmap issue carries.

--------------------------------------------------------------------------------

## 1. Context & Problem Statement

Three RFCs exist, two of them are built, and every convention they share is
there by imitation. dispatch.md §3.1 says a milestone opens with one and that
any other major structural change runs the same sequence; §4.1 defines the
three `decision/*` states an operator ruling moves through. Between those two
sentences and a finished document sits everything an author actually has to
get right: which changes need this treatment, what the document must contain,
who moves which label and when, what the operator's approval attaches to, and
what happens to the text after the code it describes exists.

The gap is not theoretical. Two amendment conventions are live and nothing
says which applies when: one design was revised by a new comment that scoped
itself as an amendment, and one review disposition was rewritten by editing it
in place with an italic *(edited: …)* note. Both are right, and the difference
between them is not the one a reader would guess — the edited disposition was
not wrong when it was written either, it was overtaken. The test that decides
them is §7. The design gate has also run twice with opposite polarity:
rfc-002's decision issue was closed once its design was approved and its
slices filed, while rfc-003's stays open at `decision/pending` with six slices
filed and three of them already open as draft pull requests. One of those is the rule.

The cost of leaving it implicit is paid by the next author, who reads rfc-002
and copies whatever they happen to notice — and by the operator, whose review
time goes to format rather than to design.

**This document is the process it describes**, and went through it: proposed
as a pull request against this repository, reviewed before it reached the
operator, ruled on through its decision issue's label loop, and cut into §12's
slices on approval.

--------------------------------------------------------------------------------

## 2. What is inherited, and what is decided here

Everything in the left column is settled elsewhere; this document says only
what it means for an RFC.

| Precedent | Applied here |
| --- | --- |
| dispatch.md §3.1 — a milestone opens with a design RFC, and any other major structural change runs the same sequence | the requirement test of §3, which says what "major structural" is |
| dispatch.md §4.1 — the three `decision/*` states | the lifecycle of §5, which binds them to an RFC's states and settles which party sets each one |
| dispatch.md §3 — every pull request gets an independent review before merge | the pre-operator review of §6.1 |
| dispatch.md §4 — corrections edit in place with an *(edited: …)* note | the amendment rules of §7, which say where that applies and where it does not |
| dispatch.md §1.1 — a brief carries the issue, the owned paths, the gate | what a slice issue must carry (§5.4) |
| style/README.md — docs layer, and `docs/rfc/` holds accepted proposals as history | §9, and the marker rule for a proposed style rule |
| rfc-002's status header — it names the documents its content now lives in, and where what was built disagrees, the design document wins | the deviation rule of §7.3 |
| rfc-003 §2, §15, §17, §18 — the precedent table, what does not propagate, the landing table, the slice list | the required sections of §4 |

Two things this document decides that no precedent covers at all: which of the
two live amendment conventions applies to what (§7), and which polarity of the
design gate is the rule (§5.3).

--------------------------------------------------------------------------------

## 3. When an RFC is required

**New rule.** The test is **whether the work would benefit from the process**:
whether it will take several rounds of iteration to converge, and whether
somebody will later need to find the reasoning rather than the result. Work
that would benefit gets a document. Work that would not is a plain ops issue,
or a design posted on one (§3.2), however large it runs.

### 3.1 What such work usually looks like

Four symptoms. They are what benefiting work has looked like so far, not a
checklist that decides on its own — a change meeting one of them is still a
design on its issue when it will converge in one pass of §5.3's loop and
nobody will come looking for the argument later.

1.  **It opens a milestone.** dispatch.md §3.1, unchanged. This one is a
    requirement rather than a symptom: a milestone's design is read by every
    dispatch cut from it.
2.  **It states or changes a rule** in `docs/style/` — anything a reviewer
    would hold a later change to.
3.  **It moves a boundary more than one program, package or document has to
    agree on**: the source layering, the stack decomposition, where a census
    lives, how providers are built, what crosses a stack boundary.
4.  **It reverses something an accepted RFC or a design document states.**

Several issues that only make sense together, in an order, are a symptom of
those rather than a fifth of their own: work that large usually crosses a
boundary or writes a rule, and where it does neither it does not need this
document's machinery.

**Size is not the test either.** A thousand lines of new application component
built to the contract in
[declarative/workloads.md](../declarative/workloads.md) is an ops issue: the
contract already decided everything an RFC would decide, and nothing about it
will iterate. A forty-line change to `docs/style/pulumi.md` is an RFC, because
every future reviewer has to be able to find why the rule says what it says.
Bug fixes, chart bumps, version pins, renames inside one module, document
corrections and new resources declared under an existing pattern are ops
issues however large they run.

**An RFC is not the way to ask a question.** A single ruling the operator has
to make — a trade-off with two defensible answers, a name, a risk someone has
to accept — is an ops issue carrying `decision/pending` (dispatch.md §4.1),
and it is answered in a comment. An RFC is for a design: a set of decisions
that hold each other up and are read together later. Filing the former as the
latter buys a week of writing for an answer that fits in a paragraph.

### 3.2 A design on its own issue, and how it is promoted

A design that will converge in one round — one pass of §5.3's loop: posted,
answered, revised, ruled — changes no rule, and lands in the design documents
rather than in a rule is **posted as a design on its own decision issue**,
ruled on there, and cut into slices exactly as §5.4 describes. It is the
cheaper half of the same machinery, and none of the rest of this document
stops applying to it: the label loop of §5.3, the dispositions of §6.2 and the
slice discipline of §5.4 are the same. What an RFC adds is a durable document,
which is worth its cost only when someone will read it.

How much a design may contain without needing one: the gateway persistence
design ran exactly one pass, answered seven points in a single revision,
landed in five design documents, and stayed a design. The count that matters
is passes of the loop, not points, documents or slices.

**A design issue becomes an RFC when it turns out to be bigger than it
looked** — a second pass of that loop nobody expected, a rule that has to be
written after all, a decision a later reader will need the argument for. **New
rule**, and it is a promotion rather than a restart:

*   **The issue stays.** It keeps its number, its labels, its milestone and
    its place in the ledger, and becomes the RFC's decision issue from that
    moment (§5.2). Its label state carries over unchanged: a design sitting at
    `decision/responded` is an RFC sitting at `decision/responded`.
*   **The title gains `rfc-NNN`** — the lowest number not already spent — in
    the same action as the promotion. §8's claim rule is unchanged; the
    promotion is simply when the claim happens, rather than at the first
    dispatch.
*   **The posted design is the first draft.** Its content moves into the
    document as the context and the decisions, and the comments that revised
    it stay where they are as the record of how it got there. What the
    document adds is what §4 requires, and what an issue thread never had:
    the precedent table, what does not propagate, the landing table, the
    slices.
*   **A ruling already given stands for what it covered.** The document still
    goes to §6.1's review and back to the operator as a pull request, because
    the sections it adds have not been read; a ruling on the design is not
    withdrawn by the promotion and is not asked for twice.
*   **Slices already cut stay cut.** They keep their numbers and their briefs,
    and §12's list names them as they are rather than re-deriving them. Slices
    still open gain §5.4's `rfc-NNN slice K:` title prefix in the same action
    as the promotion; ones that have already merged keep the title they merged
    under, because a merged title is a link somebody has already followed. One
    that has already merged is named and marked as landed: the document does
    not re-propose what is built, and §7.3 governs it from the start — what is
    built is canon, and the document records any deviation rather than
    restating the plan.

**Whoever notices may propose one.** An agent that finds, mid-dispatch, that
its issue has grown into one of these stops and reports rather than widening
its scope (AGENTS.md); the dispatcher promotes the issue or files a new one.
The finding is not the agent's to build.

--------------------------------------------------------------------------------

## 4. What an RFC contains

**New rule**, from what rfc-002 and rfc-003 already do. The order is fixed so
that a reader who knows the shape can find a section without reading the
document, and a reviewer can check the list rather than form an impression.

### 4.1 The status header

A bullet list, before anything else, carrying in this order:

*   **Status** — one of the words of §5.1, with the date it was reached. For
    an implemented RFC, the header is also where the content's new homes and
    any construction deviation are named (§7.3).
*   **Created** — the date the document was first proposed. For a promoted
    design (§3.2) that is the date of the pull request carrying the document,
    because the design it is drafted from was never a document; the design's
    own date belongs in the Status line.
*   **Updated** — one dated line per revision made after acceptance, saying
    what that revision changed (§7.2). Absent until there is one; rfc-001
    carries two.
*   **Authority** — what the document obeys, and the sentence that a rule it
    proposes beyond that is marked **new rule**.
*   **Companion** — the RFCs it builds on, whose precedents it cites instead
    of re-arguing. Omitted when there are none.
*   **In scope** — enumerated, not gestured at.
*   **Out of scope** — enumerated too, each item with where it is settled
    instead. This is the half that saves the review, because most of what a
    reader will otherwise raise is something the author deliberately excluded.

There is no author line. The document belongs to the repository and `git log`
says who wrote it; rfc-001 carries one because it predates this rule, and it
is not removed.

### 4.2 The body

1.  **Context and problem statement.** What is wrong now, in terms of the
    system rather than of anyone's plans, and why this is the moment — the
    facts that make the change cheap now and expensive later. rfc-002's two
    (nothing applied yet, the apps layer not started) are the shape.
2.  **What is inherited, and what is decided here** — a two-column table of
    precedent against where it lands. Required whenever the document leans on
    an earlier RFC or a style rule; it is what keeps a second document from
    re-arguing the first.
3.  **The decisions**, each with its argument, in whatever sectioning the
    subject wants. Every rule the style documents do not already state is
    marked **new rule** at the point it is stated (§9).
4.  **What is already conformant** — the parts of the area the document
    deliberately does not touch, named so that a reader does not go looking
    for work that is not there. Optional; cheap; rfc-003 §14 is the shape.
5.  A section headed **What does not propagate**, holding the parts of a
    cited precedent, or of a weighed alternative, that are deliberately not
    applied, each with its reason. Required in every document that carries
    the precedent table.
6.  **The documents this content lands in** — a table of document against
    what lands there. **No RFC is approved without it**: it is what lets the
    proposal finish, because it names the diff that ends it. An RFC whose
    content lands nowhere describes something nobody will be able to find
    later.
7.  **How we get there** — the slices (§5.4).
8.  **Open questions** — what is knowingly undecided, and what will decide it.
    "Settled on first contact" is a legitimate answer and should say so, as
    [declarative/README.md](../declarative/README.md) §4 does; silence is not.

Alternatives weighed and rejected belong beside the decision they lose to,
with the reason, rather than in a section of their own — that is where a
reader is asking the question. rfc-002 §7.1 and rfc-003 §7.4 are the shape.

--------------------------------------------------------------------------------

## 5. The lifecycle

### 5.1 Three states, and one that arrives later

**New rule.** The status word in the header is one of:

*   **Proposed** — written, under review, nothing built. The document is
    freely rewritten (§7.1).
*   **Accepted** — the operator has approved the design and the pull request
    has merged. The slices may be cut and dispatched. The text is now stable:
    it changes only by the rules of §7.2. The word and its date are written by
    the final push to the RFC's own pull request, after the ruling and before
    the merge, together with the index row — so no RFC ever sits on
    `main` claiming to be proposed when it is not.
*   **Implemented** — every slice has merged. The header names where the
    content now lives, and the body is history (§9). The word is written by
    the slice that lands the last piece (§5.4); the operator's acceptance of
    the built result is the standing receipt on the gate issue (§6.2), not a
    second header state.
*   **Superseded by** `rfc-NNN`, with the date — an implemented RFC whose design
    a later one replaces. The body is not edited; the header is (§7.4).

There is no rejected state, because a rejected RFC never lands: its pull
request is closed unmerged, and the argument stays there. The number is spent
either way (§8).

*Approval* is the operator's act; *Accepted* is the state the document
carries after it. One word each, and neither is used for the other.

**A promoted design enters at Proposed** (§3.2), whatever its issue had
already been ruled: the ruling stands for the design it covered, and the
sections the document adds have not been read. Where the work it describes is
already built, the document is written as history and reaches **Implemented**
at its own merge, because there is nothing left to cut.

Going backwards is allowed and is not an event: an accepted RFC whose core
design is overturned before it is built returns to **Proposed** and runs the
gate again (§7.2).

### 5.2 The three artifacts

An RFC is three things in two repositories, and confusing them is the most
common way to lose track of one:

| Artifact | Where | What it carries |
| --- | --- | --- |
| The document | a pull request against this repository | the design, and after merge the accepted text |
| The decision issue | the ops repository | the operator's ruling, and the `decision/*` label that is the queue |
| The slices | ops issues, one per dispatch | the work, cut from the document after acceptance |

**A promoted design already has two of the three.** The decision issue exists,
and some slices may too (§3.2); the promotion adds the document, and nothing
else moves.

The document is public, and the issues are not, so **the document argues from
substance, never from an issue number**: a decision it depends on is stated in
a sentence a stranger can check, not cited as a ticket they cannot open.
Implementation-period issues live in the ops repository and not in a
checked-in list (AGENTS.md), and the index of §8 obeys the same rule: a
proposal's status names the pull request it is waiting in, which is public,
rather than the issue it is waiting on, which is not.

### 5.3 The labels are a hand-off, and who moves them

The three states of dispatch.md §4.1 ride the **decision issue**, not the pull
request: this repository's pull requests carry no `decision/*` label, because
the labels and the operator's queue live in the ops repository.

**A decision issue is usually not filed as one.** dispatch.md §4.1 says the
label is carried from the moment the issue is filed, which is true of an issue
whose whole purpose is a ruling. An RFC's issue is filed as a task, is claimed
and dispatched like any other, and becomes a decision issue at the handoff —
when the document is ready and the ball moves to the operator. Every RFC
issue filed since the labels existed has gone that way. Slice 1 reconciles
dispatch.md's sentence with it.

**New rule**, from how the loop actually runs: **the label is set by whoever
is putting the ball down, for the party picking it up, in the same action as
the comment.** It is a hand-off flag, not a verdict pronounced on someone
else's work.

*   An agent or dispatcher that posts a design, a revision, or an answer sets
    `decision/pending`. The operator's queue is exactly that filter, so an
    unlabeled proposal is not waiting — it is invisible.
*   The operator, replying with anything short of approval, leaves
    `decision/responded`. The agent revises or investigates, and posting the
    result sets `decision/pending` again. The loop runs as many times as it
    needs to.
*   `decision/lgtm` is the operator's, and means the latest thing in the issue
    is approved and clear to build.

**`in-flight` and `decision/*` are orthogonal, and an issue may carry both.**
They answer different questions: `in-flight` says a dispatcher has claimed the
issue (dispatch.md §2), and a `decision/*` label names the party now holding
the ball. An issue waiting on the operator is still claimed — dropping the
claim there is exactly what would let a second dispatcher pick it up — and
dispatch.md §4 takes `in-flight` off at the merge or the abandonment, neither
of which has happened while the operator reads. Slice 1 states this in
dispatch.md, where both labels are defined.

**Approval, then slices, then merge.** rfc-002 is the precedent for that
sequence and not for the mechanism that carries it: its design was approved,
its slices were filed, and its decision issue was closed by a comment
written by hand minutes later — the pull request carried no closing keyword,
and the three-state label machine did not exist yet. **New rule**, adopting
dispatch.md §4's mechanism for the sequence from here on: an RFC's pull
request carries `Closes` for its decision issue, so the merge moves the ledger
by itself. rfc-003's pull request references its issue without the keyword
today, and gains the keyword — and updates the index row this document
adds — in the same commit that writes its status when the ruling lands.

**The exception is building ahead**: where the text already fixes a slice's
content, that slice may be opened as a **draft** pull request before approval,
provided none of them merges until the design issue is closed. rfc-003 is
running that way. It buys elapsed time and costs a rebase if the design moves,
and the risk belongs to the dispatcher who chose it — an approval that changes
a slice's premise invalidates the draft, and no reviewer owes it a second
read.

### 5.4 Slices

A slice is **one dispatch**: one agent, one worktree, one pull request, one
set of owned paths (dispatch.md §1). Work that does not fit is split until it
does. The slice list is ordered so that nothing is moved twice.

**New rule.** Every entry says what it contains **and what done means for it,
in one sentence** — the same sentence dispatch.md §1.1 already demands of
every brief, written once here instead of invented per dispatch. Where the
gate alone settles it the sentence says so; where it does not, it is a
condition somebody can check, as rfc-003's slices are, each ending in a
preview that shows no replacement and no delete. **The slice list is therefore
the deliverables list**: every entry names a thing that will exist and the
sentence that says when it does, and an RFC with no such list cannot be told
apart from an unfinished one.

**New rule.** A slice issue names its RFC and the section it is cut from **in
its title**, and so does the pull request that closes it. The form is the
file's own: `rfc-004 slice 3: …`, and a section reference is `rfc-004 §5.4` —
one spelling, lowercase, matching the file name, because titles already carry
`RFC-002 slice 1` and `RFC 002 §10.2` and two spellings are one too many. The
title is the only link between an issue and the design it comes from, so it is
not optional.

**Every slice is an ops issue**, including the ones that produce no pull
request — a label description, a setting in the forge, a change in another
repository. A slice with a pull request is closed by the merge; one without is
closed by the dispatcher with a comment saying what was done. Nothing is a
slice that has no issue, because the slice list would then stop being the
roll.

**The slice list is the roll.** An RFC is done when its last slice merges;
nothing else tracks completion, and no per-RFC tracking issue is opened
(§10). The slice that lands the last piece also flips the status header to
**Implemented** and fills in where the content now lives — the same rule
AGENTS.md applies to every other document, that the documentation a change
makes true ships inside it.

--------------------------------------------------------------------------------

## 6. The review gate

### 6.1 Before the operator sees it

**New rule**, extending dispatch.md §3 rather than contradicting it: **an RFC
is independently reviewed before it is handed to the operator**, by an agent
that did not write it, against §4's section list and the style rules. The
handoff comment says that review ran.

dispatch.md §3 already requires an independent review of every pull request
before merge; this says which review comes first and what it is for. The
operator's attention is the scarcest thing in the loop, and a missing
out-of-scope list or an unmarked new rule costs a whole round trip to
discover. Format is stopped before the document reaches the operator, so that
design is all the reading costs.

### 6.2 The operator's review

The gate is per milestone, not per RFC (dispatch.md §3.1): one issue covering
a milestone's worth of change, read as a doc-versus-implementation audit plus
the operator's own design review. Its shape, as it runs:

*   **The issue body is the review**, numbered per area, with `lgtm` written
    against the areas that are clean, so the reader can tell "reviewed and
    fine" from "not reached".
*   **Every point gets a disposition, in the numbering the operator used**, in
    one reply rather than scattered across the thread. **New rule:** there are
    exactly three dispositions, and every point takes one —
    1.  **answered with no change**, with the reason the point does not hold
        or the fact it missed;
    2.  **dispatched**, as a slice issue, named;
    3.  **escalated**, as a design proposal posted in the reply for the
        operator to rule on in the thread. Keeping it there is what keeps the
        fix list checkable in one place; a separate `decision/pending` issue
        is opened only when the ruling turns out to be someone else's to
        make.

    Disagreeing is a disposition, not an omission: a point argued against with
    reasons is a proper answer, and one silently unaddressed is the failure
    this rule exists to prevent.
*   **Approval attaches forward.** The ruling that concluded rfc-002's review
    was conditional in its own words: once every fix named in the issue is
    made, that implementation counts as approved. **New rule**, generalizing
    it: the operator may approve against a fix list, and the approval takes
    effect when the list is exhausted, with no second review. What makes that
    safe is the completeness of the dispositions above — the list is closed,
    so "exhausted" is a fact rather than a judgment.
*   **The gate issue stays open**, carrying `decision/lgtm` as a standing
    receipt for the milestone — unlike an RFC's own decision issue, which the
    merge closes (§5.3). Nothing closes the gate; it is the record that the
    milestone was accepted and on what terms.

Findings the operator raises that turn out to be someone else's decision
become their own `decision/pending` issues rather than growing this one
(dispatch.md §3).

**An RFC that opens no milestone has no gate of its own**, and does not need
one. It is **Implemented** when its last slice merges (§5.1), and the areas it
touched are audited at the next milestone's checkpoint along with everything
else that landed in the meantime. rfc-003 is the first document this applies
to.

--------------------------------------------------------------------------------

## 7. Amendment

Four stages, and one test running through all of them: **has the other party
— the operator for a design or a disposition — already acted on the text?**
Text somebody has answered is never edited; it is
revised by a new comment scoped to what changes. Text nobody has acted on yet
is fixed where it stands — and so, whenever it is found, is a statement that
was false when it was made.

### 7.1 While it is proposed

The document is a pull request; it is rewritten in place and the diff is the
record. Review comments are answered on the pull request. Nothing in the text
marks a revision, because nothing has been accepted yet.

**New rule**, settling the two conventions the decision issue carries today:

*   **Answered text is revised by a new comment.** Once the operator has
    replied, the text replied to is frozen: the revision is its own comment,
    in the operator's numbering, scoped to what it changes — *everything not
    named here stands as written*. Editing it instead would erase the thing
    the reply was about, and a reader following the thread needs both halves,
    in order, to see what moved. The unit is the comment, not the point: a
    reply to any part of it freezes all of it, because the operator read the
    whole to answer any of it, and a revision then carries every change,
    including the ones the reply passed over.
*   **Unanswered text is fixed where it stands**, with an italic
    *(edited: …)* note saying what changed (dispatch.md §4). A disposition
    rewritten while the review is still open, a pull request body corrected
    before any ruling: nobody has acted on either, so there is nothing to
    preserve and every reason not to make the next reader assemble the truth
    from two places. A pull request's description is not text anyone acts on
    either; it describes the diff (dispatch.md §1.1) and follows it, edited in
    place with the note before or after any ruling, merged or not.
*   **A false statement is corrected in place whenever it is found**, answered
    or not, by the same note. This is the second clause and not the test: a
    text can be overtaken without ever having been wrong, and that alone does
    not license an edit.

The three cases on record decide cleanly. A design revision posted after the
operator had responded is a new comment. A recommendation rewritten mid-review
because checking the packages turned up a better option is an edit — nobody
had acted on it, though it was not wrong when written. A pull request body
corrected before the ruling is an edit.

### 7.2 After acceptance, before it is built

**New rule**, and the distinction the two live conventions were missing:

*   **A detail moves** — a boundary case found in construction, a name, a
    step's order. The RFC text is edited in place, in the pull request that
    changes the implementation with it, and the header gains a dated
    `Updated:` line saying what changed. rfc-001's header carries two of them
    and is the form. There is no *(edited: …)* note in the file, because a
    file in git already has its history; the note is for issue bodies and
    comments, which do not (dispatch.md §4).
*   **The core design is overturned** — the thing the RFC exists to decide is
    no longer the answer. The document returns to **Proposed**, is rewritten,
    and runs the gate again: the decision issue reopens at
    `decision/pending`. No supplementary document is written, and no amendment
    is layered on top (§10), because nothing is built yet that a second
    document would have to stay compatible with, and one document per design
    is what a later reader can afford to read.

The discriminator is not the size of the diff. It is whether a slice already
merged has to be re-argued.

### 7.3 After it is built

**What is built is canon, and the RFC is history.** Where the text and a
design document disagree, the design document is right — rfc-002's own header
says so, and this generalizes it.

**New rule.** A decision that moved during construction is recorded **in the
status header**, not by editing the body: the body stays the text that was
accepted, and the header says what was built instead and why the accepted
answer could not stand. rfc-002's endpoint is the precedent — a provider
imports no `conventions`, so the address stayed a declared resource input, and
the header says exactly that. The design document carries the truth in full;
the header carries only enough that a reader of the RFC knows not to trust
that paragraph.

The recording ships in the slice that deviates, not later.

### 7.4 Superseding

A design defect found after the RFC is implemented is a **new RFC**, whose
header names what it supersedes. The old document's body is never edited; its
status becomes `Superseded by rfc-NNN`, with the date, and the index carries
the same. An implemented RFC is a closed record of what was decided and on
what evidence, and the reason to keep it intact is that the next person to
propose the same thing needs to know why it was decided that way the first
time.

--------------------------------------------------------------------------------

## 8. Numbering, naming, and the index

**New rule**, all of it mechanical:

*   Files are `docs/rfc/rfc-NNN-<slug>.md`, with `NNN` zero-padded to three
    digits and the slug naming the subject in lowercase words joined by
    hyphens — not the milestone, which is not what a reader searches for.
*   The title line is `# RFC NNN: <Subject>`.
*   **The number is claimed by writing it into the ops issue's title**, in
    the same action as the `in-flight` label, and it is the lowest number not
    already spent. The label alone would not prevent the collision it exists
    to prevent: it carries no number, so two dispatchers would each compute
    "the lowest not spent" from artifacts neither had created yet. Putting
    `rfc-005` in the title creates the artifact, and dispatch.md §2 already
    has both dispatchers reading each other's issues before they claim.
*   **A number is spent** once it appears in an ops issue title, a branch
    name, a pull request or a file, and **is never reused** — including by a
    proposal that was rejected and never landed.
*   **A file is never renamed after it merges.** The slug is wrong forever
    rather than the links being broken forever.

**The index is [README.md](README.md) in this directory**: every RFC, its
subject, its status, and where its content lives now. **New rule:** it lists
proposals too, unlinked until their file lands on `main`, and the row is
written or updated by the pull request that changes the fact — the RFC's own
pull request when it is proposed and again when it is accepted, and the pull
request of the last slice when it is implemented. An index maintained apart
from the thing it indexes is a second truth, and it drifts.

--------------------------------------------------------------------------------

## 9. The style rules, and the design documents

**An RFC obeys the style rules; it does not quietly outvote them.** That is
what the header's authority line commits it to. Two consequences:

*   **A rule the style documents do not state carries the marker** where it is
    stated, and the landing table (§4.2's item 6) names the style document it
    belongs in. rfc-002 and rfc-003 both do this.
*   **A proposed rule becomes binding when the slice that writes it into the
    style document merges** — not when the RFC is accepted. **New rule**, and
    it matters: a reviewer holds a change to `docs/style/`
    ([style/README.md](../style/README.md)), and reads that, not the RFC
    archive. A rule that lives only in an accepted proposal is enforced by
    whoever remembers it.
*   **An RFC that needs to contradict a standing style rule says so and
    changes the rule**, in the same landing table. A silent contradiction is a
    defect the review of §6.1 catches.

The same split governs everything else the document produces. `docs/style/`
owns how things are written, `docs/framework/` owns the mechanisms, and
`docs/cluster/`, `docs/physical/` and `docs/declarative/` own the design of
what is built; `docs/rfc/` holds the accepted proposals those documents were
changed by, as history rather than as reference
([style/README.md](../style/README.md)). A reader who wants to know how
something works today reads the design documents. The RFC keeps what they
cannot: the alternatives weighed, the measurements, and the reason the losing
option lost.

**This document holds the mechanism only until §12's first slice moves it.**
The process stated above is *reference*: it is read to find out how something
works today, which is exactly what style/README.md says this directory does
not hold. The answer is not an exception to that sentence but an end to the
condition — the mechanism lands at `docs/framework/rfc.md`, where mechanisms
live, and this document keeps what an RFC keeps: the argument, the
alternatives weighed, and the record of what the process replaced. Until that
slice merges, one document here is both, and that is a transitional state with
a slice against it rather than a rule with an exception in it.

--------------------------------------------------------------------------------

## 10. What does not propagate

Mechanisms considered for this process and deliberately not taken. Each is
sound elsewhere; each costs more here than it returns.

*   **A per-RFC tracking issue** holding the implementation as a tree of
    sub-issues. The milestone, the slice list of §4.2's item 7 and the
    `Closes` line
    on each slice's pull request already say what is left, and a fourth
    account of it is a fourth thing to keep true. dispatch.md §4's principle
    is that nothing is reported twice by hand that a merge can report once.
*   **`decision/*` labels on pull requests.** They exist in the ops
    repository; the document is in this one. The label rides the decision
    issue and the pull request links to it (§5.2). Two label sets across two
    repositories would need a rule for when they disagree.
*   **A supplementary document that amends an accepted RFC.** Reasonable for a
    proposal with users to stay compatible with; here it would split one
    design across two files that a reader must merge in their head. Before
    implementation the document is rewritten (§7.2); after it, the replacement
    is a full RFC (§7.4).
*   **"Approved" as the status word.** This repository already says
    *accepted*, in the style rules' own description of this directory. One
    term per concept outranks a synonym imported from somewhere else.
*   **A deliverables list** enumerating artifacts. The landing table and the
    slice list carry the same guarantee in checkable form — a slice is an
    issue and a landing row is a diff, while a deliverables list is a promise.
    The bar it exists to set is kept twice over: an RFC with no landing
    table is not approved, and every slice entry carries the sentence that
    says when it is done (§5.4).
*   **A blanket ban on writing implementation code before approval.** The
    rule here is narrower: drafts may exist, nothing merges (§5.3). Nothing
    mechanical stops a draft leaving draft state, so what enforces it is the
    dispatcher that owns the claim and does the merging (dispatch.md §2.4) —
    a smaller promise than a ban, and one somebody is actually in a position
    to keep.

--------------------------------------------------------------------------------

## 11. The documents this content lands in

| Document | What lands there |
| --- | --- |
| framework/rfc.md (new) | the mechanism itself — §3 to §9 — as reference, which is what §12's first slice creates |
| framework/dispatch.md | §3.1 and §4.1 point at that home rather than growing an account of their own; §4.1's "from the moment it is filed" is reconciled with how an RFC issue is actually filed (§5.3); §4 and §2 gain the orthogonality of the two label sets |
| This directory's README | the pointer paragraph and the index, with the rule that the index moves with the fact; the pointer follows the mechanism in the same slice |
| AGENTS.md | one line pointing at the process, beside the pointer to dispatch.md |
| this document | the argument, the alternatives and the record — everything an RFC keeps once its content has landed elsewhere |
| style/README.md | nothing. Its sentence about this directory becomes true again when the mechanism leaves, which is why the exception it would otherwise need is not written |
| style/pulumi.md, style/python.md | nothing. No rule here is about how code or prose is written |

Per AGENTS.md, each of those edits ships inside the slice that makes it true,
not as a follow-up.

--------------------------------------------------------------------------------

## 12. How we get there

Three slices. Each is an ops issue; the third produces no pull request and is
closed by the dispatcher with a comment (§5.4).

1.  **The mechanism moves to `docs/framework/rfc.md`.** §3 to §9 leave this
    document for the framework directory, where mechanisms live; what stays
    here is the argument, the alternatives and the record. The directory
    README points at the new home, and dispatch.md §3.1 and §4.1 point there
    too instead of growing their own account — §4.1's "from the moment it is
    filed" reconciled with §5.3, and §4 and §2 stating that the two label sets
    are orthogonal. **One slice rather than two**, because pointing dispatch.md
    at this document and then re-pointing it after the move would move the
    same line twice, which is the thing a slice order exists to prevent.
    **Done when** nothing in `docs/rfc/` is read as reference and dispatch.md
    names one home for the process.
2.  **AGENTS.md gains one line** pointing at the process, beside the pointer
    to dispatch.md that is already there. After slice 1, so that it points at
    the permanent home and is written once. Its own slice because AGENTS.md is
    serialized (dispatch.md §2): it waits for a window in which no other pull
    request touches that file, and blocks nothing while it waits. **Done
    when** an agent reading only AGENTS.md can find the process.
3.  **The `decision/*` labels get descriptions** in the ops repository saying
    who sets each one and for whom (§5.3), and naming no pull request, because
    the labels ride the issue. Not this repository's to implement. **Done
    when** each of the three labels reads as a hand-off rather than as a
    verdict, closed by the dispatcher with a comment saying so.

Three things ship with this document rather than as slices, because they are
facts about today rather than proposals: the index, which states what each
RFC's status *is*; rfc-001's status header, which said `Accepted` while its
mechanism had been built and in use since 2026-08-15, and which an index
stating otherwise would have contradicted for exactly as long as it stood
(§5.2); and the pointer from
[declarative/README.md](../declarative/README.md) that makes this directory
findable from the design documents.

--------------------------------------------------------------------------------

## 13. Open questions

The three questions this document opened are answered, and their answers are
in the text above rather than here: `in-flight` and `decision/*` are orthogonal (§5.3),
the test is whether the work benefits from the process rather than a checklist
of triggers (§3), and the mechanism graduates to `docs/framework/rfc.md` on
acceptance (§9, §12). The rule that an RFC opening no milestone is Implemented
at its last slice's merge and audited at the next checkpoint (§6.2) went
uncontested and is settled too.

Two remain, and nothing available now would settle either:

*   **Whether a non-milestone RFC's slices need a milestone of their own.**
    Today they land on `Parallel` and are told apart only by their titles,
    which works at three RFCs and is not obviously what works at ten. Settled
    on first contact with the case that hurts.
*   **Whether a rejected proposal should land anyway**, with its argument, so
    that the next author finds it before writing the same thing. §5.1 says no
    on the grounds that a closed pull request holds it well enough. Nothing
    has been rejected yet, so the claim is untested.
