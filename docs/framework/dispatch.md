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
parallel therefore runs as **one agent per issue, each in its own `jj`
workspace, each opening its own pull request**. The dispatcher
commissions the review (§3) and merges; it does not also implement the
work it dispatched (§1.3).

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

### 1.2 A workspace dies with the dispatch

Work happens in a **`jj` workspace** of its own under
`.claude/workspaces/<name>`:

    mkdir -p .claude/workspaces
    jj workspace add -r main .claude/workspaces/<name>
    cd .claude/workspaces/<name> && mise trust

`add` takes the workspace's name from the last element of the path and
starts it on an empty change on top of `main`. It errors on a missing
parent rather than creating one, and `.claude/` is ignored rather than
tracked, so the `mkdir` is not optional in a fresh clone.

`mise trust` is not optional either, and skipping it is what the first
gate command in a new workspace fails on. mise keys trust by absolute
path and shares it only across git **worktrees** — a `jj` workspace is
not one, so the workspace's `mise.toml` is untrusted at its new path
even though the primary checkout trusts the very same tracked file, and
every `mise x uv -- uv run …` — the form AGENTS.md requires for all
Python tooling — refuses with `Config files … are not trusted` until
`mise trust` has been run once inside the workspace. It grants nothing
the primary checkout does not already have: for the `-r main` form
above, `jj workspace add` puts there the same `mise.toml` the primary
already trusts. A workspace started on some other revision is trusting
that revision's `mise.toml`, which is a judgment about the branch
rather than a formality.

No workspace outlives the dispatch that created it:

-   An agent that finishes **without** a pull request removes its own
    workspace before it reports — `jj workspace forget <name>`, run
    **from the primary workspace**, and then `rm -rf <path>`. Two
    commands, because `forget` deliberately leaves the directory alone;
    from the primary, because `forget` run inside its own workspace
    succeeds with a warning and leaves the agent standing in the
    directory it is about to remove.
-   An agent that **opened** a pull request leaves the workspace
    standing and names its path in the report. Removing it is the
    merging dispatcher's closing step, beside the label and the card,
    because each dispatcher merges only its own pull requests (§2).
-   A reviewer's throwaway workspace is deleted when the review is
    written (§3.2).

Neither `/tmp` nor the home directory keeps a dead workspace. One whose
work is merged or abandoned is a trap for the next agent, which can
read a stale copy of a file it is about to edit. `jj workspace list` is
the census that shows them, a directory that was deleted without being
forgotten included: that one lists with no path, and `jj workspace
forget` clears it.

**A workspace goes stale when another workspace rewrites the commits it
is sitting on**, which a dispatcher's `jj git fetch` does routinely
while a builder is live. Every command in the stale workspace then
fails with `The working copy is stale`, and `jj workspace update-stale`
is the fix. It is a working-copy repair, not a recovery: if the commits
were abandoned, it updates to a fresh empty change and takes the files
off disk with it, so read the paragraph on losing work below before
running it.

**`@` is always the empty change, and keeping it that way is a rule,
not a habit.** After each piece of work: describe it, then `jj new`, so
that the work is at `@-` and `@` is empty again. Only then:

    jj bookmark set <branch> -r @-
    jj git push --bookmark <branch>

`--bookmark` tracks a bookmark the remote has never seen, so a first
push needs no extra flag. **Both halves of this fail silently if the
precondition does not hold**, which is why `jj log` before every push,
confirming the work is the commit at `@-`, is part of the form:

-   Work still sitting in `@` — the ordinary `jj` habit of edit,
    describe, push, with no `jj new` — makes `@-` the tip of `main`.
    The bookmark is then created pointing at `main`, the push reports
    `bookmark: <branch> [add to <sha>]` and exits zero, and the branch
    on the forge carries none of the work. A confident-looking push and
    an empty pull request.
-   A bookmark left where it was on a second round of work, because a
    bookmark does not follow commits made after it was set, makes
    `jj git push` report `Nothing changed`, exit zero, and push nothing.

A *rewrite* is the exception that proves the rule: `jj squash --into @-`
and `jj describe` carry the bookmark to the rewritten commit, so on a
fix round the `set` is a silent no-op and the push reports `move
sideways` rather than an add — neither needs a force flag, because `jj
git push` already checks the remote against what it last fetched. Run
the `set` anyway: it costs nothing, and it is the only thing that
catches the round where the fix landed as a new commit instead.

Those two failure modes replace git's "forgot to commit" as the way work
is lost here, and neither returns a non-zero exit. What catches both is
the head SHA the report names (§4), **read with `jj` after the push**:

    jj log -r <branch> --no-graph -T commit_id

**Do not use `git rev-parse HEAD` for this.** An added workspace has no
git repository of its own and sits inside the colocated primary, so git
walks up and answers about the primary: `git rev-parse HEAD` returns
`main`, and `git status --porcelain` describes the primary's tree, not
the one the agent is working in. Both answer confidently and both are
about the wrong directory.

**Whether a change merged is the forge's answer, not the
repository's.** This repository rebase-merges, so a merged change's
commits keep their pre-merge hashes, are ancestors of nothing, and no
local query distinguishes them from unmerged ones. The proof is that
the pull request is `MERGED` and that the head it merged is the head
the builder's report named — the same equality §2 rule 8 checks before
merging, which is why the report's SHA has to be right:

    gh pr view <n> --json state,headRefOid

**A local query answers a different question: what would be lost.**
Before deleting a workspace or a bookmark, run

    jj log -r 'mutable() & ~::main & ~empty()'

