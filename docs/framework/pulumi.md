# Pulumi Python Framework Design

Objective: Design and document the Python framework for Pulumi that reduces
boilerplate, handles async operations gracefully, and avoids "callback hell"
when dealing with Outputs.

## 1. Core Concepts

### 1.1 Native Async Inputs (RFC-001)

The framework provides an ergonomic, type-safe, and 100% native `async/await`
solution that allows synchronous sub-resource construction alongside
asynchronous parameter preparation, while strictly preserving Pulumi's core
safety guarantees (the DAG and dry-run preview capabilities).

Sub-resources are constructed synchronously in the component's `__init__`,
exactly like plain Pulumi code. Any input that needs async preparation is
wrapped with `async_output`; inside the coroutine, other outputs are awaited
natively via `resolve()`, which also captures the resources behind them as
dependencies (both internal children and external constructor-passed ones)
and propagates their secretness to the resulting input.

#### Pattern for Component:

```python
import pulumi
from putils import Component, async_output, resolve

class MyComponent(Component):
    def __init__(self, name: str, opts: pulumi.ResourceOptions | None = None):
        super().__init__(name, opts=opts)

        # Sub-resources are created synchronously, in plain Python order.
        self.vpc = gcp.compute.Network(
            f'{name}-vpc',
            auto_create_subnetworks=False,
            opts=self.child_opts(),
        )
        self.subnet = gcp.compute.Subnetwork(
            f'{name}-subnet',
            cidr='10.0.1.0/24',                         # known: passed plainly
            network_id=async_output(self._network_id),  # async: wrapped
            opts=self.child_opts(protect=True),
        )
        self.register_outputs({})

    async def _network_id(self) -> str:
        # Natively await outputs, then apply standard Python transformations.
        vpc_id = await resolve(self.vpc.id)
        return f'subnet-for-{vpc_id.upper()}'
```

`resolve(x)` returns the plain value; `resolve(x, y)` returns a tuple and
gathers dependencies from all arguments at once.

### 1.2 Fine-Grained Diffs on an Unknown Value

Known values are passed as plain arguments, so they always show up concretely
in a diff. If an `async_output` coroutine awaits a value that is unknown,
`resolve` raises `UnknownValueException` to abort that coroutine early, and
only that one input becomes `UNKNOWN` — sibling inputs of the same resource are
unaffected. No special protocol is needed.

The abort does not ask which kind of run it is in: an unknown is a property
of the value, not of the run. How an update comes to hold one, and what the
engine then does with it, is under "When an awaited value is unknown" below.

### 1.3 Parent Propagation via `child_opts`

Instead of writing `opts=pulumi.ResourceOptions(parent=self)` for every child,
use `self.child_opts()`. Extra options pass through
(`self.child_opts(protect=True, depends_on=[other])`), and an explicit
`ResourceOptions` can be merged in via `self.child_opts(opts=...)`.

#### The parent backstop

Forgetting it is not an error to Pulumi. The resource lands on the stack
rather than on the component, inherits the stack's providers rather than the
component's, and whatever goes wrong afterward complains about a provider
rather than about a parent. So the framework refuses it, in the one way a
framework can: `install_parent_backstop()` registers a stack transformation
that fails any resource registered while a component is under construction
whose options name no parent, naming both the resource and the component.

It refuses rather than repairs, and rfc-002 §8.2 is where that reads as a
conclusion rather than an assertion.

Consequences worth knowing:

-   **The scope is `super().__init__()` … `register_outputs()`**, which is why
    every component ends its constructor with the latter. A component is
    pushed onto the scope after its own registration, so a top-level component
    needs no parent of its own while a nested one does; a component that never
    calls `register_outputs` stays on the scope and the next unparented
    resource is refused in its name instead of its own.
-   **Nothing is exempt**, providers included. Resources declared outside any
    component, which is what a stack program does, pass untouched.
-   **A provider a component builds for itself is its sibling, not its
    child.** It is built before the component's own `super().__init__()`,
    because a provider reaches a subtree through the component's options and
    those are fixed at registration. `own_provider_opts(opts)` gives it the
    parent the component was given — which is also what keeps it from being
    refused inside an enclosing component — and `with_provider(opts, provider)`
    is the component's own options with the provider added, so every resource
    under the component inherits it without naming it.

`kluster.main` installs the backstop once, before any stack program runs; a resource
carries only the transformations that existed when its parent was built, so
the call has to come first.

### 1.4 Cookbook

Small self-contained recipes for common situations. All of them assume the
imports from §1.1.

#### Awaiting several outputs at once

`resolve` takes multiple outputs and returns a tuple. Prefer this over
sequential awaits when the values are independent — it registers all
dependencies in one step and wakes up once:

```python
async def _connection_string(self) -> str:
    host, port = await resolve(self.db.host, self.db.port)
    return f'postgres://{host}:{port}/mydb'
```

#### Using a resource passed in from outside

Nothing special is needed. Whatever the coroutine can reach — constructor
parameters, module globals — is tracked when awaited:

```python
class AppService(Component):
    def __init__(self, name: str, cluster: Cluster, opts=None):
        super().__init__(name, opts=opts)
        self.cluster = cluster
        self.deployment = Deployment(
            f'{name}-deploy',
            kubeconfig=async_output(self._kubeconfig),
            opts=self.child_opts(),
        )
        self.register_outputs({})

    async def _kubeconfig(self) -> str:
        endpoint = await resolve(self.cluster.endpoint)
        return render_kubeconfig(endpoint)
```

#### Conditional sub-resources

