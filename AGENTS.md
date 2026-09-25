# Agent notes

What an agent needs before touching anything here. The protocol around
the work — how it is dispatched, reviewed, and reported — is
[docs/framework/dispatch.md](docs/framework/dispatch.md).

## Environment

* ALWAYS use `mise x uv -- uv` to manage python environment of the project
* ALWAYS run tests under a timer — the `timeout … pytest` form under "The
  gate" — so a hang ends the case, or the run, instead of waiting forever

## The gate

A change is done when every one of these passes. CI runs the same set,
except where a bullet says otherwise:

* `mise x uv -- uv run ruff check` and `ruff format --check`
* `mise x uv -- uv run basedpyright` — strict, clean
* `mise x uv -- uv run lint-imports` — the layering contract below
* `timeout 1200 mise x uv -- uv run pytest` — a per-case bound inside
  the run (`pytest-timeout`, configured in `pyproject.toml`) fails a hung
  case by name and lets the run go on to its summary; the outer `timeout`
  is a hang guard for what that bound cannot reach, an order of magnitude
  above the run's duration. Which form and why:
  [docs/framework/testing.md](docs/framework/testing.md) §1.
* `ltex-cli-plus` on every markdown file touched, one file at a time —
  how it reaches the repository's word lists is under "Writing the
  prose". CI runs it the same way, over the markdown the pull request
  changed, or over every markdown file in the cases that section names.
* a claim the change made false is swept for — not the identifier that
  moved. How to shape the patterns, and what a sweep that found nothing
  owes the pull request, are in
  [docs/style/README.md](docs/style/README.md) under "Comments and
  docs". This one runs on a workstation only; CI does not run it.
* provider-facing code has one more requirement —
  [dispatch.md](docs/framework/dispatch.md) §1.1

New behavior ships with a test that fails without it, and the
documentation the change makes true ships with it rather than after it.

## Writing the code

* Read `docs/framework/pulumi.md` before writing components; its §1–2
  are the framework itself and how `putils` implements it. Key rules:
  - Sub-resources are created synchronously in `Component.__init__`; async input
    prep goes through `async_output`, and outputs are awaited only via `resolve`
    inside those coroutines (`resolve` hard-errors anywhere else, including the
    `pulumi.run` entrypoint and tests — in tests, await `.future()` instead).
  - `__main__.py` must stay a real file (not a console-script symlink): the
    script's `sys.exit` would kill the `pulumi.run` async entrypoint.
* Python code standard, enforced on everything under `src/`, `tests/`, and
  `deploy/`: **fully type-annotated, and `basedpyright` strict passes clean**.
  Config lives in `pyproject.toml`; the only project-wide relaxations
  are `reportAny`/`reportExplicitAny`/`reportUnusedCallResult`, which
  fight a provider-SDK codebase more than they help. A module that is
  glue over an untyped or partially typed library turns off, for that
  file alone, the checks the library defeats — a file-level
  `# pyright: report…=false` comment, and the file names the library.
  Generated bindings — `packages/crds` and the SDKs under `sdks/` — are
  excluded; they are not ours to annotate.
