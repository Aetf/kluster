# The RFC Process

How a design is written, approved and built here: when a change needs a
document and when an ops issue is enough, what the document carries, the
states it moves through and the labels that carry them, the operator's
review gate, how it is amended after acceptance, and how it is numbered
and indexed. [dispatch.md](dispatch.md) is the protocol around a
dispatch; this is the mechanism that runs before the dispatches exist.
Anyone about to write an RFC reads this first, and anyone about to
review one holds it to §2.

The argument behind these rules — the alternatives weighed, and the
conventions they replaced — is
[rfc-004](../rfc/rfc-004-rfc-process.md).

## 1. When an RFC is required

The test is **whether the work would benefit from the process**: whether
it will take several rounds of iteration to converge, and whether
somebody will later need to find the reasoning rather than the result.
Work that would benefit gets a document. Work that would not is a plain
ops issue, or a design posted on one (§1.2), however large it runs.

### 1.1 What such work usually looks like

Four symptoms. They are what benefiting work looks like, not a checklist
that decides on its own — a change meeting one of them is still a design
on its issue when it will converge in one pass of §3.3's loop and nobody
will come looking for the argument later.

1.  **It opens a milestone** ([dispatch.md](dispatch.md) §3.1). This one
    is a requirement rather than a symptom: a milestone's design is read
    by every dispatch cut from it.
2.  **It states or changes a rule** in `docs/style/` — anything a
    reviewer would hold a later change to.
3.  **It moves a boundary more than one program, package or document has
    to agree on**: the source layering, the stack decomposition, where a
    census lives, how providers are built, what crosses a stack
    boundary.
4.  **It reverses something an accepted RFC or a design document
    states.**

Several issues that only make sense together, in an order, are a symptom
of those rather than a fifth of their own: work that large usually
crosses a boundary or writes a rule, and where it does neither it does
not need this machinery.

**Size is not the test either.** A thousand lines of new application
component built to the contract in
[declarative/workloads.md](../declarative/workloads.md) is an ops issue:
the contract already decided everything an RFC would decide, and nothing
about it will iterate. A forty-line change to
[style/pulumi.md](../style/pulumi.md) is an RFC, because every future
reviewer has to be able to find why the rule says what it says. Bug
fixes, chart bumps, version pins, renames inside one module, document
corrections and new resources declared under an existing pattern are ops
issues however large they run.

**An RFC is not the way to ask a question.** A single ruling the operator
has to make — a trade-off with two defensible answers, a name, a risk
somebody has to accept — is an ops issue carrying `decision/pending`
([dispatch.md](dispatch.md) §4.1), and it is answered in a comment. An
RFC is for a design: a set of decisions that hold each other up and are
read together later.

### 1.2 A design on its own issue, and how it is promoted

A design that will converge in one round — one pass of §3.3's loop:
posted, answered, revised, ruled — changes no rule, and lands in the
design documents rather than in a rule is **posted as a design on its
own decision issue**, ruled on there, and cut into slices exactly as
§3.4 describes. None of the rest of this document stops applying to it:
the label loop of §3.3, the dispositions of §4.2 and the slice
discipline of §3.4 are the same. What an RFC adds is a durable document.
The count that decides between them is passes of the loop, not points
answered, documents landed in, or slices cut.

**A design issue becomes an RFC when it turns out to be bigger than it
looked** — a second pass of that loop nobody expected, a rule that has
to be written after all, a decision a later reader will need the
argument for. It is a promotion rather than a restart:

*   **The issue stays.** It keeps its number, its labels, its milestone
    and its place in the ledger, and becomes the RFC's decision issue
    from that moment (§3.2). Its label state carries over unchanged: a
    design sitting at `decision/responded` is an RFC sitting at
    `decision/responded`.
*   **The title gains `rfc-NNN`** — the lowest number not already spent
    — in the same action as the promotion. §6's claim rule is unchanged;
    the promotion is simply when the claim happens, rather than at the
    first dispatch.
*   **The posted design is the first draft.** Its content moves into the
    document as the context and the decisions, and the comments that
    revised it stay where they are as the record of how it got there.
    What the document adds is what §2 requires, and what an issue thread
    never had: the precedent table, what does not propagate, the landing
    table, the slices.
*   **A ruling already given stands for what it covered.** The document
    still goes to §4.1's review and back to the operator as a pull
    request, because the sections it adds have not been read; a ruling
    on the design is not withdrawn by the promotion and is not asked for
    twice.