Sub-resources are created with plain Python, so a plain `if` works:

```python
def __init__(self, name: str, *, with_backup: bool = False, opts=None):
    super().__init__(name, opts=opts)
    self.volume = Volume(f'{name}-vol', opts=self.child_opts())
    if with_backup:
        self.backup = BackupPolicy(f'{name}-backup', opts=self.child_opts())
    self.register_outputs({})
```

#### Mixing real async work with outputs

The coroutine is ordinary asyncio code — call APIs, read files, sleep:

```python
async def _certificate(self) -> str:
    domain = await resolve(self.dns_record.fqdn)
    # any real async library works here
    async with httpx.AsyncClient() as client:
        resp = await client.get(f'https://ca.example.com/issue?domain={domain}')
    return resp.text
```

For blocking (synchronous) work, wrap it with `putils.background` to run it in
a thread instead of stalling the event loop:

```python
from putils import background

async def _machine_config(self) -> str:
    ip = await resolve(self.vm.ip)
    return await background(render_heavy_template)(ip)
```

#### A physical dependency without consuming an output

If a child must wait for another resource but doesn't use any of its values,
declare it in the options — same as plain Pulumi:

```python
self.app = Deployment(
    f'{name}-app',
    opts=self.child_opts(depends_on=[self.namespace]),
)
```

#### Program-level async work (`pulumi.run`)

The program entrypoint itself is async (`__main__.py` registers
`kluster.main.main` via `pulumi.run`, Pulumi >= 3.254). Async work that does
*not* consume resource outputs — external APIs, files, stack references —
belongs there, and stack outputs are published with `pulumi.export`:

```python
async def main() -> None:
    ami = await fetch_talos_ami()      # plain asyncio, no outputs involved
    cluster = Cluster('kluster', ami=ami)
    pulumi.export('endpoint', cluster.endpoint)
```

`resolve` deliberately refuses to run there (`RuntimeError`): feeding resource
outputs through async code is the job of `async_output` inside components,
which tracks dependencies and keeps an unknown from reaching Python.

#### When an awaited value is unknown

You don't need to do anything. If an awaited value is unknown (e.g. the VPC
does not exist yet), the coroutine is aborted and just that one input shows as
unknown in the diff; inputs passed plainly keep their concrete values.

A whole-stack `pulumi up` is the case where every value is known and every
coroutine does run to completion, but it is not the only case. **An update
restricted by `--target` skips the resources outside the target set**, and one
whose *create* is skipped has no outputs to give: they reach a program that is
otherwise applying for real as unknown. A skipped resource that already exists
is stepped over with the outputs already in state, which stay known, so this
is a condition of a stack part of which has never been applied rather than of
every targeted run. The engine accepts unknown inputs on the resources it
skips creating, which is why degrading the input is the right answer rather
than failing the run.

Where the resource consuming the unknown is itself targeted, the engine
refuses the update by name — `Resource 'A' depends on 'B' which was was not
specified in --target list`, doubled word and all — naming the URN to add.
The refusal is graph-based rather than value-based: it fires on the
dependency edge, whatever the input holds. What the abort restores is that
the registration reaches the engine at all, carrying the edge `resolve`
records before it aborts.

A third case is neither skipped nor refused. The stack's own resource is
never outside a target set, so an unknown that reaches a `pulumi.export` is
accepted and written to state as Pulumi's unknown sentinel — the literal
string `04da6b54-80e4-46f7-96ec-b56ff0331ba9`, in plain text even where the
export is a secret. `pulumi stack output` returns that string. A
`StackReference` reader never receives it as one:

-   **Previewing**, the reader gets an unknown, and not only for that
    output: one sentinel anywhere in the stack's outputs makes every
    output of the reference unknown, its well-formed ones included.
-   **Updating**, the reader gets an absence — `get_output` answers
    `None` and `require_output` raises `KeyError` — and a secret whose
    plaintext is the sentinel reads back as `None` from either.

So a targeted apply leaves every export that depends on a resource the
run skips creating poisoned until the rest of the stack is applied, and
nothing between the two runs may read one (§3.1).

## 2. Integration with `putils`

The framework is implemented in the library `src/putils` (stable; the async
half is verified by `tests/test_async_properties.py` and the parent backstop
by `tests/test_parenting.py`):

-   `component.py`: Provides the base `Component` class (the type token
    each subclass states, `child_opts()`), the parent backstop
    (`install_parent_backstop`, §1.3),
    and the two helpers for a provider a component builds for itself
    (`own_provider_opts` and `with_provider`, §1.3).
-   `paio.py`: Handles bridging `asyncio` with Pulumi, including `async_output`
    and `resolve`.

## 3. Stack Programs

A Pulumi *project* holds several *stacks*, and here they all share one
Python program. `__main__.py` hands `pulumi.run` an async entrypoint,
which looks `pulumi.get_stack()` up in a stack-name → program table
(`kluster.stacks`) and awaits exactly that program; an unknown name
fails by name rather than quietly declaring nothing. Stack selection is
the whole dispatch mechanism, so a run can only declare what the
selected program declares, and no configuration flag widens it. Because
the entrypoint dispatches rather than declares, a program publishes its
stack outputs with `pulumi.export` instead of returning a mapping.

Which stacks exist, what each one owns, and where the boundaries
between them fall are design decisions rather than mechanism:
[declarative/README.md](../declarative/README.md) §1.

### 3.1 Cross-stack data

A value reaches another stack by one of two routes, and they carry
different things:

-   **A stack output, read through a `StackReference`:**

    ```python
    import pulumi

    physical = pulumi.StackReference('organization/kluster-py/physical')
    kubeconfig = physical.require_output('kubeconfig')
    ```

    This is the only route for a value no program can know before an
    apply — an identifier the cloud generates, an address it assigns, a
    credential a resource mints. The cost is that a reader sees
    whatever the producer published last, so a preview taken before the
    producer applies previews stale values. Staleness is the milder
    hazard. An output that holds nothing usable reaches a reader with
    no error and nothing to mark it: `get_output` answers an absent
    output with `None`, an update reads one a targeted apply of the
    producer wrote as Pulumi's unknown sentinel the same way, and a
    preview reads every output of such a producer as unknown. Carried
    on unchecked, an absence becomes text — `str(None)` is `"None"` —
    so a reader that hands an output to a resource checks it at the read
    and refuses anything but a usable value by name, unknowns included
    (the `dns` stack's anchors, `_address` in `stacks/dns.py`). The
    mechanism, and the rule that nothing may read such an output until
    the rest of the producer is applied, are §1.4's "When an awaited
    value is unknown".
    Whether a reader uses `require_output`, as above, or `get_output`
    is [style/pulumi.md](../style/pulumi.md)'s rule under "Layering".

-   **A Python module both programs import.** The value is a literal,
    so it is concrete during preview and imposes no apply order. It
    only works for names the program itself chooses: a resource left to
    Pulumi's autonaming has no literal to share, so either autonaming
    is disabled and the name becomes shared code, or the generated name
    travels as a stack output.

Neither route is a dependency Pulumi can schedule. A resource in one
stack cannot depend on a resource in another, so any ordering the
resources really need — CRDs before the custom resources that are
instances of them, an API server before anything that speaks to it —
belongs to the deployment pipeline ([ci.md](ci.md)).

Which values take which route here is a design decision:
[declarative/README.md](../declarative/README.md) §2.

### 3.2 Version pins, and where a value shared by every stack lives

Pulumi has no include between stack configuration files, so a value
every stack agrees on would otherwise be one copy per stack, drifting
apart.
What it does have is **project-level configuration**: a `config:` block
in `Pulumi.yaml` whose values apply to every stack, which a stack's own
file overrides only where it deliberately differs. Two limits come with
it, neither of which bites for a pin: `pulumi config set` cannot write
there, so the values are hand-edited YAML; and a key in someone else's
namespace may carry a value but neither a type nor a default.

Every version pin a stack program reads lives in that block, in one
`versions:` namespace with **the kind in the key** —
`versions:talos`, `versions:image-<name>`, `versions:chart-<name>` and
`versions:manifest-<name>`, a container root filesystem being an image
like any other. One namespace because they are one kind of fact, a
build somebody else produced and this repository selects by version;
the prefix because it is what lets renovate's managers for a kind match
its own entries and nothing else.

The Talos release and an image are plain values. A chart and a manifest
are objects, and **an object is written under `value:`**, the one form
Pulumi's project schema accepts for one: written directly under the key,
the CLI refuses the whole file as an invalid type declaration. A chart
pin holds where the chart is served, its version and, for a chart from
an OCI registry, the digest of its manifest, which Helm pulls the chart
by; beside those, what `update_crds` needs to render it (§4) — whether
it renders definitions, the values that make it render them, and the
floor its operator version has to clear with the section that states
it. A manifest pin, a YAML bundle published as a release asset, holds
the GitHub repository, the release, the asset and the asset's sha256.
`lib/versions.py` exposes one accessor per kind, each checking the
pin's shape and returning it parsed — a chart as `ChartPin`, a manifest
as `ManifestPin`, an image as its repository, tag and digest, the Talos
release as a tag checked to be one — and each refusing a missing or
malformed pin by naming the key, so a pin nobody set fails where it is
read instead of somewhere downstream.

A pin no stack program reads lives with the tool that reads it: Python
dependencies in `pyproject.toml` and `uv.lock`, the command-line tools
in `mise.toml`, the bridged provider SDKs in `Pulumi.yaml`'s own
`packages:` block, a script's pins in that script's modules
(`update_crds/pins.py`), the container builds' pins beside their build
files under `docker/`, and the actions and mise's own release in the
workflows that call them (`.github/workflows/`).

**A pin that a stack program and a script both read lives in the
`versions:` block, and the script reads `Pulumi.yaml` itself, through
the parser the program's accessor uses.** `lib/versions.py` parses a
pin out of one of two sources: a program hands it its configuration
(`ProgramConfig`), where the engine passes an object's `value:` as JSON
text, and a script hands it the file's `config:` block (`ProjectFile`),
which refuses an object written outside `value:`. Each source undoes its
own shape, so one parse serves both, and the script and the program
cannot disagree on a pin's shape. The script reads the file rather than
asking `pulumi config`, which answers only for a selected stack — that
means reaching the state backend and holding the stack passphrase, and
a pin needs neither. **A pin a script reads is never overridden in a
stack's own file**, which the script does not read; a test holds that no
stack file carries one. `update_crds` is the case today: it reads the
chart and manifest pins the `k8s-base` program installs from (§4). The
state-backend appliance's pins are read the other way, from
`kluster.lib.state_backend.settings`, the module the appliance's render
shares with the `state-backend` script
([style/pulumi.md](../style/pulumi.md), "Layering"); moving them into
the block is that appliance's own change.

### 3.3 Operator stacks

**An operator stack is a stack no CI job runs.** Its configuration is
encrypted under the operator passphrase, which no CI Environment holds,
apart from the stack passphrase every other stack is under, and a run of
it starts from the workstation that holds the operator passphrase. The census of them is
`OPERATOR_STACKS` in `conventions.identity`, beside the stack names,
recording for each where its state lives: the appliance's backend, with
every stack CI deploys, or a checkpoint committed to this repository.
Today it holds `github`, whose state is in the backend, and
`state-backend`, whose state is committed. Everything that has to know which stacks are
held away from CI reads that census: the operator passphrase covers
exactly these ([credentials.md](../credentials.md) §3),
the census over the workflows keeps every command naming one out of CI
(`tests/test_conventions.py`), and the driver below runs these and
nothing else.

**An operator stack runs only through the one driver**, the
`operator-stack` console script, which sets the stack's backend and its
passphrase on the process it starts. Neither reaches an operator stack
through `mise.toml`'s `[env]`, and no `mise` task runs one
(`tests/test_mise_env.py`):

    operator-stack github plan
    operator-stack github up
    operator-stack github pulumi config get githubAdminToken

-   **The environment is the driver's.** `kluster.lib.stack_environment`
    maps a stack to it, and the `credentials` commands that write a
    stack's configuration reach every stack through the same mapping: the
    operator passphrase, found through the acquisition chain of
    [credentials.md](../credentials.md) §2 — the desktop secret store,
    its workstation slot, `KLUSTER_OPERATOR_PASSPHRASE`, a prompt at a
    terminal — and the backend the census names. That is the estate's backend, with the
    `operator` client bundle's connection string and its three files, or
    `file://<checkout>/checkpoints?metadata=skip` with
    `PULUMI_DIY_BACKEND_DISABLE_CHECKPOINT_BACKUPS` set. The process
    starts with none of the caller's variables that can steer `pulumi`
    or its backend: nothing under `PULUMI_`, libpq's `PG`, or the
    cloud-bucket backends' credentials (`AWS_`, `AZURE_STORAGE_`,
    `GOOGLE_APPLICATION_CREDENTIALS`), but `PULUMI_HOME` and
    `PULUMI_SKIP_UPDATE_CHECK`, which hold nothing of the stack's. So the
    stack passphrase `mise.toml` exports never reaches it, a backend a
    shell exported does not either, and neither does a switch that would
    compress the checkpoint or keep copies of it where the checks do not
    look — including one a later CLI release adds.