* **The source tree is layered, and the layering is a checked contract.**
  `kluster.stacks` → `kluster.components` → `kluster.providers` →
  `kluster.lib` → `kluster.conventions` → `putils`: a layer imports what is
  below it and nothing above it, and further edges are forbidden
  outright, each named in the contract (a script reaches no
  declaration; a custom provider knows no `conventions`; `putils` knows
  no installation; only `kluster.main` imports a stack program).
  `import-linter` enforces it. The contract in `pyproject.toml` is the
  canon for the layers and the forbidden edges; what each layer is for,
  and why each edge is forbidden, is
  [docs/style/pulumi.md](docs/style/pulumi.md) under "Layering".
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
  `.vscode/ltex.disabledRules.en-US.txt`. The checker is pinned like
  every other gate tool — in `mise.toml`, since it is the one `uv.lock`
  cannot carry — so `mise x -- ltex-cli-plus` is the binary, installed
  by `mise install` and reached the same way on a workstation and in
  CI. It reads neither word list on its own, so a
  run is two steps — build a client configuration carrying the contents
  of both lists, then check one file against it. From the workspace
  root:

      mkdir -p .claude && python3 -c 'import json,pathlib as P;w=lambda n:P.Path(f".vscode/ltex.{n}.en-US.txt").read_text().split();P.Path(".claude/ltex.json").write_text(json.dumps({"dictionary":{"en-US":w("dictionary")},"disabledRules":{"en-US":w("disabledRules")}}))'
      mise x -- ltex-cli-plus --client-configuration=.claude/ltex.json <file.md>

  The `checks` workflow runs exactly that, one invocation per markdown
  file, and any finding fails it — the checker exits non-zero on `info`
  findings too. Which files is decided in `checks.yml`'s prose step.
  Ordinarily it is the markdown the change touched. It is **every**
  markdown file in the tree when the run has no base commit to diff
  against, and when the change touches the checker or what it reads —
  the `machinery` list in that step, which names `mise.toml` (where the
  checker is pinned), both word lists and `checks.yml` itself
  ([docs/framework/ci.md](docs/framework/ci.md) §3 says why). Outside
  those cases a file the change did not touch is not checked, so a
  finding an untouched file already carries on `main` surfaces on the
  first change that touches it.

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
    configuration goes where it will still be there when the checker runs
    and will not be committed: `.claude/` inside the workspace, which
    `.gitignore` covers and which the recipe above creates.
  - **Confirm the configuration loaded before believing any finding.**
    The control is one file checked under two configurations: the built
    `.claude/ltex.json`, and a second file holding only `{}`. On a file
    that is clean on `main` — `docs/framework/github.md` is one — the
    built configuration reports nothing and the empty one reports dozens
    of findings, the word lists being all that separates the two runs.
    Two runs that agree mean the word lists never reached the checker,
    and every finding on the file actually under review is then
    suspect. Skip this check and a misconfigured run reads
    as a large, plausible prose regression on a file that is clean,
    inviting the mistaken repair: rewriting correct sentences, or growing
    a word list to silence them.
  - **The prose step does not run while `jj st` names a conflicted
    file.** A materialized conflict is prose to this checker: change ids
    come back as misspellings and grammar findings land on lines that
    have no grammar problem, and none of it survives resolving the
    conflict. Rewriting a commit that another commit sits on — a fold,
    or a `jj squash --into @-` — is what can leave one behind:
    [dispatch.md](docs/framework/dispatch.md) §1.2.
  - **A dictionary entry is for a term this repository owns.** Anything
    else gets the prose reworded instead. The word lists are **not** in
    the serialized set below; what keeps them out of contention is that a
    change about something else does not grow them — that is scope this
    repository would rather not take.
  - Disable a rule only when it is systematically wrong for this repo (a
    firewall `ACCEPT`, a `.phd` domain, `key id`, the dot in `A1.Flex`,
    alice/bob as instance names). A one-off gets the prose fixed instead.
  - Run it **one file at a time**: given many files at once it hangs
    rather than finishing.
  - Some findings are **artifacts of the checker** and are answered by
    neither a dictionary entry nor a disabled rule. Both kinds below come
    from the conversion the checker runs before it reads a file: inline
    code spans become dummy tokens whose length differs from the source.
    - **The first body row of a table whose first cell is a lone code
      span** is misread: every finding on that row lands at a shifted
      offset and reports a fragment of neighboring words, or the dummy
      token itself, as a misspelling (`Dummy0 Pu`, `gram entrypo`), and
      a word the dictionary holds is flagged there all the same. The row's
      length and any other code spans in it change nothing; a row of the
      same shape below it passes, and reordering the rows moves the
      finding to whichever row is first. The cure is the row's shape: a
      column order that does not lead with the code span, or any text
      beside the span in that cell. The dictionary is never the answer —
      the word it trips on is already there, and a fragment added to the
      list would mask a real misspelling. Nor is muting: the only disable
      the checker honors in a markdown file is the block-level
      `<!-- LTeX: enabled=false -->` … `<!-- LTeX: enabled=true -->` pair,
      each on a line of its own (inline, it is whitespace), and it mutes
      the table's prose cells too. A misspelling that spans a verb and a
      bare tool name (`runs apid`) is not this artifact: LanguageTool is
      offering to merge or split the pair, and the code span the name
      should have had is the reword.
    - A **sentence-segmentation artifact** is a grammar rule firing on a
      sentence the change never touched: enough dummy tokens ahead of a
      line shift where the checker believes a sentence starts. A
      fragment gives itself away on sight; a segmentation artifact does
      not, because the sentence reads fine and the rule name is real. Its
      tells are that the finding does **not** reproduce on `main`'s copy
      of the same file, and that it moves or vanishes when unrelated
      nearby text changes length.

