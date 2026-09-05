# Agent notes

What an agent needs before touching anything here. The protocol around
the work — how it is dispatched, reviewed, and reported — is
[docs/framework/dispatch.md](docs/framework/dispatch.md).

## Environment

* ALWAYS use `mise x uv -- uv` to manage python environment of the project
* ALWAYS use a timer when running tests, to avoid waiting forever when test hangs:
  `timeout 60 mise x uv -- uv run pytest`

## The gate

A change is done when every one of these passes, and CI runs the same
set:

* `mise x uv -- uv run ruff check` and `ruff format --check`
* `mise x uv -- uv run basedpyright` — strict, clean
* `mise x uv -- uv run lint-imports` — the layering contract below
* `timeout 60 mise x uv -- uv run pytest`
* `ltex-cli-plus` on every markdown file touched, one file at a time —
  where the binary is and how it reaches the repository's word lists is
  under "Writing the prose"
* provider-facing code has one more requirement —
  [dispatch.md](docs/framework/dispatch.md) §1.1

New behavior ships with a test that fails without it, and the
documentation the change makes true ships with it rather than after it.

## Writing the code

* Read `docs/framework/pulumi.md` before writing components;
  `docs/rfc/rfc-001-native-async-inputs.md` has the internals. Key rules:
  - Sub-resources are created synchronously in `Component.__init__`; async input
    prep goes through `async_output`, and outputs are awaited only via `resolve`
    inside those coroutines (`resolve` hard-errors anywhere else, including the
    `pulumi.run` entrypoint and tests — in tests, await `.future()` instead).
  - `__main__.py` must stay a real file (not a console-script symlink): the
    script's `sys.exit` would kill the `pulumi.run` async entrypoint.
* Python code standard, enforced on everything under `src/`, `tests/`, and
  `deploy/`: **fully type-annotated, and `basedpyright` strict passes clean**.
  Config lives in `pyproject.toml`; the only relaxations are
  `reportAny`/`reportExplicitAny`/`reportUnusedCallResult`, which fight a
  provider-SDK codebase more than they help. Generated CRD bindings
  (`packages/crds`) are excluded — they are not ours to annotate.
* **The source tree is layered, and the layering is a checked contract.**
  `kluster.stacks` → `kluster.components` → `kluster.providers` →
  `kluster.lib` → `kluster.conventions` → `putils`: a layer imports what is
  below it and nothing above it, and four further edges are forbidden
  outright (a script reaches no declaration; a custom provider knows no
  `conventions`; `putils` knows no installation; only `kluster.main`
  imports a stack program). `import-linter` enforces it. The contract is
  in `pyproject.toml`; what each layer is for is
  `docs/rfc/rfc-002-src-layout-and-the-gateway.md` §2.
* **Scripts are Python**, not shell — a shell script needs a reason (a
  handful of lines with no logic, or a context with no interpreter). They
  live under `src/kluster/scripts/` and are exposed as console scripts in
  `pyproject.toml` (`update_crds`, `credentials`, `state-backend`), the
  same way for every script; `just` recipes or symlinks are for
  convenience on top, never the home of the logic.
* How code and prose are written — naming, comments, component and
  provider architecture — is `docs/style/`, and a reviewer holds every
  change to it.

## Writing the prose

* **Every artifact is as-built.** Docs, comments and commit messages say
  what is, not what was done: no "verified on", no narrative of attempts,
  no history the reader has to subtract.