-   **The stack is the driver's first argument, never a flag it passes
    through.** A `--stack`, an `-s`, or a single-dash cluster holding an
    `s` ahead of a `--` is refused rather than overridden, since a caller
    who wrote one meant some other stack. A run refuses inside a `jj`
    workspace under `.claude/`, where the slots do not answer
    ([dispatch.md](dispatch.md) §1.2).
-   **`plan` is a refreshed preview**, exiting 0 when nothing is planned
    and 1 when something is. That is also how drift in an operator stack
    is read, since no scheduled job reads it ([ci.md](ci.md) §3). The
    preview is `pulumi preview --refresh --json` with its engine events
    streamed (`PULUMI_ENABLE_STREAMING_JSON_PREVIEW`, which the flag's own
    help names), and the driver prints the steps, the changed outputs and
    the count it read from them. What is planned is counted the way the
    engine's own `HasChanges` counts it, with two more kinds of change
    counted too, since the engine counts no step for either and an `up`
    writes both: a plain stack output, and a resource's `protect`,
    `retainOnDelete`, `provider` or `parent`, which the step's events
    carry before and after. **Two changes alone are neither planned nor
    applied**: one to a secret stack output, which the events show as
    the same placeholder before and after, and one to an option they do
    not carry at all — `dependsOn`, `deleteBeforeReplace`,
    `ignoreChanges`, `replaceOnChanges`, `additionalSecretOutputs`,
    `aliases`, `customTimeouts`. `operator-stack <stack> pulumi up`
    applies either. The reading fails closed: events that are not events, no
    summary, a summary that counts no step at all, or one that counts no
    change while a step names one are each a refusal rather than an empty
    plan. A test over the pinned CLI plans a real resource's change, so a
    release that moves the summary fails there.
-   **`up` refreshes, previews, asks, and applies**, and `--yes` skips
    the question. With nothing planned it runs no `up` at all and exits
    as `plan` does.
-   **A ^C is `pulumi`'s to answer.** The terminal sends it to `pulumi` as
    well as to the driver, and `pulumi` answers the first one by
    cancelling gracefully: it finishes the steps in flight and releases
    its lock. So the driver ignores SIGINT while `pulumi` runs and waits
    for it, rather than killing it partway through that cancel.
-   **Anything else is passed to `pulumi`**, as
    `operator-stack <stack> pulumi <arguments>`, with the stack added
    ahead of any `--`. An `up` passed through this way skips the
    driver's question, not the engine's own refusals. A `config cp` with
    a `--dest` or `-d` is refused like a `--stack`, since it names the
    stack it writes. An `import` or a `stack import` passed through for a
    stack whose state is committed is refused, for the reason below.
    These refusals read every word ahead of a `--` that is not a flag,
    wherever it stands, since `pulumi` takes a flag between a command's
    words: `stack --color=never import` is a `stack import`.
-   Exit 2 is a refusal, a failed preview, or a committed checkpoint that
    failed its checks.
