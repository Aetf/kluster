# kluster

[![checks](https://github.com/Aetf/kluster/actions/workflows/checks.yml/badge.svg)](https://github.com/Aetf/kluster/actions/workflows/checks.yml)
[![deploy](https://github.com/Aetf/kluster/actions/workflows/deploy.yml/badge.svg)](https://github.com/Aetf/kluster/actions/workflows/deploy.yml)

One person's home infrastructure, declared: a Talos Linux Kubernetes cluster
spanning an OCI VCN and a homelab LAN with Cilium networking, plus the
surrounding machines — the gateway, DNS, the appliance holding Pulumi's own
state. Everything is Pulumi Python, applied by CI, and the design decisions
behind it are written down in `docs/` rather than lost.

It succeeds a k3s-based cluster and will run nearly the same workloads on new
infrastructure. It is not a template: it names one installation's hosts,
networks and accounts throughout, and nothing here is parameterized for
reuse. It is public because the CI security model needs it to be — branch
protection is not available on a private repository under this plan — and
readable because the reasoning is the point.

Managed with [uv](https://github.com/astral-sh/uv) and
[mise](https://mise.jdx.dev).

## Layout

| Path | What |
| --- | --- |
| `__main__.py` (repository root) | Pulumi program entrypoint; registers the async `kluster.main.main` via `pulumi.run`. Must stay a real file (a console-script symlink's `sys.exit` would kill the async entrypoint before it runs). |
| `src/putils/` | The Pulumi framework layer: `Component`, `async_output`/`resolve` (RFC-001), asyncio helpers. |
| `src/kluster/` | The program itself: `components/` declares the resources area by area, `providers/` talks to the systems Pulumi has no provider for, `stacks/` dispatches, `scripts/` holds the console scripts, and `lib/`, `conventions/` hold what the rest share — the state-backend appliance's machine among it, its Butane file, dump script and operator keys beside the code that renders them (`lib/state_backend/`). |
| `deploy/` | What a cloud session runs before it starts: `cloud-session/toolchain.sh`, which installs its tools (docs/framework/dispatch.md §1.4). |
| `docker/` | The self-built container images: per image, a build file plus a `.conf` holding its build args and tag. Published by the `images` workflow. |
| `packages/crds/` | The CRD bundle (`crds.yaml`) the CRD types are generated from, rendered via `mise x -- uv run update_crds` from the chart and manifest pins in `Pulumi.yaml`'s `versions:` block, with the record of the pins it read (`rendered-from.json`). |
| `sdks/` | The generated SDKs, from the `packages:` block of `Pulumi.yaml` by `pulumi install` and committed: the bridged providers (`b2`, `unifi`, `zerotier`) and the CRD types (`crds`, the Kubernetes provider's extension, generated from the bundle above). A test holds each to the block, and renovate moves the pins (docs/framework/ci.md §3). |
| `checkpoints/` | The committed state of the operator stacks whose state is not in the appliance's backend — the `state-backend` stack's, which declares that backend — as Pulumi's `file://` backend lays it out. Written by a run of `operator-stack` and landed like any change; the files Pulumi writes beside it never leave the workstation (docs/framework/pulumi.md §3.3). |
| `escrow/` | Age ciphertexts of the secrets no provider mints, one file per generation. Committed on purpose: what protects them is the recovery key, which is in the offline kit and nowhere else (docs/credentials.md §2.2). |
| `docs/` | Design docs. |
| `tests/` | Unit tests: the framework layer against Pulumi mocks, the scripts against real files, none of it reaching a cloud. The exception is `tests/live/`, opt-in drills against real provider accounts that are collected only when `RUN_LIVE_DRILLS=1` is set (docs/framework/testing.md §5). |

## Working on it

```sh
mise x uv -- uv sync
timeout 1200 mise x uv -- uv run pytest   # the outer hang guard; the per-case
                                          # bound is pyproject.toml's
mise x uv -- uv run ruff check .
mise x uv -- uv run basedpyright
mise x -- pulumi preview --stack <stack>   # any stack but physical and the operator stacks
mise x -- env -u PULUMI_CONFIG_PASSPHRASE PULUMI_CONFIG_PASSPHRASE_FILE=.credentials/physical.passphrase \
    pulumi preview --stack physical         # physical, under its own passphrase
mise x uv -- uv run operator-stack github plan   # an operator stack
```

A `pulumi` run needs two things that cannot be looked up: `PULUMI_BACKEND_URL`,
written by `state-backend bundle operator` into the same slot as the client
bundle it authenticates with, and the passphrase that opens the stack's configuration.
That is the stack passphrase in `PULUMI_CONFIG_PASSPHRASE` for every stack
but two kinds. `physical` is encrypted under a passphrase of its own, which
only its two CI Environments carry, the ones no pull request can reach. The
operator stacks — the stacks no CI job runs, `github` and `state-backend`
today — are encrypted under the operator passphrase, which no CI environment
carries. Each passphrase is a random secret whose only recoverable
copy is a ciphertext committed under `escrow/`, which the offline kit's recovery
key alone opens (docs/credentials.md §2.2). The rest is read from
`.credentials/`, a git-ignored directory in the checkout holding everything
local this repository needs — the seed kit, the cached stack passphrase
(`pulumi.passphrase`), `physical`'s passphrase (`physical.passphrase`), the state backend's client bundle, the operator passphrase (`operator.passphrase`) on a
machine whose desktop secret store does not hold it, and the account roots'
token files on one with no store (docs/credentials.md §4.4). `mise.toml` reads the stack passphrase and the
bundle from there, and the `operator-stack` driver the bundle; the driver finds
the operator passphrase in the desktop secret store, then that slot, then
`KLUSTER_OPERATOR_PASSPHRASE`, and otherwise asks at the terminal
(docs/credentials.md §2). A workstation is set up by copying that directory
from one that already has it:

```sh
# on the workstation that holds the kit; leave kit.kdbx behind unless the
# other machine is meant to hold the offline kit too
rsync -a --exclude kit.kdbx .credentials/ <host>:<checkout>/.credentials/
```

The two checkouts need not sit at the same path: the connection string names no
file, and the bundle's three certificates travel beside it as `PGSSLROOTCERT`,
`PGSSLCERT` and `PGSSLKEY`, which `mise.toml` derives from the slot of whichever
checkout it runs in (docs/physical/state-backend.md §3).

On a machine that holds the kit, `credentials derived pulumi-passphrase recover`
writes the stack passphrase's slot, `credentials derived physical-passphrase
recover` writes `physical`'s, `credentials derived operator-passphrase
recover` keeps the operator passphrase in the desktop secret store (in its slot
where there is no store), and `state-backend bundle operator --address <ip>`
writes the bundle.

`pulumi` reads one passphrase per process, so which one a run needs depends on
the stack it names, and `mise.toml` cannot see the command line. A stack no CI
job runs -- an operator stack, `github` or `state-backend` today --
therefore goes through the `operator-stack` driver: `operator-stack github
plan`, `operator-stack github up`, or `operator-stack github pulumi <pulumi
arguments>`, which fixes the stack and hands `pulumi` that stack's backend and
passphrase (docs/framework/pulumi.md §3.3). A bare `pulumi … --stack github` meets the stack passphrase and stops at
`error: incorrect passphrase`, having written nothing. A run by hand against
`physical`, which CI does run, names that stack's slot to `pulumi` itself, with
the stack passphrase taken out of its environment (docs/credentials.md §4.4):

```sh
mise x -- env -u PULUMI_CONFIG_PASSPHRASE \
    PULUMI_CONFIG_PASSPHRASE_FILE=.credentials/physical.passphrase \
    pulumi preview --stack physical
```

No provider credential is in that directory: every one is a secret in the
committed configuration of the stack that reads it, which the passphrases above
open — the state-backend appliance's OCI key included, which `credentials
derived oci-state-backend mint` writes into the `state-backend` stack's
(docs/credentials.md §3). The
`github` stack's admin token is the one nothing here can mint -- GitHub
publishes no API that creates a personal access token -- so it is made on the
account's settings page and `credentials derived github-admin record` takes it
from there into `Pulumi.github.yaml` (docs/credentials.md §3).

The console scripts — `credentials`, `state-backend`, `update_crds` — are the
operator-side half of the installation; each one's `--help` is written to say
when it is run, not only what it does.

## Docs

Six directories, and at the root the documents that span them.
`docs/cluster/` is what is being built and why; `docs/physical/` designs the
machines and appliances themselves; `docs/declarative/` covers how each layer
is declared in the program; `docs/framework/` is the Pulumi Python framework,
the CI, and the forge; `docs/style/` is how code and prose here are written;
`docs/rfc/` keeps the accepted proposals the other directories were changed
by. At the root, [threat-model.md](docs/threat-model.md) is who can act on the
installation, which of them it defends against, and the test a security
finding must pass to be worth fixing;
[credentials.md](docs/credentials.md) is the register of every credential —
scope, slot, rotation — and [operations.md](docs/operations.md) is day-2:
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
Rev 3), the stack dispatch, the credential scripts, the state-backend
appliance — a Fedora CoreOS box in OCI serving Pulumi's Postgres state over
mutual TLS, declared by the `state-backend` stack, whose cutover from the
script that built the box is the operator's next step
(`docs/rfc/rfc-006-state-backend-stack.md` §14)
— and the `dns` stack, which declares this installation's Cloudflare zones
and records and holds them imported, not yet applied: DNSControl stays
authoritative until the zones' cutover
([docs/sources-of-truth.md](docs/sources-of-truth.md)). The CI workflow set and renovate are
wired for this repository, and the `images` workflow builds and publishes the
self-built container images in `docker/` to ghcr, multi-arch on native runners.

What is *not* built announces itself rather than being listed here: an
unimplemented stack raises from its entrypoint, a seed the register names
without an implementation is a subcommand that refuses with its own name, and
`credentials derived ls` marks each derived credential whose producer is not
built as `unbuilt`, saying what stands in the way.
Implementation issues are tracked in a separate ops repository, deliberately
not here.

## License

MIT OR Apache-2.0, at your option.
