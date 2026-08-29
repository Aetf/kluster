# Agent notes

* ALWAYS use `mise x uv -- uv` to manage python environment of the project
* ALWAYS use a timer when running tests, to avoid waiting forever when test hangs:
  `timeout 60 mise x uv -- uv run pytest`
* Read `docs/framework/pulumi.md` before writing components; `docs/framework/rfc-001-native-async-inputs.md`
  has the internals. Key rules:
  - Sub-resources are created synchronously in `Component.__init__`; async input
    prep goes through `async_output`, and outputs are awaited only via `resolve`
    inside those coroutines (`resolve` hard-errors anywhere else, including the
    `pulumi.run` entrypoint and tests — in tests, await `.future()` instead).
  - `__main__.py` must stay a real file (not a console-script symlink): the
    script's `sys.exit` would kill the `pulumi.run` async entrypoint.
* Python code standard, enforced on everything under `src/`, `tests/`, and
  `deploy/`: **fully type-annotated, and `basedpyright` strict passes clean**
  (`mise x uv -- uv run basedpyright`). Config lives in `pyproject.toml`;
  the only relaxations are `reportAny`/`reportExplicitAny`/`reportUnusedCallResult`,
  which fight a provider-SDK codebase more than they help. Generated CRD
  bindings (`packages/crds`) are excluded — they are not ours to annotate.
  The check runs in CI alongside ruff.
* **The source tree is layered, and the layering is a checked contract.**
  `kluster.stacks` → `kluster.components` → `kluster.providers` →
  `kluster.lib` → `kluster.conventions` → `putils`: a layer imports what is
  below it and nothing above it, and four further edges are forbidden
  outright (a script reaches no declaration; a custom provider knows no
  `conventions`; `putils` knows no estate; only `kluster.main` imports a
  stack program). `import-linter` enforces it —
  `mise x uv -- uv run lint-imports`, in CI beside ruff and
  `basedpyright`.
  The contract is in `pyproject.toml`; what each layer is for is
  `docs/framework/rfc-002-src-layout-and-the-gateway.md` §2.
* **Scripts are Python**, not shell — a shell script needs a reason (a
  handful of lines with no logic, or a context with no interpreter). They
  live under `src/kluster/scripts/` and are exposed as console scripts in
  `pyproject.toml` (`update_crds`, `credentials`, `state-backend`), the
  same way for every script; `just` recipes or symlinks are for
  convenience on top, never the home of the logic.
* Implementation-period issues live in the `kluster-ops` repo, not in this
  one and not in a checked-in list. What is unimplemented *here* announces
  itself: an unwritten stack raises from its entrypoint, and a register row
  with no implementation is a subcommand that refuses by name. Build order is
  `docs/cluster/migration.md` §1.
* **Prose is checked like code.** Every markdown file passes `ltex-cli-plus`
  against `.vscode/ltex.dictionary.en-US.txt` and
  `.vscode/ltex.disabledRules.en-US.txt`:
  - Both files are one entry per line with **no comment syntax**, and the
    dictionary is **case-sensitive** — `homelab` and `Homelab` are two
    entries, so do not deduplicate them case-insensitively.
  - Disable a rule only when it is systematically wrong for this repo (a
    firewall `ACCEPT`, a `.phd` domain, `key id`, the dot in `A1.Flex`,
    alice/bob as instance names). A one-off gets the prose fixed instead.
  - Run it **one file at a time**: given many files at once it hangs rather
    than finishing. It also mis-columns inside very long table rows and
    reports a fragment of a word as a misspelling — those are artifacts, not
    dictionary entries.

## How work is dispatched

`main` is protected: everything lands through a pull request whose
`checks` and `changes` are green on an up-to-date branch, the account
owner included (`docs/framework/github.md` §3). Work that can run in
parallel therefore runs as **one agent per issue, each in its own git
worktree, each opening its own pull request**, with a reviewer merging.
The dispatcher reviews and merges; it does not also implement the
dispatched work.

**Path ownership is what makes it parallel.** Two agents may not be able
to edit the same file, so each brief names the paths its work owns, and
an agent that finds it needs a path outside that list stops and reports
rather than widening its own scope. Shared files (`AGENTS.md`,
`docs/framework/ci.md`, `pyproject.toml`) are serialized: at most one
open pull request may touch each.

A brief carries, and an agent is finished only when it has all of them:

1. **The issue**, by number in the ops repository, and what "done"
   means in one sentence.
2. **Owned paths**, exhaustively. Everything else is out of scope.
3. **The gate**: `ruff check`, `ruff format --check`, `basedpyright`
   (strict, clean), `lint-imports` and `pytest` all pass; new behavior
   has a test that fails without it; `ltex-cli-plus` passes on every
   markdown file touched, one file at a time. A change to
   provider-facing code also ships with a live-drill transcript
   (`docs/framework/testing.md` §5)
   or an explicit "unproven live" note in the pull request saying what
   the first live run must confirm.
4. **Documentation is part of the change, not a follow-up.** Docs
   describe what is, not what was done: no "verified on", no narrative
   of attempts. The same holds for commit messages.