-   **A stack with a gate of its own has it applied around `plan` and
    `up`.** The `state-backend` stack's is the one today
    ([physical/state-backend.md](../physical/state-backend.md) §1): an
    `up` that would create, replace or delete the box writes nothing and
    names what moved until it is given `--force`, or `--replace`, which
    replaces the box even when nothing moved; a create of the box
    beside a reserved address that already points at something is
    refused either way, and so is a run that would replace a resource
    the program imports by id, which the engine would fail part way; and
    after every `plan` and `up` the driver reads the estate's backend,
    answering 3 while it serves no stack, in place of any status but 2,
    and 4 while it does not answer, in place of 0 or 1.

**A stack whose state is committed** has its backend in `checkpoints/`
at the checkout's root, which the driver creates when it is missing,
since a `file://` backend refuses a root that does not exist. Of what
Pulumi writes there, the checkpoint,
`checkpoints/.pulumi/stacks/<project>/<stack>.json`, and
`checkpoints/.pulumi/meta.yaml` are what travels: a backend holding only
those two serves every operation and writes the rest back. The
checkpoint's `.bak` and the `history/` and `locks/` directories never
leave the machine; `metadata=skip` keeps the storage library from
writing an `.attrs` file beside every file, and the variable above turns
the `backups/` copies off. Why the appliance's state is kept this way,
and against which alternatives, is
[rfc-006](../rfc/rfc-006-state-backend-stack.md) §3.

**A stack whose state is committed keeps its checkpoint as a tracked
file, a run of it starts from a working copy that holds the forge's
current `main`, and a run that wrote the checkpoint leaves it for the
operator to land like any change.** The run leaves the file changed in
the primary checkout's `@`, since that is the checkout holding
`.credentials/`; the operator describes the change and pushes it as a
pull request, and [dispatch.md](dispatch.md) §2, rule 5 is what keeps
that `@` across a fetch.

**The publication is the push.** This repository is public, so a branch
carrying a checkpoint is public from the moment it reaches the forge,
before any review. What stands between a run and the public is what the
driver checks on the workstation, around every command it runs against
such a stack:

1.  **Before the command, a working copy behind the forge's `main` is
    refused.** The driver reads the forge's `main` with
    `git ls-remote origin refs/heads/main`, which writes no ref, and runs
    `git merge-base --is-ancestor` of it against the working copy: `jj`'s
    `@`, or git's `HEAD` in a plain clone. An answer of 1, the forge's
    `main` fetched but `@` not on it, names the rebase; any other, the
    commit never fetched, names `jj git fetch` first. `jj` is asked with
    the snapshot every `jj` command takes, so a working copy another
    workspace made stale — its `@` rebased from there, while the files on
    disk are still the older ones `pulumi` would read — is refused by
    `jj` itself.
2.  **Then a conflicted checkpoint is refused**, naming the reconcile:
    keep `main`'s side (`jj restore --from main <path>`), run a refreshed
    `plan`, and bring in through the program's `import_` what the other
    run created, or delete it by hand. Two runs from one base meet as a
    conflict in the file because each rewrites the manifest's timestamp.
    The ancestry comes first because of this remedy: after a checkpoint
    lands, the fetch that abandons the landed change can leave a second,
    unlanded run's checkpoint conflicted until the rebase resolves it,
    and the conflict's remedy would throw that run away.
3.  **A command that changed no part of the deployment leaves the file's
    bytes as they were.** Every write moves the manifest's timestamp and
    encrypts every secret again under a fresh nonce, and the engine does
    not keep the order of resources that register concurrently, so a run
    that changed nothing would otherwise still be a change and a pull
    request. The deployment is read from
    `pulumi stack export --show-secrets` before and after, compared
    without `manifest.time` and with the resources in one canonical
    order, and where it is the one the command started from the previous
    bytes are put back. That export carries each secret's envelope
    beside its plaintext, so a property that became secret is a change
    and keeps its new bytes.
4.  **After the command, the stack has no file under
    `checkpoints/.pulumi/stacks/` but its checkpoint and that one's
    `.bak`.** Any other file named for it is its state in a form the
    checks do not read — compressed, or a retained copy — and each is
    named.
