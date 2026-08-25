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
* **Scripts are Python**, not shell — a shell script needs a reason (a
  handful of lines with no logic, or a context with no interpreter). They
  live under `src/kluster/scripts/` and are exposed as console scripts in
  `pyproject.toml` (`update_crds`, `credentials`), the same way for every
  script; `just` recipes or symlinks are for convenience on top, never the
  home of the logic.
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
   (strict, clean) and `pytest` all pass; new behaviour has a test that
   fails without it; `ltex-cli-plus` passes on every markdown file
   touched, one file at a time.
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