5. **A pull request whose description is written for a stranger** —
   this repository is public. What changed and why, what a reviewer
   should check, what was deliberately left out. No internal
   shorthand, no credentials, no host names that are not already in the
   repository.

An agent that finishes early does not pick up more work; it reports.
Scope creep is the failure mode this structure exists to prevent.

### Two dispatchers

More than one dispatcher session may run at a time (typically: one
driving a milestone's serial pipeline, one draining the `Parallel`
milestone). The rules that keep them out of each other's way:

1.  **The `in-flight` label is the claim.** A dispatcher labels an
    issue before dispatching it and never dispatches, edits, or merges
    work for an issue the other has labeled. First label wins;
    everything else follows from ownership of the claim.
2.  **Claims must not overlap in paths.** Before claiming, a
    dispatcher lists the other's in-flight issues and open pull
    requests; if the owned paths would intersect, it does not claim.
    While a structural campaign runs, the parallel dispatcher prefers
    work that cannot collide: other repositories, `scripts/`-only,
    tests-only, and docs the campaign's briefs do not name.
3.  **Serialized files serialize across sessions**: at most one open
    pull request repository-wide may touch each of them, whoever
    opened it.
4.  **Each dispatcher merges only its own pull requests** and absorbs
    its own rebases when the other's merge advances `main`; neither
    ever force-touches the other's branches.
5.  **The primary checkout stays on `main`.** Builders work in their
    own worktrees already; a dispatcher's direct edits go through a
    worktree of its own, never by switching branches in the shared
    checkout.
6.  **Cards follow claims**: a dispatcher moves only the board cards
    of issues it has claimed.
7.  **Sessions talk.** Local sessions can message each other; a
    planned touch on anything ambiguous is announced to the other
    dispatcher before it happens, not discovered in a conflict.

### Review stage

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
2.  **Architecture & style**, against `docs/style/`: config
    read at the right layer, resources on the right component,
    providers inherited not re-plumbed, names and comments that survive
    the style rules' tests, censuses where they belong.

Findings go to the builder as one fix cycle (mid-flight message or a
follow-up brief); a finding the operator must rule on becomes a
`decision/pending` issue. A clean review is stated in one line on the pull
request thread before merge. Small diffs get small reviews — a
docs-only change may take a single combined pass — but no pull request
merges reviewed by nobody but its author.

**On the board**: when the builder opens the pull request, its issue
card moves to *In review*; the merge moves it to *Done* through the
built-in workflow. A card in *In review* with no open pull request is a
dispatch that died and should be re-driven or returned to *Ready*.

**Cadence**: reviews are phased, not saved up. Every pull request gets
the two-angle review above, and every milestone is bracketed by the
operator: it **opens with a design RFC** — the milestone's design
submitted for approval before any implementation is dispatched — and
**closes with its review-checkpoint issue** — a read-only
doc-vs-implementation audit of the milestone's areas plus the
operator's design-level review and acceptance. Each operator pass
covers one milestone's worth of change, so problems surface while
they are cheap. Any other major structural change runs the same
RFC-first sequence (rfc-002 is the shape), and an RFC names the
design documents its content must land in once built, if any. An
issue that needs an operator ruling carries a `decision/*` label from
the moment it is filed, and the label is a three-state machine:
`decision/pending` awaits the operator's review; `decision/responded`
means the operator replied and the agent investigates or revises the
proposal per the reply (then sets `decision/pending` again);
`decision/lgtm` means the latest decision in the issue is approved
and clear to build. The dispatcher sweeps `decision/responded` and
`decision/lgtm` whenever idle — they are the queue of what can move.

### How progress is reported

The ops repository's issues are the work ledger, and GitHub's own
machinery keeps it true — nothing is reported twice by hand that a
merge can report once:

-   **Every task is an ops issue** carrying an `area/*` label, a
    `kind/*` label, and a **milestone** (the roadmap's phases M0–M4
    plus `Parallel`; the index is the roadmap issue). Issues needing
    an operator ruling carry `decision/pending`; issues that gate the next
    milestone carry `blocker`. The project board tracks these issues;
    labels and milestones are what make its views mean something.
-   **Dispatch is visible**: the dispatcher adds `in-flight` when a
    builder starts and the brief names the issue; the label comes off
    when the pull request merges or the dispatch is abandoned.
-   **The pull request closes the issue**: its description carries
    `Closes Aetf/kluster-ops#N` (cross-repository closing works and is
    the one mechanism that cannot forget), so the merge itself moves
    the ledger. One pull request may close several issues; an issue
    only partly addressed is *referenced* without the keyword and kept
    open with a comment saying what remains.
-   **Findings become issues, not comments in passing.** An agent's
    unpredicted discovery — a mismatch, a dead mechanism, a stale
    document — is filed as its own issue, labeled and put on a milestone (by the
    dispatcher where the agent lacks standing). The discovery rate of
    implementation work is the ledger's main source of truth about
    what is left.
-   **Corrections edit in place.** A wrong statement in an issue body
    or comment is fixed where it stands, with an *(edited: …)* note —
    never a trailing correction the reader must merge themselves.
