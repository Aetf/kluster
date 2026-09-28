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
workspace — or its own clone, in a cloud session (§1.4) — each opening
its own pull request**. The dispatcher
commissions the review (§3) and merges; it does not also implement the
work it dispatched (§1.3).

**Path ownership is what makes it parallel.** Two agents may not be
able to edit the same file, so each brief names the paths its work
owns, and an agent that finds it needs a path outside that list stops
and reports rather than widening its own scope; the one exception is
§1.1 item 5. A few files every brief would otherwise want are
**serialized** — AGENTS.md names the set, and the constraint is
repository-wide (§2).

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
    narrative of attempts. The same holds for commit messages. Where
    this meets item 2 — a change falsifies prose the brief did not
    single out — the **file** is what decides: an agent repairs prose
    its own change falsified in any file its brief already names, and
    stops and reports only when the file itself is outside the brief.
    Either way the pull request says which call it made and on what
    prose.
5.  **A file the gate demands that no owned path can hold.** Where
    item 3 meets item 2 — new behavior needs a test and the brief names
    nothing able to carry one — owned paths permit a **new** file, and
    the pull request discloses it; they never permit editing a path the
    brief did not name. The asymmetry is the reason: a new file cannot
    collide with another agent's edits, so it costs the
    parallel-dispatch property nothing, while an edit to an unnamed
    file can, which is why that half stays absolute.
6.  **A pull request whose description is written for a stranger** —
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
parent rather than creating one, and `.claude/workspaces/` is ignored
rather than tracked, so the `mkdir` is not optional in a fresh clone.

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

**What AGENTS.md repeats from this section is decided by whether the
step's failure routes an agent here.** AGENTS.md duplicates a form from
here — the commands, what their failure prints or fails to print, and
the tell that catches it — only when the failure would not send an agent
to this section on its own: it is silent, or it reports as something
else. A failure that announces itself and names its remedy is left to
this section, and AGENTS.md carries at most the step's name and what it
is for. That is the split as it stands. The push failures below exit
zero and push nothing, and the prose checker reads a materialized
conflict as misspellings rather than as a conflict, so no error routes
an agent here and AGENTS.md carries both forms whole. The trust failure
is the opposite case: `mise ERROR Config files in … are not trusted.`
followed by ``Trust them with `mise trust`.`` — fail-closed, and the
remedy in the text — so AGENTS.md names `mise trust` in its setup
sentence with the one clause saying what it is for, that `mise x uv`
refuses without it, and the rest — what the refusal prints, why a
workspace is untrusted, what trusting grants — lives here alone.

**Scratch belongs under the workspace's own `.claude/`.** A workspace
root is a checkout, so a file written there is a repository path and
the next `jj` command snapshots it into the change. `.gitignore`'s
`.claude/` patterns are anchored at whatever root is being evaluated, so
`<workspace>/.claude/` is ignored inside the workspace exactly as
`.claude/` is in the primary — everything in it but the paths the
repository tracks there, which the workspace checks out like any other
tracked file — and a tool's config or a dump written there never enters
the change at all.

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

**A builder never fetches; the dispatcher fetches and rebases.** A
branch that opens behind `main` is the expected case, because bringing
it up to date is the merging dispatcher's step (§2 rule 4) and that is
where `jj git fetch` and `jj rebase -d main` live. The one rebase that
is not the dispatcher's is the one that conflicts, which rule 4 sends
back: workspaces share a single store, so the builder rebases onto the
`main` the fetch already brought in, still without fetching itself. The
hazard that a fetch abandons commits whose bookmark the forge has
deleted is that dispatcher's precondition, not a general caution
against fetching.

**A workspace goes stale when another workspace rewrites the commits it
is sitting on**, which a dispatcher's `jj git fetch` does routinely
while a builder is live. Every command in the stale workspace then
fails with `The working copy is stale`, and `jj workspace update-stale`
is the fix. It is a working-copy repair, not a recovery: if the commits
were abandoned, it updates to a fresh empty change and takes the files
off disk with it, so read the paragraph on losing work below before
running it.

**At the moment of a push, `@` is empty and `@-` is the work.** That
invariant is the rule, and `jj new` is how it is restored once a piece
of work is done rather than a command to run unconditionally: `jj`
snapshots edits into `@` as they are made, so `@` carries the work until
`jj new` leaves it behind at `@-`. Run against an `@` that is already
empty — after a push, say — `jj new` stacks a second empty change, `@-`
is empty too, and the bookmark below is set on an empty change instead
of on the work. That mode shows in `jj log` as `(empty) (no description
set)` against `@-` as well as against `@`, and it is guarded besides:
`jj bookmark set` warns `Target revision is empty` and exits zero, and
the push then refuses with `Won't push commit … since it has no
description` and exits non-zero. So: describe the work, then `jj new`,
and only then:

    jj bookmark set <branch> -r @-
    jj git push --bookmark <branch>

