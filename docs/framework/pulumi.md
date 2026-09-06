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

Two consequences worth knowing:

-   **The scope is `super().__init__()` … `register_outputs()`**, which is why
    every component ends its constructor with the latter. A component is
    pushed onto the scope after its own registration, so a top-level component
    needs no parent of its own while a nested one does; a component that never
    calls `register_outputs` stays on the scope and the next unparented
    resource is refused in its name instead of its own.
-   **Nothing is exempt** — a provider built inside a component is a child of
    it like everything else. Resources declared outside any component, which is
    what a stack program does, pass untouched.

`kluster.main` installs it once, before any stack program runs; a resource
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
string `04da6b54-80e4-46f7-96ec-b56ff0331ba9`. `pulumi stack output` returns
it, and a `StackReference` reader receives it as a known value rather than as
an absence. So a targeted apply leaves every export that depends on a
resource the run skips creating poisoned until the rest of the stack is
applied, and nothing between the two runs may read one (§3.1).

## 2. Integration with `putils`

The framework is implemented in the library `src/putils` (stable; verified by
`tests/test_async_properties.py`):

-   `component.py`: Provides the base `Component` class (auto `pulumi_type`,
    `child_opts()`).
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

    physical = pulumi.StackReference('organization/kluster/physical')
    kubeconfig = physical.get_output('kubeconfig')
    ```

    This is the only route for a value no program can know before an
    apply — an identifier the cloud generates, an address it assigns, a
    credential a resource mints. The cost is that a reader sees
    whatever the producer published last, so a preview taken before the
    producer applies previews stale values. Staleness is the milder
    hazard: an output the producer last published from a targeted apply
    can be present, well-typed and meaningless, because Pulumi's unknown
    sentinel reaches a reader as an ordinary known string — the
    mechanism, and the rule that nothing may read such an output until
    the rest of the producer is applied, are §1.4's "When an awaited
    value is unknown".

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
five stacks agree on would otherwise be five copies drifting apart.
What it does have is **project-level configuration**: a `config:` block
in `Pulumi.yaml` whose values apply to every stack, which a stack's own
file overrides only where it deliberately differs. Two limits come with
it, neither of which bites for a pin: `pulumi config set` cannot write
there, so the values are hand-edited YAML; and a key in someone else's
namespace may carry a value but neither a type nor a default.

Every version pin the repository holds lives in that block, in one
`versions:` namespace with **the kind in the key** —
`versions:talos`, `versions:chart-<name>` and `versions:image-<name>`,
a container root filesystem being an image like any other. One
namespace because they are one kind of fact, a build somebody else
produced and this repository selects by version; the prefix because it
is what lets one renovate manager per kind match its own entries and
nothing else. `lib/versions.py` exposes one accessor per kind, each
returning a parsed value rather than the raw string and each refusing a
missing or malformed pin by naming the key, so a pin nobody set fails
where it is read instead of somewhere downstream.

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

## 5. Talking to a System With No Provider

Three systems here are driven by code of this repository's own: the
desired-state files on the gateway device, the Talos image factory's
artifacts, and the rewrites on an AdGuard instance
(`src/kluster/providers/`). All three are **dynamic providers**, and
this section is what that costs and how one is written. *Which* of them
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
resources as an ordinary Python object a component hands to its
children, and the instance is pickled into a
reserved property on **each** resource it manages, `__provider`, marked
secret. Three more limits come with the choice: Python and TypeScript
only; `pulumi import` and `get` unavailable; and the package half of
the type token always `pulumi-python`, so a policy pack cannot tell one
dynamic resource kind from another by package (the `module`/`name`
halves are the program's — `pulumi-python:dynamic/device:File`).

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
session lives in stack configuration — unless the credential's own
design puts it in the environment instead, which is the store rule in
[style/pulumi.md](../style/pulumi.md) — and nowhere else: not on a
resource, not in a pickle, not in any component's signature. **Only the
store moves.** Either way the value is read in `configure`, out of the
process's own configuration or its own environment and by no program,
and that is what keeps it out of the pickle. Rotating a configured one
is an edit to configuration.

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