which lists the commits `main` does not contain, the empty working-copy
change every workspace carries excluded. The list covers work an agent
never described, because the working copy is itself a commit and
unsaved work is not a state that exists. **Run it before fetching.** A
fetch empties it regardless of whether the work landed: deleting the
head branch on the forge — which the merge does automatically — leaves
the local commits unreferenced, and the next `jj git fetch` abandons
them, prints `Abandoned N commits that are no longer reachable` naming
each one, and leaves any workspace sitting on them stale with its files
still on disk until `update-stale` takes them. That naming line is the
one to keep: it carries the commit id an abandoned change is revived
by (§2 rule 8). A change that genuinely merged still lists before that
fetch; a change where nothing merged lists as empty after it. The
verdict turns on the branch deletion alone, so it is a statement about
what is still here, never about what landed.

### 1.3 The three roles

Who may do what is protocol rather than habit. Which model a role runs
on is deliberately absent: that lives in the agent definitions, outside
this repository, and changes far more often than this document.

-   **Dispatcher** — the session the operator starts. It commissions
    the work, commissions the review (§3), merges, absorbs its own
    rebases, files issues and keeps the ledger (§4). It does not design
    and it does not implement. It does write, though: issue bodies
    edited in place, briefs, and the commit messages a fold produces
    are all records of decisions already taken. The boundary is between
    designing and recording, not between writing and not writing.
-   **Tech lead** — architect and reviewer. Every design longer than a
    paragraph is a tech-lead dispatch: an RFC ([rfc.md](rfc.md)), a
    design proposal on a `decision/*` issue (§4.1), the slice plan that
    turns an accepted design into issues, and each angle of a pull
    request's review (§3). It never merges, and it writes to the
    repository only where the deliverable is a document.
-   **Builder** — implementation. One issue, one workspace, one pull
    request, an explicit brief, and only the paths that brief names.

A dispatcher that finds itself designing has skipped a dispatch.

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
    its own rebases when another's merge advances `main` — `jj rebase
    -d main`, which rewrites commit hashes but carries every change ID
    through, so the branch stays the same piece of work under a new
    parent. No one ever force-touches a branch that is not theirs.
5.  **The primary workspace holds no work.** Its `@` is an empty change
    on top of `main`, restored with `jj new main` after each fetch.
    That is what "stays on `main`" means where there is no current
    branch to be on: the working copy is a commit, and a bookmark moves
    only when someone moves it (§1.2). Builders work in workspaces of
    their own already, and a dispatcher's direct edits go through a
    workspace of its own too.
6.  **Cards follow claims**: a dispatcher moves only the board cards
    of issues it has claimed.
7.  **Sessions talk.** Local sessions can message each other; a
    planned touch on anything ambiguous is announced to the affected
    dispatcher before it happens, not discovered in a conflict.
8.  **The head of the builder's latest report is the head that
    merges.** Before merging, compare
    `gh pr view <n> --json headRefOid` against the head SHA that report
    names (§4); a fix cycle (§3) moves the branch legitimately and ends
    in a fresh report, so a mismatch means someone else moved it.
    Reconstructing who is timestamp work —
    `gh api repos/<o>/<r>/issues/<n>/timeline` carries the
    `head_ref_force_pushed` and `merged` events — because every agent
    pushes as the same account and `actor.login` distinguishes nobody.
    A commit a merge did not take is not lost with the workspace that
    held it: every workspace shares one repository, so the commit
    survives under its bookmark. One that has no bookmark left survives
    too — an abandoned commit stays addressable by its commit id, which
    `jj op log` still shows, and `jj new <id>` revives it with the files
    back on disk and no other effect. Reach for `jj op restore` only
    when no one else is running: it rolls the whole repository back,
    `main` and every other workspace's commits included.

## 3. Review

A pull request is merged only after an **independent review** — an
agent that did not write the change, briefed with the diff and nothing
of the builder's reasoning, so it reads the code the way a stranger
will. The dispatcher runs it when the builder reports, never while the
builder is still working, and merges only when it comes back clean or
its findings are fixed.

**Both angles are tech-lead dispatches** (§1.3), one reviewer each,
and they may run in parallel. The dispatcher's own contribution to a
review is the decision to merge:

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

### 3.2 A reviewer does not hold the branch

A review is read-only: the reviewer does not push, does not merge,
and moves no branch. §2 rule 4 says no one force-touches a branch that
is not theirs; a reviewer touches none at all.

**A workspace is a working copy, not a repository.** Every workspace
shares one store and one operation log, so a `jj` command that rewrites
commits — `rebase`, `abandon`, `bookmark set` — rewrites them for every
workspace at once, and leaves the workspaces sitting on them stale
(§1.2). Having a workspace of one's own confers no isolation. Two rules
carry the read-only rule into practice:

1.  **The reviewer runs no repo-mutating `jj` command at all**, and
    works in a workspace of its own so that its checkout does not fight
    a builder writing in that same tree. Reading — `jj log`, `jj diff`,
    `jj show` — is the whole of a reviewer's vocabulary.
2.  **A fold is described, not rehearsed.** Ask which commits combine
    and why. A trial rebase is the dispatcher's, and it is not private
    even so: it moves the branch itself, which is why it belongs to the
    dispatcher that owns the claim.

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
-   **A builder's report names the head SHA** of the branch it opened,
    read after the push with
    `jj log -r <branch> --no-graph -T commit_id` and never abbreviated
    or extended by hand. That is what the dispatcher compares the pull
    request's head against before merging (§2 rule 8), and it is why
    `git rev-parse HEAD` is not used inside a workspace, where it
    answers about the primary checkout instead (§1.2).
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