`--bookmark` tracks a bookmark the remote has never seen, so a first
push needs no extra flag. **The failures below carry no such guard**:
neither command returns non-zero when the precondition is broken, which
is why `jj log` before every push is part of the form. The healthy state
it shows is `(empty) (no description set)` against `@` and the described
work against `@-`. Each failure has its own tell instead:

-   Work still sitting in `@` — the ordinary `jj` habit of edit,
    describe, push, with no `jj new` — leaves `@-` where the round
    before it left it, `main`'s tip on a first round. The bookmark is
    set there, the push exits zero reporting `[add to <sha>]` or
    `Nothing changed`, and the branch on the forge carries none of the
    new work: a confident-looking push and an empty pull request. **Its
    tell is the described work sitting at `@`**, whatever stands at
    `@-`.
-   A bookmark left where it was on a second round of work, because a
    bookmark does not follow commits made after it was set, makes
    `jj git push` report `Nothing changed`, exit zero, and push
    nothing. **Its tell is the bookmark name still sitting on a commit
    below `@-` after the `set`** rather than on `@-` itself.

A *rewrite* is the exception that proves the rule: `jj squash --into @-`
and `jj describe` carry the bookmark to the rewritten commit, so on a
fix round the `set` is a silent no-op and the push reports `move
sideways` rather than an add — neither needs a force flag, because `jj
git push` already checks the remote against what it last fetched. Run
the `set` anyway: it costs nothing, and it is the only thing that
catches the round where the fix landed as a new commit instead.

**A materialized conflict is checked for before the prose gate runs.**
Rewriting a commit that another commit sits on — a fold, or the
`jj squash --into @-` above — rebases the second onto the first and can
leave conflict markers in the working copy, and `ltex-cli-plus` reads
those as prose: change ids come back as misspellings and grammar
findings land on lines that have no grammar problem, none of which
survives resolving the conflict. An agent that runs the gate first
spends the round fixing sentences that are fine. `jj st` names a
conflicted file, and the prose step does not run while one exists.

What replaces git's "forgot to commit" as the way work is lost here is a
push that exits zero carrying none of the work; the two failures listed
before the paragraph above are each of that kind. What catches one is
the head SHA the report names (§4), **read with `jj` after the push**:

    jj log -r <branch> --no-graph -T commit_id

**Not `git rev-parse HEAD`, and the reason generalizes: inside a
workspace, git answers about the primary checkout wherever the answer
depends on a working copy, on `HEAD`, or on a path resolved relative to
the current directory.** An added workspace has no git repository of its
own and sits inside the colocated primary, so git walks up — the `jj`
form, or a tool that consults no git repository, is what to use instead.
Each instance answers confidently about the wrong directory and none of
them complains: `git rev-parse HEAD` returns `main` rather than the
workspace's head; `git status --porcelain` describes the primary's tree;
`git apply -p1` resolves a diff's paths against the primary's root,
drops everything outside the current directory, and exits zero having
applied nothing, where `patch -p1` applies it. `git show <sha>` and
`git diff <a>..<b>` answer correctly **when no path is named**, because
every workspace shares the one object store. With one named, they fail
the same silent way: adding `-- <path>` makes `git diff <a>..<b>` print
nothing and exit zero, and `git show <sha>` print its header and no
diff, because the path is read relative to the workspace directory as
git sees it, and `-- ':(top)<path>'` is what answers from in there.
Depending on none of those does not by itself make a command safe:
`git ls-tree <sha>` and `git grep <pat> <sha>` name no path and still
answer about the current directory, because git applies the prefix
itself, and `--full-tree` and `:(top)` are what restore them. A
reviewer is the most exposed to all of it (§3.2), since an empty answer
reads as an absent one.

**Whether a change merged is the forge's answer, not the repository's.**
This repository rebase-merges, so a merged change's commits keep their
pre-merge hashes, are ancestors of nothing, and no local query
distinguishes them from unmerged ones. The proof is that the pull
request is `MERGED` and that the head it merged is the head the
dispatcher last pushed — the head the builder's report named where
nothing moved it, and otherwise what the dispatcher's own rebase made of
it (§2 rule 8). The ledger carries only the first of those, so where a
rebase moved the branch the proof is `MERGED` plus the timeline rule 8
names. The comparison rule 8 makes before that rebase is why the
report's SHA has to be right:

    gh pr view <n> --json state,headRefOid

