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
* Repo status and open design decisions are tracked in README "Status & open
  decisions" — check it before starting infra work.
