# kluster-py

Pulumi python code to build my k8s cluster using Talos on GCP.

Project managed by [uv](https://github.com/astral-sh/uv) and mise.

## Tools

**pulumi**: `mise x -- pulumi`.

Anything installed by `uv`:

**ruff**: `mise x uv -- uv run ruff`.

## Architecture

The architecture design has been consolidated and updated in
[docs/architecture.md](docs/architecture.md). Please refer to that file for the
current design.

## Docs

- [docs/pulumi.md](docs/pulumi.md) — the Pulumi Python framework: `Component`,
  `async_output`/`resolve`, stack layering. Start here, §1.4 has cookbook
  examples.
- [docs/rfc-001-native-async-inputs.md](docs/rfc-001-native-async-inputs.md) —
  design rationale and mechanics of the native async inputs framework.
- [docs/testing.md](docs/testing.md) — unit testing Pulumi code with mocks.