**A local query answers a different question: what would be lost.**
Before deleting a workspace or a bookmark, run

    jj log -r 'mutable() & ~::main & ~empty()'

which lists the commits `main` does not contain, the empty working-copy
change every workspace carries excluded. The list covers work an agent
never described, because the working copy is itself a commit and
unsaved work is not a state that exists. **A dispatcher runs it before
every fetch**, which is the precondition the fetch rule above points
at. A fetch empties it regardless of whether the work landed: deleting
the head branch on the forge — which the merge does automatically —
leaves the local commits unreferenced, and the next `jj git fetch`
abandons them, prints `Abandoned N commits that are no longer reachable`
naming each one, and leaves any workspace sitting on them stale with its
files still on disk until `update-stale` takes them. That naming line is
the one to keep: it carries the commit id an abandoned change is revived
by (§2 rule 8). A change that genuinely merged still lists before that
fetch; a change where nothing merged lists as empty after it. The
verdict turns on the branch deletion alone, so it is a statement about
what is still here, never about what landed.

### 1.3 The three roles

Who may do what is protocol rather than habit. Which model a role runs
on is deliberately absent: that lives in the agent definitions under
`.claude/agents/`, and changes far more often than this document.

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

### 1.4 A builder in a cloud session

A cloud session — Claude Code on claude.ai/code — starts from a fresh
clone of this repository on a `claude/…` branch of its own, and carries
nothing from any workstation: what it knows of this repository is
`CLAUDE.md`, and `AGENTS.md` through it, and `.claude/`, read from the
clone. A builder there is the
builder of §1.3, and §1 holds except where this list says otherwise:

-   **The clone is the workspace.** It holds one piece of work at a
    time — the only one, unless the session takes its work from the
    queue below — and dies with the session, so no `jj` workspace is
    added and none is removed. Scratch goes under the clone's
    `.claude/`, as in §1.2.
-   **git is the version control, and `gh` is not on the image.** The
    work is committed with `git add <path>` on each path it touched —
    never `git commit -a`, whose scope is the whole tree rather than the
    work — and pushed with `git push origin HEAD` to the session's
    branch, the only one it may push to. The `jj` push form and its
    failure modes (§1.2) do not arise, and in a plain clone git answers
    about the clone itself, so `git rev-parse HEAD` after the push is the
    head SHA the report names (§4). The pull request is opened and read
    with the session's GitHub tools, which reach this repository and no
    other unless the operator attaches it.
-   **A fix cycle goes back to the session that opened the pull
    request.** A session started on claude.ai gets a branch of its own
    and pushes only that one, so a fix cycle (§3), and the conflicting
    rebase §2 rule 4 sends back, are sent to that session, resumed.
    Reopening a session that has expired provisions a fresh VM with the
    conversation restored and nothing else, so the builder pushes before
    it ends a turn: a commit that never reached the branch is lost with
    the old VM.
-   **It fetches for itself.** A builder never fetches (§1.2) because
    workspaces share one store; a clone shares none. So a round that
    starts from the forge — a fix cycle on a branch the dispatcher
    rebased, or the conflicting rebase §2 rule 4 sends back — starts
    with `git fetch`, and a rewritten branch goes back with
    `git push --force-with-lease`.
-   **Stop and report is a comment on the pull request**, written with
    those GitHub tools: no dispatcher shares the session, so what §1
    sends to one — a path outside the brief, a finding, the finished
    report — goes there, and to the ops issue only when the operator has
    attached the ops repository. Before a pull request exists, the
    report goes on the ops issue the session took from the queue below,
    and is otherwise the session's own last message, which the operator
    reads on claude.ai and relays. A gate that cannot run is such a
    report, not a reason to install tools another way.
