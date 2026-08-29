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

### 1.2 Fine-Grained Preview Diffs

Known values are passed as plain arguments, so they always show up concretely
in `pulumi preview`. If an `async_output` coroutine awaits a value that is
unknown during preview, `resolve` raises `UnknownValueException` to abort that
coroutine early, and only that one input becomes `UNKNOWN` — sibling inputs of
the same resource are unaffected. No special protocol is needed.

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
which tracks dependencies and stays preview-safe.

#### What happens during `pulumi preview`

You don't need to do anything. If an awaited value is unknown (e.g. the VPC
does not exist yet), the coroutine is aborted and just that one input shows as
unknown in the diff; inputs passed plainly keep their concrete values. During
`pulumi up` every value is known and coroutines always run to completion.

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
    producer applies previews stale values.

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
