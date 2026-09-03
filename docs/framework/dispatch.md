# Dispatch, Review, and the Ledger

How work moves through this repository: what a dispatcher hands an
agent, how several dispatchers stay out of each other's way, what has
to happen before a pull request merges, and where the state of the
work is kept. AGENTS.md is the entry point every agent reads; this
document is the protocol behind it.

## 1. One issue, one agent, one pull request

`main` is protected: everything lands through a pull request whose
`checks` and `changes` are green on an up-to-date branch, the account
owner included ([github.md](github.md) §3). Work that can run in
parallel therefore runs as **one agent per issue, each in its own git
worktree, each opening its own pull request**. The dispatcher
commissions the review (§3) and merges; it does not also implement the
work it dispatched.

**Path ownership is what makes it parallel.** Two agents may not be
able to edit the same file, so each brief names the paths its work
owns, and an agent that finds it needs a path outside that list stops
and reports rather than widening its own scope. A few files every
brief would otherwise want are **serialized** — AGENTS.md names the
set, and the constraint is repository-wide (§2).

An agent that finishes early does not pick up more work; it reports.
Scope creep is the failure mode this structure exists to prevent.

### 1.1 What a brief carries

A brief carries, and an agent is finished only when it has all of
them:

1.  **The issue**, by number in the ops repository, and what "done"
    means in one sentence.
2.  **Owned paths**, exhaustively. Everything else is out of scope.
3.  **The gate**: AGENTS.md's checklist, passed in full. New behavior
    has a test that fails without it. A change to provider-facing code
    also ships with a live-drill transcript ([testing.md](testing.md)
    §5) or an explicit "unproven live" note in the pull request saying
    what the first live run must confirm.
4.  **Documentation is part of the change, not a follow-up.** Docs
    describe what is, not what was done: no "verified on", no
    narrative of attempts. The same holds for commit messages.
5.  **A pull request whose description is written for a stranger** —
    this repository is public. What changed and why, what a reviewer
    should check, what was deliberately left out. No internal
    shorthand, no credentials, no host names that are not already in
    the repository.

### 1.2 A worktree dies with the dispatch

Work happens in a git worktree of its own, and none outlives the
dispatch that created it:

-   An agent that finishes **without** a pull request removes its own
    worktree and branch before it reports — `git worktree remove
    <path>` and `git branch -D <branch>`.
-   An agent that **opened** a pull request leaves the worktree
    standing and names its path in the report. Removing it is the
    merging dispatcher's closing step, beside the label and the card,
    because each dispatcher merges only its own pull requests (§2).
-   A reviewer's throwaway checkout is deleted when the review is
    written.

Neither `/tmp` nor the home directory keeps a dead tree. A worktree
whose branch is merged or abandoned is a trap for the next agent,
which can read a stale copy of a file it is about to edit, and
`git worktree list` is the census that shows them.

Two conditions say a branch is safe to delete. **Merged content is
established by `git cherry origin/main <branch>`, not by ancestry**:
this repository rebase-merges, so a merged branch's commits keep their
pre-merge hashes and are ancestors of nothing — `git cherry` compares
patches instead, and no line starting with `+` means every commit has
an equivalent on `main`. **And the working tree is clean**:
`git status --porcelain` in the worktree prints nothing, so no
uncommitted work goes with it. `git worktree prune` clears the
administrative entries of directories that are already gone.

## 2. Concurrent dispatchers

Any number of dispatcher sessions may run at a time (typically: one
driving a milestone's serial pipeline, others draining the `Parallel`
milestone). The rules that keep them out of each other's way:

1.  **The `in-flight` label is the claim.** A dispatcher labels an
    issue before dispatching it and never dispatches, edits, or merges
    work for an issue another dispatcher has labeled. First label
    wins; everything else follows from ownership of the claim. Because
    the label is the claim and nothing else is, a `decision/*` label
    beside it does not release it (§4.1) — an issue parked on a ruling
    with no claim is one a second dispatcher would pick up.
2.  **Claims must not overlap in paths.** Before claiming, a
    dispatcher lists every other in-flight issue and open pull
    request; if the owned paths would intersect, it does not claim.
    While a structural campaign runs, the parallel dispatcher prefers
    work that cannot collide: other repositories, `scripts/`-only,
    tests-only, and docs the campaign's briefs do not name.
3.  **Serialized files serialize across sessions**: the set AGENTS.md
    names is constrained repository-wide rather than per session — at
    most one open pull request may touch each of them, whoever opened
    it.
4.  **Each dispatcher merges only its own pull requests** and absorbs
    its own rebases when another's merge advances `main`; no one ever
    force-touches a branch that is not theirs.
5.  **The primary checkout stays on `main`.** Builders work in their
    own worktrees already; a dispatcher's direct edits go through a
    worktree of its own, never by switching branches in the shared
    checkout.
6.  **Cards follow claims**: a dispatcher moves only the board cards
    of issues it has claimed.
7.  **Sessions talk.** Local sessions can message each other; a
    planned touch on anything ambiguous is announced to the affected
    dispatcher before it happens, not discovered in a conflict.

## 3. Review

