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
belongs there, and a returned mapping becomes stack outputs:

```python
async def main() -> pulumi.Inputs | None:
    ami = await fetch_talos_ami()      # plain asyncio, no outputs involved
    cluster = Cluster('kluster', ami=ami)
    return {'endpoint': cluster.endpoint}
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

## 3. Layering & Stack Structure

> **Status**: decided 2026-08-22 (interactive review). Three stacks in one
> project, one environment. The earlier 4-layer proposal
> (infra-homelab / infra-cloud / k8s-base / applications) is superseded:
> splitting the physical layer by site bought nothing (same change
> cadence, mutual references), while the apps/base split earned its keep
> — see the frequency argument below.

### 3.1 The three stacks

| Stack | Contents | Change cadence |
| --- | --- | --- |
| `physical` | OCI: VCN, 3× A1 nodes, NLB, reserved IPs, security lists, block volumes; libvirt: Talos worker VM + adopted HAOS domain; gw-config provider (FRR/BGP, nspawn estate); UniFi firewall rules; Talos machine configs + bootstrap (kubeconfig is an output); B2 buckets + keys; DNS zone + NLB anchor records | low (~monthly: Talos upgrades, firewall tweaks) |
| `k8s-base` | Everything cluster-scoped speaking the k8s API: Cilium (LB pools, BGP peering, Gateway API, gateways), VolSync, cert-manager, CNPG operator, sealed-secrets controller, VictoriaMetrics + grafana | medium (renovate chart bumps) |
| `apps` | Every application component: workloads, their namespaces, PVCs, HTTPRoutes/Services, SealedSecrets, **and their DNS records** (declared next to the app) | high (the daily driver; ~80–90% of all ups touch only this stack) |

Boundary rules:

-   **The only hard boundary is "exists before the k8s API does"** —
    that is `physical` vs the rest. The base/apps split is a blast-radius
    and preview-hygiene boundary: a Cilium upgrade can never ride along
    inside an app deploy, and app previews don't load the operator
    machinery.
-   **Conventions are code, not stack outputs.** Gateway names, pool
    labels, storage-class names live in a shared `conventions.py` —
    made possible by giving cross-stack-referenced singletons explicit
    names (autonaming disabled; see cluster-infra.md §0). Resources
    that deliberately keep autonaming expose their generated names as
    stack outputs — dynamic names are machine facts. StackReferences
    otherwise carry only physical's machine facts: kubeconfig, node
    IPs, NLB IP, bucket names.
-   **Namespaces belong to apps**: each app component creates its own
    namespace (legacy habit preserved); `k8s-base` owns only shared,
    cluster-scoped infrastructure.
-   **Cross-stack resource dependency (apps need base's CRDs/operators)
    is not expressible in Pulumi** — deploy order is CI's job
    ([ci.md](ci.md)).

### 3.2 Cross-Stack Output Reuse

`StackReference` for the physical machine facts only:

```python
import pulumi

physical = pulumi.StackReference("organization/kluster/physical")
kubeconfig = physical.get_output("kubeconfig")
```

## 4. CRD Types Handling

For custom resources (like Cilium's CRDs), we will continue to use the
`src/kluster/scripts/update_crds.py` script to generate Python types using
`crd2pulumi`. This ensures type safety and autocomplete when using CRDs in our
Pulumi code. The framework will assume these generated types are available in
the Python path.