*   **Slices already cut stay cut.** They keep their numbers and their
    briefs, and the document's slice list names them as they are rather
    than re-deriving them. **Slices still open gain §3.4's
    `rfc-NNN slice K:` title prefix in the same action as the
    promotion**; ones that have already merged keep the title they
    merged under. A slice that has already merged is named and marked as
    landed: the document does not re-propose what is built, and §5.3
    governs it from the start — what is built is canon, and the document
    records any deviation rather than restating the plan.

**Whoever notices may propose one.** An agent that finds, mid-dispatch,
that its issue has grown into one of these stops and reports rather than
widening its scope (AGENTS.md); the dispatcher promotes the issue or
files a new one. The finding is not the agent's to build.

## 2. What an RFC contains

The order is fixed, so that a reader who knows the shape can find a
section without reading the document, and a reviewer can check the list
rather than form an impression.

### 2.1 The status header

A bullet list, before anything else, carrying in this order:

*   **Status** — one of the words of §3.1, with the date it was reached.
    For an implemented RFC, the header is also where the content's new
    homes and any construction deviation are named (§5.3).
*   **Created** — the date the document was first proposed. For a
    promoted design (§1.2) that is the date of the pull request carrying
    the document, and the design's own date belongs in the Status line.
*   **Updated** — one dated line per revision made after acceptance,
    saying what that revision changed (§5.2). Absent until there is one.
*   **Authority** — what the document obeys, and the sentence that a
    rule it proposes beyond that is marked **new rule**.
*   **Companion** — the RFCs it builds on, whose precedents it cites
    instead of re-arguing. Omitted when there are none.
*   **In scope** — enumerated, not gestured at.
*   **Out of scope** — enumerated too, each item with where it is
    settled instead. This is the half that saves the review, because
    most of what a reader would otherwise raise is something the author
    deliberately excluded.

There is no author line. The document belongs to the repository and
`git log` says who wrote it; rfc-001 carries one because it predates
this rule, and it is not removed.

### 2.2 The body

1.  **Context and problem statement.** What is wrong now, in terms of
    the system rather than of anyone's plans, and why this is the moment
    — the facts that make the change cheap now and expensive later.
2.  **What is inherited, and what is decided here** — a two-column table
    of precedent against where it lands. Required whenever the document
    leans on an earlier RFC or a style rule; it is what keeps a second
    document from re-arguing the first.
3.  **The decisions**, each with its argument, in whatever sectioning
    the subject wants. Every rule the style documents do not already
    state is marked **new rule** at the point it is stated (§7).
4.  **What is already conformant** — the parts of the area the document
    deliberately does not touch, named so that a reader does not go
    looking for work that is not there. Optional; cheap.
5.  A section headed **What does not propagate**, holding the parts of a
    cited precedent, or of a weighed alternative, that are deliberately
    not applied, each with its reason. Required in every document that
    carries the precedent table.
6.  **The documents this content lands in** — a table of document
    against what lands there. **No RFC is approved without it**: it is
    what lets the proposal finish, because it names the diff that ends
    it. An RFC whose content lands nowhere describes something nobody
    will be able to find later.
7.  **How we get there** — the slices (§3.4).
8.  **Open questions** — what is knowingly undecided, and what will
    decide it. "Settled on first contact" is a legitimate answer and
    should say so; silence is not.

Alternatives weighed and rejected belong beside the decision they lose
to, with the reason, rather than in a section of their own — that is
where a reader is asking the question.