A pull request is merged only after an **independent review** — an
agent that did not write the change, briefed with the diff and nothing
of the builder's reasoning, so it reads the code the way a stranger
will. The dispatcher runs it when the builder reports, and merges only
when it comes back clean or its findings are fixed.

Two angles, one reviewer each (they may run in parallel):

1.  **Correctness**: does the change do what the issue says, does
    every new behavior have a test that fails without it, does the
    diff break an invariant a test elsewhere pins, is anything
    provider-facing left unproven without saying so.
2.  **Architecture & style**, against [`docs/style/`](../style/):
    config read at the right layer, resources on the right component,
    providers inherited not re-plumbed, names and comments that
    survive the style rules' tests, censuses where they belong.
    [style/pulumi.md](../style/pulumi.md) keeps that reviewer's
    standing questions.

Findings go to the builder as one fix cycle (mid-flight message or a
follow-up brief); a finding the operator must rule on becomes a
`decision/pending` issue (§4.1). A clean review is stated in one line
on the pull request thread before merge. Small diffs get small
reviews — a docs-only change may take a single combined pass — but no
pull request merges reviewed by nobody but its author.

### 3.1 Cadence

Reviews are phased, not saved up. Every pull request gets the
two-angle review above, and every milestone is bracketed by the
operator: it **opens with a design RFC** — the milestone's design
submitted for approval before any implementation is dispatched — and
**closes with its review-checkpoint issue** — a read-only
doc-vs-implementation audit of the milestone's areas plus the
operator's design-level review and acceptance. Each operator pass
covers one milestone's worth of change, so problems surface while they
are cheap. Any other major structural change runs the same RFC-first
sequence. **The process itself is [rfc.md](rfc.md)** — when an RFC is
required and when an ops issue is enough, what one contains, the states
it moves through and the labels that carry them, the operator's gate,
amendment after acceptance, and numbering — and this document says
nothing about it that rfc.md does not settle.

## 4. How progress is reported

The ops repository's issues are the work ledger, and GitHub's own
machinery keeps it true — nothing is reported twice by hand that a
merge can report once:

-   **Every task is an ops issue** carrying an `area/*` label, a
    `kind/*` label, and a **milestone** (the roadmap's phases M0–M4
    plus `Parallel`; the index is the roadmap issue). Issues that gate
    the next milestone carry `blocker`; issues needing an operator
    ruling carry a `decision/*` label (§4.1).
-   **Dispatch is visible**: `in-flight` marks a dispatched issue — it
    is the dispatcher's claim as well (§2) — and comes off when the
    pull request merges or the dispatch is abandoned. **`in-flight` and
    `decision/*` answer different questions** — whether somebody is
    dispatched, and who holds the ball — so an issue may carry both,
    and an issue waiting on the operator is still claimed: neither the
    merge nor the abandonment that takes `in-flight` off has happened
    while the operator reads.
-   **The pull request closes the issue**: its description carries
    `Closes Aetf/kluster-ops#N` (cross-repository closing works and is
    the one mechanism that cannot forget), so the merge itself moves
    the ledger. One pull request may close several issues; an issue
    only partly addressed is *referenced* without the keyword and kept
    open with a comment saying what remains.
-   **Findings become issues, not comments in passing.** An agent's
    unpredicted discovery — a mismatch, a dead mechanism, a stale
    document — is filed as its own issue, labeled and put on a
    milestone (by the dispatcher where the agent lacks standing). The
    discovery rate of implementation work is the ledger's main source
    of truth about what is left.
-   **Corrections edit in place.** A wrong statement in an issue body
    or comment is fixed where it stands, with an *(edited: …)* note —
    never a trailing correction the reader must merge themselves.

### 4.1 Decisions

An issue that needs an operator ruling carries a `decision/*` label,
and the label is a three-state machine: `decision/pending` awaits the
operator's review; `decision/responded` means the operator replied and
the agent investigates or revises the proposal per the reply (then sets
`decision/pending` again); `decision/lgtm` means the latest decision in
the issue is approved and clear to build. The dispatcher sweeps
`decision/responded` and `decision/lgtm` whenever idle — they are the
queue of what can move.

**When the label goes on depends on why the issue exists.** An issue
whose whole purpose is a ruling carries it from the moment it is filed.
An issue filed as a task gains it at the hand-off, when the work reaches
the point where the ball moves to the operator — and an RFC's issue is
that case: filed as a task, claimed and dispatched like any other, and a
decision issue from the moment its document is ready
([rfc.md](rfc.md) §3.3). Either way the label rides the issue, never a
pull request in this repository.

### 4.2 The board

The project board tracks the ops issues; the labels and milestones
above are what make its views mean something. Where a card sits
follows the work and the labels, and only the dispatcher that claimed
the issue moves it (§2):

-   The builder opening the pull request moves the card to *In
    review*; the merge moves it to *Done* through the built-in
    workflow. A card in *In review* with no open pull request is a
    dispatch that died and should be re-driven or returned to *Ready*.
-   `decision/pending` puts the card in *In review*.
    `decision/responded` and `decision/lgtm` return it to *Backlog*,
    *Ready* or *In progress* by whether the work is merely known,
    planned next, or being acted on.