## Working beside other agents

* **An agent owns only the paths its brief names.** Needing one outside
  that list is a reason to stop and report, never to widen scope. The
  one thing that list already permits is a new file the gate demands and
  no owned path can hold, disclosed in the pull request —
  [dispatch.md](docs/framework/dispatch.md) §1.1.
* Shared files — `AGENTS.md`, `docs/framework/ci.md`, `pyproject.toml` —
  are **serialized**: at most one open pull request may touch each of
  them, whoever opened it.
* Work happens in a `jj` workspace of its own (`jj workspace add -r main
  .claude/workspaces/<name>`, then `mise trust` inside it, which every
  `mise x uv` command otherwise refuses), and **a workspace dies with the
  dispatch that created it** — the agent removes its own when it reports
  with no pull request, the merging dispatcher removes it after the
  merge.
  **Scratch goes under the workspace's own `.claude/`**, which
  `.gitignore` covers: a workspace root is a checkout, so a file written
  anywhere else in it is a repository path that the next `jj` command
  snapshots into the change.
* **At the moment of a push, `@` is empty and `@-` is the work.**
  That invariant is the rule, and `jj new` is how it is restored once a
  piece of work is done rather than a command to run unconditionally:
  run against an `@` that is already empty it stacks a second empty
  change, and the bookmark below lands on an empty change instead of on
  the work. So describe the work, then `jj new`, and only then push, in
  two commands with `jj log` first — confirming the work is at `@-` is
  part of the form: `jj bookmark set <branch> -r @-`, then
  `jj git push --bookmark <branch>`. The ways that precondition breaks
  silently are the dangerous ones — both halves exit zero and the branch
  carries none of the work: work left in `@` sets the bookmark on
  whatever `@-` held before it, `main`'s tip on a first round, and a
  bookmark nobody moved reports `Nothing changed` and sends nothing at
  all. The stacked second empty change above is guarded instead: the
  `set` warns `Target revision is empty` and exits zero, and the push
  then refuses with `Won't push commit … since it has no description`
  and a non-zero exit. Read the head SHA back with
  `jj log -r <branch> --no-graph -T commit_id`, not with
  `git rev-parse HEAD`. That trap is one instance of a rule: inside a
  workspace, git answers about the primary checkout wherever the answer
  depends on a working copy, on `HEAD`, or on a path resolved relative
  to the current directory. The failure modes, the rest of that rule and
  the rest of the protocol are
  [dispatch.md](docs/framework/dispatch.md) §1.2.
* **A builder never fetches; the dispatcher fetches and rebases.** A
  branch that opens behind `main` is the expected case, because bringing
  it up to date is a step of the merge rather than of the work. The one
  exception is a rebase that conflicts: that one comes back to the
  builder, onto the `main` the dispatcher's fetch has already brought
  in, still without fetching.
* Implementation-period issues live in the `kluster-ops` repo, not in this
  one and not in a checked-in list. What is unimplemented *here* announces
  itself: an unwritten stack raises from its entrypoint; a seed whose mint
  is not written is a `credentials seed` member that refuses by name; and
  a derived row whose producer is not built is marked `unbuilt`, with what
  stands in the way, by `credentials derived ls`, which needs no kit. Such
  a row has no `credentials derived <row>` subcommand, so naming one is an
  argument error (`invalid choice`) rather than a refusal. Build order is
  `docs/cluster/migration.md` §1.