* **Prose is checked like code.** Every markdown file passes
  `ltex-cli-plus` against `.vscode/ltex.dictionary.en-US.txt` and
  `.vscode/ltex.disabledRules.en-US.txt`. The binary is **not on `PATH`**
  — on this machine it is `~/.local/share/nvim/mason/bin/ltex-cli-plus` —
  and it reads neither word list on its own, so a run is two steps: build
  a client configuration carrying the contents of both lists, then check
  one file against that configuration.

      python3 - <<'PY'
      import json, pathlib
      words = lambda p: pathlib.Path(p).read_text().split()
      pathlib.Path(".claude/ltex.json").write_text(json.dumps({
          "dictionary": {"en-US": words(".vscode/ltex.dictionary.en-US.txt")},
          "disabledRules": {"en-US": words(".vscode/ltex.disabledRules.en-US.txt")},
      }))
      PY
      ~/.local/share/nvim/mason/bin/ltex-cli-plus \
          --client-configuration=.claude/ltex.json <file.md>

  - Both word-list files are one entry per line with **no comment
    syntax**, and the dictionary is **case-sensitive** — `homelab` and
    `Homelab` are two entries, so do not deduplicate them
    case-insensitively.
  - **The configuration is a path, and the words inside it are literal.**
    `--client-configuration` takes a file path; handed the JSON itself it
    fails with `File name too long`, which reads as a filesystem problem
    rather than a usage error. Handed a path that does not exist it prints
    a `java.nio.file.NoSuchFileException` stack trace and exits 1, which
    reads as a lint failure on the file being checked. And the
    `":<absolute path>"` value the VS Code extension accepts for
    `dictionary` and `disabledRules` — a reference to a word-list file
    rather than its contents — is **silently dropped** here: no error, the
    same exit code, and a run indistinguishable from one configured with
    `{}`. So the words go into the configuration whole, and the
    configuration itself goes inside the workspace, where `.gitignore`
    covers `.claude/`: a copy under `/tmp` is reaped between rounds and
    comes back as that stack trace.
  - **Confirm the configuration loaded before believing any finding.**
    Under a working configuration `docs/framework/github.md` reports
    exactly two `info` findings; a count in the thirties on that file
    means the word lists never reached the checker, and every finding on
    the file actually under review is then suspect. Without this check the
    failure looks like a large, plausible prose regression on a file that
    is clean, and the repair is to rewrite correct sentences.
  - **A dictionary entry is for a term this repository owns.** Anything
    else gets the prose reworded instead: the dictionary is shared by
    every open pull request, and growing it from a change about something
    else is scope this repository would rather not take.
  - Disable a rule only when it is systematically wrong for this repo (a
    firewall `ACCEPT`, a `.phd` domain, `key id`, the dot in `A1.Flex`,
    alice/bob as instance names). A one-off gets the prose fixed instead.
  - Run it **one file at a time**: given many files at once it hangs
    rather than finishing.
  - Some findings are **artifacts of the checker** and are answered by
    neither a dictionary entry nor a disabled rule nor a reword: it
    mis-columns inside very long table rows; it reports a fragment of a
    word as a misspelling; and it produces **sentence-segmentation
    artifacts**, where a grammar rule fires on a sentence the change never
    touched. That last one comes from the conversion the checker runs
    before it reads a file: inline code spans become dummy tokens whose
    length differs from the source, and enough of them ahead of a line
    shift where the checker believes a sentence starts. The first two
    give themselves away on sight; this one does not, because the
    sentence reads fine and the rule name is real. Its tells are that
    the finding does **not** reproduce on `main`'s copy of the same
    file, and that it moves or vanishes when unrelated nearby text
    changes length.

## Working beside other agents

* **An agent owns only the paths its brief names.** Needing one outside
  that list is a reason to stop and report, never to widen scope.
* Shared files — `AGENTS.md`, `docs/framework/ci.md`, `pyproject.toml` —
  are **serialized**: at most one open pull request may touch each of
  them, whoever opened it.
* Work happens in a `jj` workspace of its own (`jj workspace add -r main
  .claude/workspaces/<name>`), and **a workspace dies with the dispatch
  that created it** — the agent removes its own when it reports with no
  pull request, the merging dispatcher removes it after the merge.
* **After each piece of work, `jj new`**, so that `@` is always the empty
  change and the work sits at `@-`. Pushing is then two commands, and
  `jj log` first, confirming the work is at `@-`, is part of the form:
  `jj bookmark set <branch> -r @-`, then
  `jj git push --bookmark <branch>`. Both halves exit zero without
  pushing the work if that precondition does not hold — work left in
  `@` puts `main` on the branch instead, and a bookmark nobody moved
  reports `Nothing changed` and sends nothing at all. Read the head SHA
  back with `jj log -r <branch> --no-graph -T commit_id`;
  `git rev-parse HEAD` answers about the primary checkout, not the
  workspace. The failure modes and the rest of the protocol are
  [dispatch.md](docs/framework/dispatch.md) §1.2.
* Implementation-period issues live in the `kluster-ops` repo, not in this
  one and not in a checked-in list. What is unimplemented *here* announces
  itself: an unwritten stack raises from its entrypoint, and a register row
  with no implementation is a subcommand that refuses by name. Build order is
  `docs/cluster/migration.md` §1.
