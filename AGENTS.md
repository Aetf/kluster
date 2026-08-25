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