5.  **Then two checks on the file**, each naming the property it found
    and never the value, one line per place:
    *   **No secret value is in the clear.** The stack's configuration
        secrets and its state's secrets, read in the clear before the
        command and after it, appear nowhere in the file outside a
        ciphertext envelope, whole or line by line for a multi-line value
        such as a key. Before as well as after, because a value whose
        only marking the command removed — a provider release that stops
        marking a field, a `stack import` of an edited export — is plain
        after it. A line of PEM armor, and any string shorter than eight
        characters, is not searched for on its own: neither identifies a
        secret. **The search is literal**: a value encoded before it was
        written — in base64, as OCI's `user_data` is, in hex, or escaped
        inside a string that is itself a JSON document — is not found.
    *   **Every property the engine marks secret is ciphertext**, which
        needs no value to check. The rules are the engine's own at the
        pinned CLI. A key a resource's recorded options name in
        `additionalSecretOutputs` is ciphertext as an output, which the
        engine wraps whole, and as an input too, which the engine leaves
        as the program passed it
        ([`step_executor.go` at 3.257.0](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/resource/deploy/step_executor.go#L512-L565)).
        An output whose input of the same name holds ciphertext anywhere
        in it is ciphertext; where both are objects the rule is read
        again one level down
        ([`annotateSecrets` at 3.257.0](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/resource/plugin/provider_plugin.go#L900-L929)).
        The engine applies that second rule only for a provider that does
        not accept secrets, as the dynamic provider does not, and the file
        does not record which provider did, so every resource is held to
        it. Either input rule is cleared the same way: the program passes
        that input whole as a secret (`pulumi.Output.secret`), which the
        next write records as one envelope, and the engine then wraps the
        output of that name too. The refusal names that input. Ciphertext
        is one envelope, or a null, which holds nothing, since what the
        engine marks it wraps whole. A structure carrying envelopes inside
        it where one belongs is refused whether the engine's marking was
        lost at its top or a provider that accepts secrets marked only
        inside it. An envelope holding its `plaintext` rather than a
        `ciphertext` is refused wherever it is: that is the form
        `pulumi stack export --show-secrets` writes, and the engine loads
        it from a checkpoint as readily as the encrypted one. Every
        resource the file records is read so, and the resource of every
        operation it holds pending, which the engine writes before the
        operation starts and a killed run leaves behind
        ([`snapshot.go` at 3.257.0](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/backend/snapshot.go#L426-L436)).
        A place both rules find is named once.

**Every command that can write is recorded as unchecked before it
starts**, in `checkpoints/<stack>.failed-check`, and the record comes off
only when the checks pass. A failed check fails the run and replaces the
record with what was found; a run stopped between the write and the
checks — a ^C, a query that fails — leaves it standing, and `plan` names
a record that stands. The file then holds the value in the working copy
and in `jj`'s local snapshots, and nowhere public. The fix is a program
change that marks the property secret, or the stray file removed, and
`operator-stack <stack> up`: while the record stands, `up` runs even
with nothing planned, since marking an output or an input secret on a
resource already in the state rewrites it as ciphertext at the next
write, and it checks the file again. The fix is squashed into the change that carries
the leak, so no commit that reaches the forge holds it.

**Every import into a stack whose state is committed goes through the
program**, with the `import_` option on the resource it declares, which
carries the program's secret markings into the imported state. A
`pulumi import` runs no program: it records no secret output names and
takes the provider's read as the inputs, so what it imports would carry
its secrets in the clear past both checks. A `pulumi stack import`
writes a whole deployment the same way, with no program at all. The
driver refuses either, passed through, for such a stack.

**A value that belongs to one invocation reaches the program as an
environment variable the driver sets on the process it starts**, and is read
where it is acted on — never stack configuration, which is committed and
would hand the value to every later run. The permission a destructive step
needs is such a value. The `state-backend` stack's is
`KLUSTER_STATE_BACKEND_REPLACE`, which the hooks on its instance read
(`kluster.lib.state_backend.permission`, rfc-006 §4.3): without it, a step
that would create, replace or delete the box fails at its hook with the box
untouched, however the run was started. The driver sets it on the one
`pulumi up` that `up --force` or `up --replace` starts, and takes it out
of every other `pulumi` run of the stack, so a caller's shell that
exported it grants nothing, to a passed-through `up` least of all.

## 4. CRD Types Handling

Custom resources are written against generated Python types, so
declaring one gets the same type checking and completion as declaring a
built-in resource. The `update_crds` console script
(`src/kluster/scripts/update_crds/`) renders the pinned chart set to
collect the CRD schemas without touching a cluster and hands them to
`crd2pulumi`, which writes the bindings into `packages/crds`; running
it is the only supported way to change anything under that directory.
The generated package is excluded from the type-annotation standard the
handwritten code holds to — it is not ours to annotate.

**The bindings record the pins they were generated from.** The script
reads the chart and manifest pins out of the `versions:` block (§3.2),
checks each chart's floor against the `appVersion` the chart itself
declares, so no operator version is kept by hand beside a pin, and
renders the definitions. Beside the bindings it writes
`packages/crds/rendered-from.json`: every pin it read, as the file holds
it — a chart that renders definitions, the chart whose version is the
ref of the Cilium source tree the script reads Cilium's definitions
from, a chart carrying a floor, and every manifest. A test holds that
record to the block. Renovate moves those pins and cannot run the
script, so a bump of one is red in `checks` until someone runs
`update_crds` on the branch, as a bump of the provider SDK below is; a
bump of a chart the script reads nothing from leaves the record as it is
and needs nothing more. A run with `--from-bundle` writes no record,
since the bindings it generates were not rendered from the pins.

**The Kubernetes provider SDK is pinned exactly, and a bump of the pin
is finished only by a regeneration.** `pyproject.toml` holds
`pulumi-kubernetes==<v>` where every other dependency holds a floor,
because the generated package is one provider version's artifact: the
bindings are generated against a version and register each resource at
it, so a program installing any other version asks the engine for two
`kubernetes` plugins and gets two default providers. Exact rather than a
floor because a floor lets `lockFileMaintenance` move the resolved
version in `uv.lock` alone — a change no regeneration follows and that
a test would then have to read out of the lock. The generated package
declares the version it was generated against as a floor of its own,
which `update_crds` writes: `crd2pulumi` bakes the version its release
was built with into that line, and `--version` moves the package
version and `pulumi-plugin.json` without touching it. A test holds the
three — the pin, the generated floor, `pulumi-plugin.json` — equal, so
a pin moved alone is red naming `update_crds`. Renovate proposes the
bump apart from the python dependencies group (`renovate.json5`) for
the same reason: grouped, a bump waiting on a regeneration would hold
every other library bump red with it.

## 5. Talking to a System With No Provider

Every system here that Pulumi has no provider for is driven by code of
this repository's own, one package per system under
`src/kluster/providers/`: today the desired-state files on the gateway
device, the Talos image factory's artifacts, the rewrites on an AdGuard
instance, the objects the state-backend appliance's image is imported
from, and the appliance's TLS handshake waited for. Every one of them is
a **dynamic provider**, and this section is what that costs and how one
is written. *Which* of them
should be one is a design decision, argued where each is designed
([cluster/architecture.md](../cluster/architecture.md) §5.2,
[rfc-002](../rfc/rfc-002-src-layout-and-the-gateway.md) §7.2–7.3).

### 5.1 The four options, and what separates them

-   **An existing provider**, native or bridged through Terraform.
    Always first, and the answer whenever one exists.
-   **The Command provider** (`local.Command`, `remote.Command`,
    `remote.CopyToRemote`) — for running a command as part of
    provisioning rather than modeling a resource with a lifecycle. It
    does implement `diff` and `read`, but only over its own inputs and
    state: `diff` compares the declared command and its triggers, and
    `read` hands back the state it already holds. **Neither ever looks
    at the target.** So "make the system match this content" is not
    something the resource can mean, and a change made on the target is
    invisible.
-   **A dynamic provider** — the answer when no provider covers the
    resource, the logic is specific to a single program, and nothing
    outside it will ever consume the code. Its limitations are §5.2.
-   **A full provider**, native or bridged. Gains `import`, `read`,
    cross-language use, ordinary provider inheritance and no
    per-resource blob in state, and costs a second language and a
    release pipeline. The trigger to pay that is a second consumer:
    another repository, or a second instance of the system.

Drift detection is usually what decides between the middle two. A
dynamic provider's `diff` may open a session and compare the target's
bytes with the declared ones, so an edit made on the target appears in
`pulumi preview` without a refresh; the Command provider cannot express
that at all.

### 5.2 What a dynamic provider is, mechanically

Pulumi documents dynamic-provider serialization for JavaScript only, so
the Python semantics below were established by experiment against
Pulumi 3.257.0 and `dill` 0.4.1, in a throwaway project on a file
backend. §5.3 is what was measured.

**A native provider has a resource; a dynamic one does not.** A
`kubernetes.Provider` *is* a resource: it appears in state with its
configuration as properties, and repointing it is a diff on a named
object. `pulumi.dynamic.ResourceProvider` is a plain class, so there is
nothing for `opts.provider` to point at — provider options are matched
by the package half of the type token, and no provider resource can be
`pulumi-python`. A dynamic provider therefore does not inherit down a
component tree the way every other one does: it travels to its
resources as an ordinary Python object, pickled into a reserved
property on **each** resource it manages, `__provider`, marked secret;
here every resource's constructor builds a fresh instance of its
class's provider. Three more limits come with the choice:
Python and TypeScript only; `pulumi import` and `get` unavailable; and
the package half of the type token always `pulumi-python`, so a policy
pack cannot tell one dynamic resource kind from another by package (the
`module`/`name` halves are the program's, chosen as
[style/pulumi.md](../style/pulumi.md) says —
`pulumi-python:dynamic/device_files:DeviceFile`).

**So a provider here carries no connection state**, and that is a
design rather than an accident of the mechanism: instance attributes
*are* serialized, in the clear inside the secret property, so a
credential set on the provider in the program would be copied onto
every resource and rotating it would rewrite all of them. Attributes
are left unset and `__getstate__` returns an empty bag instead, so what
lands in state is 55 bytes naming a module and a class: inert,
identical on every resource, and unchanged by a rotation. The values a
session needs are read in **`configure`**, which runs inside the
resource-provider process, once per process, before any operation, and
receives the stack's configuration with **secrets already decrypted**.
What follows from that is a rule this repository holds itself to
rather than one the runtime imposes — the resource-provider process
inherits the environment as well, so the mechanism forbids no second
store. The rule: a credential that only opens the provider's own
session lives in stack configuration — the store rule under "Layering"
in [style/pulumi.md](../style/pulumi.md) — and nowhere else: not in the
environment, not on a resource, not in a pickle, not in any component's
signature. The value is read in `configure`, out of the process's own
configuration and by no program, and that is what keeps it out of the
pickle. Rotating it is an edit to configuration.

What `configure` may *not* do is decide anything the caller decides. A
provider is generic code for a class of system and imports no
`conventions` (AGENTS.md's layering contract), so the address it dials
and the host key it pins are **declared resource inputs** like any
other — visible in a preview, which for a pinned public key is where a
reviewer checks it. The credential is the one value that goes the other
way.

**A provider makes its own consequences visible in `check`.** With an
inert pickle, nothing else would be. `check` is the one hook that runs
before every diff: it receives the resource's inputs and returns the
inputs the engine stores and compares, so a provider may **add**
properties there that no caller declared. Two are worth adding:

-   a **session** property — the endpoint plus a short digest of the
    credential — so that a rotation or an address move renders as
    `~ session: "host-1#9d6fb67570c1" => "host-1#bab3d6bf12a7"` rather
    than as nothing at all;
-   a **provider version** — a constant in the provider module, bumped
    by hand when its behavior changes. Not ceremony: a class imported
    from a module is pickled **by reference**, so editing the body of
    `create` changes not one byte of state, produces no diff, and
    leaves every resource's outputs stale.

Four ways to get this wrong, each measured:

-   **`diff`'s two bags are not symmetrical.** Its `olds` is the stored
    **output** bag and its `news` is the **checked input** bag, so a
    provider that compares them wholesale sees every create-time output
    as a difference and reports a change on every single run. The
    comparison is over an explicit list of keys — the declared inputs
    plus whatever `check` injects — and every provider here names that
    list rather than iterating a bag.
-   **`check` does not run on refresh.** A refresh calls `configure`,
    `read` and `diff` only, so what it compares is what is already in
    state.
-   **An injected property lands in state in the clear.** A property
    the provider synthesizes carries no secret marking however secret
    the configuration behind it. For a truncated digest that is the
    intended outcome — it is not the credential, and a redacted value
    would make the diff illegible — but it is a declassification, and
    it belongs where it is a line of code and a comment rather than an
    `unsecret` call in the program.
-   **The injected properties change without the target changing.** So
    `update` must distinguish them: when every declared input is equal
    and only a stamp moved, the update re-stamps the resource and
    touches the target not at all. Getting this wrong rewrites every
    file on the far side on every credential rotation, which is the
    opposite of what the mechanism is for.

One thing to keep enabled: `pulumi:disable-default-providers` lists the
packages a program builds providers for rather than saying `*`, because
dynamic resources depend on the `pulumi-python` default provider and
`*` would disable the one default provider such a program still needs.

### 5.3 The measurements

Run against Pulumi 3.257.0 with `dill` 0.4.1, in a throwaway project on
a file backend.

-   **E1 — what lands in `__provider`, and what changes it.** A class
    imported from a module is pickled by reference: 42 bytes naming the
    module and the class. Instance attributes *are* serialized, in the
    clear inside the secret property; class attributes are not. A class
    defined in the entrypoint module is pickled **by value** — 856
    bytes carrying its code objects and source path — so where the
    class lives decides which rule applies. Editing a method body of a
    module-level provider is no change, no diff and no update, and
    stale outputs stay; changing an instance attribute is an update,
    rendered as `~ __provider: [secret] => [secret]`; moving the class
    to another module changes it, the module name being part of the
    pickle.
-   **E2 — `configure` is real.** Called in the provider process, once
    per process, before the first operation. Its `req.config` keys
    carry the project as their namespace, and secrets arrive
    decrypted: the plugin unwraps them and tells the engine it does not
    accept secret values.
-   **E3 — a stateless provider works.** With attributes unset in the
    program and `__getstate__` returning `{}`, every operation ran
    correctly after deserialization, and `__provider` was 47–55 bytes
    and constant across a rotation.
-   **E4 — provider outputs become properties.** Values returned by
    `create` beyond the declared inputs appear as resource properties.
    They cannot by themselves carry a change into a preview, which
    compares against the checked inputs.
-   **E6 — an operation cannot reach another resource's state.** Each
    method receives the property bag of the resource being
    provisioned; there is no engine handle and no lookup call.
-   **E7 — what `check` and `diff` receive.** `check` gets the stored
    *input* bag as `olds` and the program's raw inputs as `news`.
    `diff` gets the stored *output* bag as `olds` and the *checked
    input* bag as `news`. Comparing every key reported a change on
    every run.
-   **E8 — `check` can add properties.** Properties added to the
    returned inputs are stored as inputs, reach `create`, and take part
    in the engine's comparison. `check` runs once per process before
    the first operation, in both preview and update.
-   **E9 — `update` returns properties.** Its outs replace the stored
    output bag, so a record of which session last wrote the resource
    stays current.
-   **E10 — the injected design works end to end.** A rotation with no
    program-side involvement renders
    `~ session: "host-1#9d6fb67570c1" => "host-1#bab3d6bf12a7"`; a
    version bump renders `~ provider_version: "1" => "2"`; an unchanged
    run reports `unchanged`; a refresh calls `configure`, `read` and
    `diff` but not `check`. The injected value is stored in plaintext.

`refresh` and `destroy` need no special flag on this version: both ran
plainly, and both called `configure` before `read` and `delete`.

## 6. Rendered Configuration

Another program's configuration language belongs in a file beside the
module that declares it rather than in a Python string literal
([style/python.md](../style/python.md)), and `lib/templates.py` is the
one mechanism that brings such a file back. It is one mechanism for the
repository, and it works on **directories** as well as single files,
because a directory is the shape the callers after the first ones need:
an application's configuration is a tree that becomes a config map or
the plaintext half of a sealed secret.

```python
def render_tree(package: str, directory: str, params: object | None = None) -> Mapping[str, str]: ...
def render(package: str, name: str, params: object | None = None) -> str: ...
def load(package: str, name: str) -> str: ...
```

`render_tree` walks one directory inside a package and returns
`{relative path: contents}`; `render` is the single-file case; `load`
is the file that is the artifact, read with nothing done to it.

**The `.j2` suffix decides, and the suffix is stripped from the key.** A
file named `Caddyfile.j2` is rendered with the parameters and lands
under `Caddyfile`; a file named `disk-tuning.xslt` is copied through
byte for byte under its own name. So a directory holding both kinds
takes one call and no globs, and a file that must keep literal
`{{ … }}` — a Grafana dashboard, a Go template some controller renders
later — is safe by construction rather than by the caller remembering
not to pass parameters.

Parameters are a frozen `dataclass`, which is what puts a template's
inputs in a signature instead of in a bag of names. Files are located
through `importlib.resources`, so a template resolves the same way from
a checkout and from an installed wheel, and templates live in a
`templates/` directory inside the component's own package so that a
component and its rendered files move together.

Jinja2 rather than `str.format` or `string.Template`, for three
reasons: it is already a dependency here; the files that need this have
loops and conditionals — a unit's argument list, the recovery script's
case arms, the flow rules' repeated destinations — and the alternatives
push those back into Python, which is the thing being avoided; and
`StrictUndefined` makes a parameter the caller forgot an error at
render time rather than a blank line in a configuration file. The
environment is fixed for the repository at `StrictUndefined`,
`keep_trailing_newline=True` and escaping off, since nothing rendered
here is HTML.