A row naming `pkg.module.NAME`, in a table or in a decision, names that
value's home rather than the import a caller writes
([style/python.md](../style/python.md) has the rule and what answers the
caller's question instead).

## 3. The lifecycle

### 3.1 Three states, and one that arrives later

The status word in the header is one of:

*   **Proposed** — written, under review, nothing built. The document is
    freely rewritten (§5.1).
*   **Accepted** — the operator has approved the design and the pull
    request has merged. The slices may be cut and dispatched. The text
    is now stable: it changes only by the rules of §5.2. The word, its
    date and the index row (§6) are written by the final push to the
    RFC's own pull request, after the ruling and before the merge — so
    no RFC ever sits on `main` claiming to be proposed when it is not.
*   **Implemented** — every slice has merged. The header names where the
    content now lives, and the body is history (§7). The word is written
    by the slice that lands the last piece (§3.4); the operator's
    acceptance of the built result is the standing receipt on the gate
    issue (§4.2), not a second header state.
*   **Superseded by** `rfc-NNN`, with the date — an implemented RFC
    whose design a later one replaces. The body is not edited; the
    header is (§5.4).

There is no rejected state, because a rejected RFC never lands: its pull
request is closed unmerged, and the argument stays there. The number is
spent either way (§6).

*Approval* is the operator's act; *Accepted* is the state the document
carries after it. One word each, and neither is used for the other.

**A promoted design enters at Proposed** (§1.2), whatever its issue had
already been ruled. Where the work it describes is already built, the
document is written as history and reaches **Implemented** at its own
merge, because there is nothing left to cut.

Going backwards is allowed and is not an event: an accepted RFC whose
core design is overturned before it is built returns to **Proposed** and
runs the gate again (§5.2).

### 3.2 The three artifacts

An RFC is three things in two repositories, and confusing them is the
most common way to lose track of one:

| Artifact | Where | What it carries |
| --- | --- | --- |
| The document | a pull request against this repository | the design, and after merge the accepted text |
| The decision issue | the ops repository | the operator's ruling, and the `decision/*` label that is the queue |
| The slices | ops issues, one per dispatch | the work, cut from the document after acceptance |

**A promoted design already has two of the three.** The decision issue
exists, and some slices may too (§1.2); the promotion adds the document,
and nothing else moves.

The document is public, and the issues are not, so **the document argues
from substance, never from an issue number**: a decision it depends on
is stated in a sentence a stranger can check, not cited as a ticket they
cannot open. The index of §6 obeys the same rule — a proposal's status
names the pull request it is waiting in, which is public, rather than
the issue it is waiting on, which is not.

### 3.3 The labels are a hand-off, and who moves them

The three states of [dispatch.md](dispatch.md) §4.1 ride the **decision
issue**, not the pull request: this repository's pull requests carry no
`decision/*` label, because the labels and the operator's queue live in
the ops repository.

**A decision issue is usually not filed as one.** An RFC's issue is
filed as a task, is claimed and dispatched like any other, and becomes a
decision issue at the handoff — when the document is ready and the ball
moves to the operator. The issue stays claimed while the operator reads:
`in-flight` and `decision/*` are orthogonal
([dispatch.md](dispatch.md) §2, §4).

**The label is set by whoever is putting the ball down, for the party
picking it up, in the same action as the comment.** It is a hand-off
flag, not a verdict pronounced on someone else's work.

*   An agent or dispatcher that posts a design, a revision, or an answer
    sets `decision/pending`. The operator's queue is exactly that
    filter, so an unlabeled proposal is not waiting — it is invisible.
*   The operator, replying with anything short of approval, leaves
    `decision/responded`. The agent revises or investigates, and posting
    the result sets `decision/pending` again. The loop runs as many
    times as it needs to.
*   `decision/lgtm` is the operator's, and means the latest thing in the
    issue is approved and clear to build.

**Approval, then slices, then merge.** An RFC's pull request carries
`Closes` for its decision issue, so the merge moves the ledger by itself
([dispatch.md](dispatch.md) §4).

**The exception is building ahead**: where the text already fixes a
slice's content, that slice may be opened as a **draft** pull request
before approval, provided none of them merges until the design issue is
closed. It buys elapsed time and costs a rebase if the design moves, and
the risk belongs to the dispatcher who chose it — an approval that
changes a slice's premise invalidates the draft, and no reviewer owes it
a second read.

### 3.4 Slices

A slice is **one dispatch**: one agent, one `jj` workspace, one pull
request, one set of owned paths ([dispatch.md](dispatch.md) §1). Work
that does not fit is split until it does. The slice list is ordered so
that nothing is moved twice.

Every entry says what it contains **and what done means for it, in one
sentence** — the same sentence [dispatch.md](dispatch.md) §1.1 already
demands of every brief, written once in the document instead of invented
per dispatch. Where the gate alone settles it the sentence says so;
where it does not, it is a condition somebody can check. **The slice
list is therefore the deliverables list**: every entry names a thing
that will exist and the sentence that says when it does, and an RFC with
no such list cannot be told apart from an unfinished one.

A slice issue names its RFC and the section it is cut from **in its
title**, and so does the pull request that closes it. The form is the
file's own: `rfc-004 slice 3: …`, and a section reference is
`rfc-004 §5.4` — one spelling, lowercase, matching the file name. The
title is the only link between an issue and the design it comes from, so
it is not optional.

**Every slice is an ops issue**, including the ones that produce no pull
request — a label description, a setting in the forge, a change in
another repository. A slice with a pull request is closed by the merge;
one without is closed by the dispatcher with a comment saying what was
done. Nothing is a slice that has no issue, because the slice list would
then stop being the roll.

**The slice list is the roll.** An RFC is done when its last slice
merges; nothing else tracks completion, and no per-RFC tracking issue is
opened. The slice that lands the last piece also flips the status header
to **Implemented** and fills in where the content now lives — the same
rule AGENTS.md applies to every other document, that the documentation a
change makes true ships inside it.

## 4. The review gate

### 4.1 Before the operator sees it

**An RFC is independently reviewed before it is handed to the
operator**, by an agent that did not write it, against §2's section list
and the style rules. The handoff comment says that review ran.

[dispatch.md](dispatch.md) §3 already requires an independent review of
every pull request before merge; this says which review comes first and
what it is for. Format is stopped before the document reaches the
operator, so that design is all the reading costs.

### 4.2 The operator's review

The gate is per milestone, not per RFC ([dispatch.md](dispatch.md)
§3.1): one issue covering a milestone's worth of change, read as a
doc-versus-implementation audit plus the operator's own design review.
Its shape:

*   **The issue body is the review**, numbered per area, with `lgtm`
    written against the areas that are clean, so the reader can tell
    "reviewed and fine" from "not reached".
*   **Every point gets a disposition, in the numbering the operator
    used**, in one reply rather than scattered across the thread. There
    are exactly three dispositions, and every point takes one —
    1.  **answered with no change**, with the reason the point does not
        hold or the fact it missed;
    2.  **dispatched**, as a slice issue, named;
    3.  **escalated**, as a design proposal posted in the reply for the
        operator to rule on in the thread. Keeping it there is what
        keeps the fix list checkable in one place; a separate
        `decision/pending` issue is opened only when the ruling turns
        out to be someone else's to make.

    Disagreeing is a disposition, not an omission: a point argued
    against with reasons is a proper answer, and one silently
    unaddressed is the failure this rule exists to prevent.
*   **Approval attaches forward.** The operator may approve against a
    fix list, and the approval takes effect when the list is exhausted,
    with no second review.
*   **The gate issue stays open**, carrying `decision/lgtm` as a
    standing receipt for the milestone — unlike an RFC's own decision
    issue, which the merge closes (§3.3). Nothing closes the gate; it is
    the record that the milestone was accepted and on what terms.

Findings the operator raises that turn out to be someone else's decision
become their own `decision/pending` issues rather than growing this one
([dispatch.md](dispatch.md) §3).

**An RFC that opens no milestone has no gate of its own**, and does not
need one. It is **Implemented** when its last slice merges (§3.1), and
the areas it touched are audited at the next milestone's checkpoint
along with everything else that landed in the meantime.

## 5. Amendment

Four stages, and one test running through all of them: **has the other
party — the operator, for a design or a disposition — already acted on
the text?** Text somebody has answered is never edited; it is revised by
a new comment scoped to what changes. Text nobody has acted on yet is
fixed where it stands — and so, whenever it is found, is a statement
that was false when it was made.

### 5.1 While it is proposed

The document is a pull request; it is rewritten in place and the diff is
the record. Review comments are answered on the pull request. Nothing in
the text marks a revision, because nothing has been accepted yet.

*   **Answered text is revised by a new comment.** Once the operator has
    replied, the text replied to is frozen: the revision is its own
    comment, in the operator's numbering, scoped to what it changes —
    *everything not named here stands as written*. The unit is the
    comment, not the point: a reply to any part of it freezes all of it,
    and a revision then carries every change, including the ones the
    reply passed over.
*   **Unanswered text is fixed where it stands**, with an italic
    *(edited: …)* note saying what changed
    ([dispatch.md](dispatch.md) §4). A disposition rewritten while the
    review is still open, a pull request body corrected before any
    ruling: nobody has acted on either, so there is nothing to preserve
    and every reason not to make the next reader assemble the truth from
    two places. A pull request's description is not text anyone acts on
    either; it describes the diff ([dispatch.md](dispatch.md) §1.1) and
    follows it, edited in place with the note before or after any
    ruling, merged or not.
*   **A false statement is corrected in place whenever it is found**,
    answered or not, by the same note. This is a second clause and not
    the test: a text can be overtaken without ever having been wrong,
    and that alone does not license an edit.

### 5.2 After acceptance, before it is built

*   **A detail moves** — a boundary case found in construction, a name,
    a step's order. The RFC text is edited in place, in the pull request
    that changes the implementation with it, and the header gains a
    dated `Updated:` line saying what changed. There is no
    *(edited: …)* note in the file, because a file in git already has
    its history; the note is for issue bodies and comments, which do not
    ([dispatch.md](dispatch.md) §4).
*   **The core design is overturned** — the thing the RFC exists to
    decide is no longer the answer. The document returns to
    **Proposed**, is rewritten, and runs the gate again: the decision
    issue reopens at `decision/pending`. No supplementary document is
    written, and no amendment is layered on top, because nothing is built
    yet that a second document would have to stay compatible with, and
    one document per design is what a later reader can afford to read.

The discriminator is not the size of the diff. It is whether a slice
already merged has to be re-argued.

### 5.3 After it is built

**What is built is canon, and the RFC is history.** Where the text and a
design document disagree, the design document is right. That covers
vocabulary too: an accepted RFC's body keeps the words the decision was
made in, so a sweep that renames a mechanism in the framework documents
edits those documents and leaves `docs/rfc/` alone
([style/README.md](../style/README.md) calls it history rather than
reference, and rewriting it would falsify the record). **That holds
from acceptance, not from first build**: an accepted RFC that nothing
has been built from yet is still edited only for the reasons §5.2
gives, and a vocabulary sweep is none of them. The name §5.2 admits is
one that the implementation moved, edited in the pull request that
moves it.

A decision that moved during construction is recorded **in the status
header**, not by editing the body: the body stays the text that was
accepted, and the header says what was built instead and why the
accepted answer could not stand. The design document carries the truth
in full; the header carries only enough that a reader of the RFC knows
not to trust that paragraph.

The recording ships in the slice that deviates, not later.

### 5.4 Superseding

A design defect found after the RFC is implemented is a **new RFC**,
whose header names what it supersedes. The old document's body is never
edited; its status becomes `Superseded by rfc-NNN`, with the date, and
the index carries the same. An implemented RFC is a closed record of
what was decided and on what evidence, and the reason to keep it intact
is that the next person to propose the same thing needs to know why it
was decided that way the first time.

## 6. Numbering, naming, and the index

All of it mechanical:

*   Files are `docs/rfc/rfc-NNN-<slug>.md`, with `NNN` zero-padded to
    three digits and the slug naming the subject in lowercase words
    joined by hyphens — not the milestone, which is not what a reader
    searches for.
*   The title line is `# RFC NNN: <Subject>`.
*   **The number is claimed by writing it into the ops issue's title**,
    in the same action as the `in-flight` label, and it is the lowest
    number not already spent. The label alone carries no number, so the
    title is what creates the artifact a second dispatcher reads before
    claiming ([dispatch.md](dispatch.md) §2).
*   **A number is spent** once it appears in an ops issue title, a
    branch name, a pull request or a file, and **is never reused** —
    including by a proposal that was rejected and never landed.
*   **A file is never renamed after it merges.** The slug is wrong
    forever rather than the links being broken forever.

**The index is [README.md](../rfc/README.md) in the RFC directory**:
every RFC, its subject, its status, and where its content lives now. It
lists proposals too, unlinked until their file lands on `main`, and the
row is written or updated by the pull request that changes the fact —
the RFC's own pull request when it is proposed and again when it is
accepted, and the pull request of the last slice when it is implemented.
An index maintained apart from the thing it indexes is a second truth,
and it drifts.

## 7. The style rules, and the design documents

**An RFC obeys the style rules; it does not quietly outvote them.** That
is what the header's authority line commits it to. Three consequences:

*   **A rule the style documents do not state carries the marker** where
    it is stated, and the landing table (§2.2's item 6) names the style
    document it belongs in.
*   **A proposed rule becomes binding when the slice that writes it into
    the style document merges** — not when the RFC is accepted. A
    reviewer holds a change to `docs/style/`
    ([style/README.md](../style/README.md)), and reads that, not the RFC
    archive. A rule that lives only in an accepted proposal is enforced
    by whoever remembers it.
*   **An RFC that needs to contradict a standing style rule says so and
    changes the rule**, in the same landing table. A silent
    contradiction is a defect the review of §4.1 catches.

The same split governs everything else the document produces.
`docs/style/` owns how things are written, `docs/framework/` owns the
mechanisms, and `docs/cluster/`, `docs/physical/` and
`docs/declarative/` own the design of what is built; `docs/rfc/` holds
the accepted proposals those documents were changed by, as history
rather than as reference ([style/README.md](../style/README.md)). A
reader who wants to know how something works today reads the design
documents. The RFC keeps what they cannot: the alternatives weighed, the
measurements, and the reason the losing option lost.
