# kluster

[![checks](https://github.com/Aetf/kluster/actions/workflows/checks.yml/badge.svg)](https://github.com/Aetf/kluster/actions/workflows/checks.yml)
[![deploy](https://github.com/Aetf/kluster/actions/workflows/deploy.yml/badge.svg)](https://github.com/Aetf/kluster/actions/workflows/deploy.yml)

One person's home infrastructure, declared: a Talos Linux Kubernetes cluster
spanning an OCI VCN and a homelab LAN with Cilium networking, plus the
machines around it — the gateway, DNS, the appliance holding Pulumi's own
state. Everything is Pulumi Python, applied by CI, and the design decisions
behind it are written down in `docs/` rather than lost.

It succeeds a k3s-based cluster and will run nearly the same workloads on new
infrastructure. It is not a template: it names one estate's hosts, networks
and accounts throughout, and nothing here is parameterized for reuse. It is
public because the CI security model needs it to be — branch protection is
not available on a private repository under this plan — and readable because
the reasoning is the point.

Managed with [uv](https://github.com/astral-sh/uv) and
[mise](https://mise.jdx.dev).

## Layout

| Path | What |
| --- | --- |
| `__main__.py` | Pulumi program entrypoint; registers the async `kluster.main.main` via `pulumi.run`. Must stay a real file (a console-script symlink's `sys.exit` would kill the async entrypoint before it runs). |
| `src/putils/` | The Pulumi framework layer: `Component`, `async_output`/`resolve` (RFC-001), asyncio helpers. |
| `src/kluster/` | The program itself: `physical/` declares the OCI and Talos layer, `stacks/` dispatches, `scripts/` holds the console scripts. |
| `deploy/` | Deployment material that is not library code — the state-backend appliance's Butane file, its dump script, its operator keys. |
| `packages/crds/` | `crd2pulumi`-generated CRD types, regenerated via `uv run update_crds` (still the legacy cluster's chart set). |
| `docs/` | Design docs. |
| `tests/` | Unit tests: the framework layer against Pulumi mocks, the scripts against real files. No cloud access. |

## Working on it

```sh
mise x uv -- uv sync
timeout 300 mise x uv -- uv run pytest    # always with a timeout: an
                                          # unresolved coroutine hangs
mise x uv -- uv run ruff check .
mise x uv -- uv run basedpyright
mise x -- pulumi preview --stack <layer>
```

A `pulumi` run needs two things that cannot be looked up: `PULUMI_BACKEND_URL`,
written by `state-backend provision` beside the client certificate it names,
and `PULUMI_CONFIG_PASSPHRASE`, derived from the offline derivation seed and
stored in no slot at all. `mise.toml` picks up the first from the client bundle
in `~/.config/kluster/state-backend/`, and the second from a git-ignored
`.pulumi.secret` — so a workstation without the offline kit needs the bundle
copied to it and the passphrase cached once:

```sh
# on the workstation that holds the kit
rsync -a ~/.config/kluster/state-backend/ <host>:~/.config/kluster/state-backend/
mise x uv -- uv run credentials derive passphrase | ssh <host> 'umask 077; cat > ~/kluster/.pulumi.secret'
```

The `github` stack additionally needs `GITHUB_TOKEN`, which `mise.toml` reads
from a git-ignored `.github.token`. That one is an account root rather than a
derived secret: nothing here can recreate it, which is deliberate -- that stack
is applied by hand and never by CI.

The console scripts — `credentials`, `state-backend`, `update_crds` — are the
operator-side half of the estate; each one's `--help` is written to say when it
is run, not only what it does.

## Docs

Four directories and two registers. `docs/cluster/` is what is being built and
why; `docs/physical/` designs the machines and appliances themselves;
`docs/declarative/` covers how each layer is declared in the program;
`docs/framework/` is the Pulumi Python framework, the CI, and the forge. At the
root, [credentials.md](docs/credentials.md) is the register of every credential
— scope, slot, rotation — and [operations.md](docs/operations.md) is day-2:
update ownership, upgrade and replacement runbooks, the drill program.

Reading order for a stranger:
[cluster/architecture.md](docs/cluster/architecture.md) (the canonical design,
including what was rejected), then
[framework/pulumi.md](docs/framework/pulumi.md) (§1.4 is the cookbook), then
whichever layer is in question. The migration plan and its wave order live in
[cluster/migration.md](docs/cluster/migration.md), which is also the order for
a rebuild from nothing.

## Status

Under construction, in the open. Built and running: the framework (RFC-001
Rev 3), the stack dispatch, the credential scripts, and the state-backend
appliance — a Fedora CoreOS box in OCI serving Pulumi's Postgres state over
mutual TLS, whose only apply path is re-provisioning it from this repository.
The CI workflow set and renovate are wired.

What is *not* built announces itself rather than being listed here: an
unimplemented stack raises from its entrypoint, and a seed the register names
without an implementation is a subcommand that refuses with its own name.
Implementation issues are tracked in a separate ops repository, deliberately
not here.

## License

MIT OR Apache-2.0, at your option.