-   **It never merges and never changes repository settings**, and it
    needs no live credential. Every operation that needs one stays with
    the checkout that holds `.credentials/` (kluster-ops#387): the
    clone holds none, so provider-facing work ships with the "unproven
    live" note of §1.1 item 3.

**A session the operator reuses takes its work from the `cloud/ready`
queue**, one item at a time, instead of from a brief pasted into it:

-   **The queue is the ops issues labeled `cloud/ready`.** An issue's
    brief is its latest comment that starts with `## Brief`, a brief in
    §1.1's sense. Only a dispatcher writes that comment and applies the
    label, and applying it is how the dispatcher selects the work, so
    the session selects nothing (§1). The label goes on only where §2
    would let that dispatcher claim the issue, and claims it until the
    session takes it (§2 rule 1).
-   **Taking an item** means taking the oldest open `cloud/ready` issue
    not labeled `in-flight` and adding `in-flight`: the claim, placed in
    the name of the dispatcher that queued the issue, which reviews and
    merges the work. The session works it to a pull request and stops.
    It pushes only its own branch, so while that pull request is open
    it starts nothing else, and a fix cycle is the dispatcher's message
    pointing at the review, answered in that same pull request.
-   **The next item starts on the dispatcher's word**, given once it
    has merged or closed the pull request. The session then takes
    `cloud/ready` and `in-flight` off the finished issue, open or
    closed, runs `git fetch --prune`, resets its branch to
    `origin/main`, and takes the next item. That item's first push is
    `git push --force-with-lease origin HEAD`, since it rewrites the
    branch; the prune is what lets the lease pass where the merge
    deleted the branch from the forge. A dispatcher re-queues an issue
    whose pull request it closed only after the session has cleared
    that issue's labels, or the session strips the fresh label too.
-   **One session takes from the queue at a time.** Adding a label is
    not a compare-and-set, so two sessions that read the queue together
    both take its oldest item. A second session is started, or given
    the word, only when no other session can be taking an item.
-   **A fresh session takes over after a few items**, or sooner when a
    fix cycle shows the session losing track, because a session's
    context grows with every item. The dispatcher starts it for the
    next item; nothing in the queue depends on which session takes it.
-   **The session needs `Aetf/kluster-ops` attached**, to read the
    queue and move its labels.

**`deploy/cloud-session/toolchain.sh` installs the session's tools**:
mise, from `npm`, at CI's version; everything `mise.toml` pins; and the
locked Python environment. It prints nothing when it succeeds and one
line naming this section when it fails, skips each step whose work is
already done, and finishes well inside the roughly five minutes a setup
script has for the environment to be cached. It runs from two places:

-   **The setup script of a cloud environment kept for this
    repository**, set on claude.ai. Environments belong to the account,
    and each session runs in the one it is started with, so a line in an
    environment other repositories use runs in their sessions too, and
    so does the Full network access below. The line, naming the path a
    session's clone has, is

        bash /home/user/kluster/deploy/cloud-session/toolchain.sh || true

    It runs after the clone and before Claude Code starts, and the
    environment keeps what it installed for later sessions. A setup
    script that fails keeps the session from starting, which is what the
    `|| true` is for: a failed or skipped setup leaves the install to the
    hook, whose failure line shows in the running session.
-   **The `SessionStart` hook in `.claude/settings.json`**, at every
    start and resume of a session where `CLAUDE_CODE_REMOTE` is `true`,
    which only a cloud session sets. Where the setup script has run, it
    finds everything in place and only checks; in an environment without
    one, it is the whole install. It is also what puts mise on the session's
    `PATH`. A failure shows as the hook's error, and the session starts
    regardless.

**The environment's network access is Full**, set on claude.ai. The
default, Trusted, refuses hosts the script downloads from — mise's
own, and the one uv fetches its Python from — and a refused download is
the failure the script's line names. Full does not open the GitHub API:
the session's GitHub proxy answers `api.github.com` only for
repositories attached to the session, whatever the network access. So no
tool `mise.toml` pins may need the API to install; the `ltex-ls-plus`
pin's comment there says how that one stays off it. Release downloads
from `github.com`, which every pinned tool installs from, pass at Full
as measured in a cloud session, although the Claude Code documentation
says the proxy serves release assets only from repositories attached to
the session. If the proxy comes to enforce that, the install fails for
every pinned tool, not for the prose checker alone.

## 2. Concurrent dispatchers

Any number of dispatcher sessions may run at a time (typically: one
driving a milestone's serial pipeline, others draining the `Parallel`
milestone). The rules that keep them out of each other's way:

1.  **A label is the claim: `in-flight`, or `cloud/ready` on an issue
    queued for a cloud session.** A dispatcher labels an issue before
    dispatching it and never dispatches, edits, or merges work for an
    issue another dispatcher has labeled. First label wins; everything
    else follows from ownership of the claim. A queued issue carries
    `cloud/ready` and not `in-flight`, which the session adds in the
    queuing dispatcher's name when it takes the issue (§1.4), so a
    dispatcher that already holds `in-flight` on an issue it queues
    takes it off. Because those labels are the claim and nothing else is, a
    `decision/*` label beside it does not release it (§4.1) — an issue
    parked on a ruling with no claim is one a second dispatcher would
    pick up.
2.  **Claims must not overlap in paths.** Before claiming, a
    dispatcher lists every other claimed issue and open pull
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
    parent. No one ever force-touches a branch that is not theirs. **A
    rebase that conflicts goes back to the builder** as a fix cycle
    (§3) naming the conflict: resolving it inside the builder's change
    is implementation, which is not the dispatcher's (§1.3).
5.  **The primary workspace's `@` is the operator's, and a fetch
    carries it onto `main` rather than replacing it.** After every
    fetch it is rebased, `jj rebase -b @ -d main`, whatever it holds,
    and it is never replaced with `jj new main`. What it holds is most
    often nothing, and otherwise most often a checkpoint a run of a
    stack whose state is committed left there for the operator to land
    ([pulumi.md](pulumi.md) §3.3): the primary checkout is the one that
    holds `.credentials/`, so it is where such a run starts, and `jj`
    snapshots the file into `@` like any edit. An `@` that holds nothing
    rebases the same way, so there is nothing to check first. The
    rebase is also how "on `main`" is kept where there is no current
    branch to be on: the working copy is a commit, and a bookmark moves
    only when someone moves it (§1.2). When the forge deletes a merged
    branch before the fetch, which the merge does by default, the fetch
    itself abandons the landed change; when the fetch runs first, or
    the branch is kept, the rebase leaves the landed change behind as an
    empty described commit between `main` and `@`, which is abandoned by
    hand before it rides into the next pushed branch. Builders work in
    workspaces of their own already, and a dispatcher's direct edits go
    through a workspace of its own too.
6.  **Cards follow claims**: a dispatcher moves only the board cards
    of issues it has claimed.
7.  **Sessions talk.** Local sessions can message each other; a
    planned touch on anything ambiguous is announced to the affected
    dispatcher before it happens, not discovered in a conflict.
8.  **The head the builder's latest report names is what the forge's
    head is compared against, and the comparison comes before the
    dispatcher rebases.** Compare
    `gh pr view <n> --json headRefOid` against the head SHA that report
    names (§4) while the branch is still exactly what the builder
    pushed; only then bring it up to date (rule 4) and push it —
    `jj git push --bookmark <branch>`, because the forge merges what it
    holds and not what is local. The head that actually merges is what
    `jj log -r <branch> --no-graph -T commit_id` reads back after that
    push. The legitimate movers are a fix cycle (§3), which ends in a
    fresh report, and the dispatcher's own rebase, which is sequenced
    after the comparison for exactly this reason — so a mismatch at the
    comparison means someone else moved it.
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
    native providers inherited rather than re-plumbed, a dynamic
    resource's address and pin declared as its own inputs, names and
    comments that survive the style rules' tests, censuses where they
    belong.
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
doc-vs-implementation audit of the milestone's areas, the per-zone
Certificate Transparency read beside it
([declarative/dns.md](../declarative/dns.md) §1.2, a manual procedure
whose only moment to run is this one), plus the operator's design-level
review and acceptance. Each operator pass covers one milestone's worth
of change, so problems surface while they are cheap. Any other major
structural change runs the same RFC-first sequence. **The process itself
is [rfc.md](rfc.md)** — when an RFC is
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
    while the operator reads. `cloud/ready` marks an issue queued for a
    cloud session, and one not yet taken where `in-flight` is absent
    (§1.4).
-   **The pull request closes the issue**: its description carries
    `Closes Aetf/kluster-ops#N` (cross-repository closing works and is
    the one mechanism that cannot forget), so the merge itself moves
    the ledger. One pull request may close several issues; an issue
    only partly addressed is *referenced* without the keyword and kept
    open with a comment saying what remains.
-   **A builder's report names the head SHA** of the branch it opened,
    read after the push with
    `jj log -r <branch> --no-graph -T commit_id` — in a cloud session's
    clone, with `git rev-parse HEAD` (§1.4) — and never abbreviated
    or extended by hand. That is what the dispatcher compares the pull
    request's head against before merging and before its own rebase (§2
    rule 8), and it is why `git rev-parse HEAD` is not used inside a
    workspace, where it answers about the primary checkout instead
    (§1.2).
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

-   The builder's report names the pull request it opened, and the
    dispatcher that claimed the issue moves the card to *In review*
    when that report reaches it; the merge moves it to *Done* through
    the built-in workflow. A card in *In review* with no open pull
    request is a dispatch that died and should be re-driven or returned
    to *Ready*.
-   `decision/pending` puts the card in *In review*.
    `decision/responded` and `decision/lgtm` return it to *Backlog*,
    *Ready* or *In progress* by whether the work is merely known,
    planned next, or being acted on.
