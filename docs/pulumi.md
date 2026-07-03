# Pulumi Python Framework Design

Objective: Design and document the Python framework for Pulumi that reduces
boilerplate, handles async operations gracefully, and avoids "callback hell"
when dealing with Outputs.

## 1. Core Concepts

### 1.1 Declarative Native Async Sub-Resources

The framework provides an ergonomic, type-safe, and 100% native `async/await`
solution that allows synchronous sub-resource construction alongside
asynchronous parameter preparation, while strictly preserving Pulumi's core
safety guarantees (the DAG and dry-run preview capabilities).

All sub-resources are declared statically on the class using annotations. They
are instantiated **synchronously** in the `Component` constructor, so they all
exist as fully initialized Python objects before any setup runs. Setup methods
then simply access outputs directly via standard object-oriented Python, and
`resolve()` automatically captures all dynamic dependencies (both internal and
external constructor-passed ones) at runtime.

#### Pattern for Component:

```python
import asyncio
import pulumi
from typing import TypedDict
from putils.component import Component, setup, resolve

class SubnetArgs(TypedDict):
    network_id: str
    cidr: str

class MyComponent(Component):
    # 1. Statically declare sub-resources with strong typing
    vpc: gcp.compute.Network
    subnet: gcp.compute.Subnetwork

    # 2. Async setup for vpc (no dependencies)
    @setup("vpc", keys=["auto_create_subnetworks"])
    async def prepare_vpc(self) -> dict:
        await asyncio.sleep(0.01)  # Standard async call
        return {
            "auto_create_subnetworks": False,
        }

    # 3. Async setup for subnet, accessing vpc directly using standard OOP style!
    @setup("subnet", protect=True, depends_on=["vpc"])
    async def prepare_subnet(self) -> SubnetArgs:
        # Natively await outputs
        vpc_id, = await resolve(self.vpc.id)

        # Perform standard Python transformations on the resolved value
        transformed_id = f"subnet-for-{vpc_id.upper()}"

        return {
            "network_id": transformed_id,
            "cidr": "10.0.1.0/24",
        }
```

### 1.2 Fine-Grained Preview Diffs using Async Generators

When standard setup returns a dictionary, any abort during dry-run (e.g.
awaiting an unknown value from an upstream dependency) causes the entire
resource inputs to resolve to `UNKNOWN` sentinels. To preserve fine-grained
property diffs, setup methods can be written as **Async Generators** that
`yield` known parameters before calling `resolve()`.

```python
    @setup("subnet", keys=["network_id", "cidr"], depends_on=["vpc"])
    async def prepare_subnet(self):
        # Yield known values first to preserve fine-grained diffs during preview!
        yield {
            "cidr": "10.0.1.0/24",
        }

        # Awaiting here will raise UnknownValueException in preview if vpc.id is unknown
        vpc_id, = await resolve(self.vpc.id)

        yield {
            "network_id": f"subnet-for-{vpc_id.upper()}",
        }
```

### 1.3 Automatic Parent and Option Propagation

To avoid passing `opts=ResourceOptions(parent=self)` to every child resource in
a component, the `Component` base class automatically constructs all declared
sub-resources with `parent=self` and propagates any specified resource options
(e.g., `protect` and static `depends_on` declared in the `@setup` decorator).

## 2. Integration with `putils`

The framework is implemented in the proof-of-concept library `src/putils`:

-   `component.py`: Provides the base `Component` class, `@setup` decorator, and
    `resolve` helper.
-   `paio.py`: Handles bridging `asyncio` with Pulumi.

## 3. Layering & Stack Structure

To balance DRY principles and stack isolation, we propose a layered stack
structure. This allows changing one layer without necessarily affecting others.

### Proposed Layers

1.  **`infra-homelab`**:

    -   Manages Homelab specific infrastructure.
    -   Resources: Libvirt VM (Talos CP), UniFi port forwarding.
    -   Outputs: Homelab VM IP, Control Plane URN.

2.  **`infra-cloud`**:

    -   Manages Cloud specific infrastructure.
    -   Resources: GCP Compute Engine instance (Talos Worker), Firewall rules.
    -   Outputs: GCP Worker IP.

3.  **`k8s-base`**:

    -   Manages the base Kubernetes installation and core networking.
    -   Prerequisites: `infra-homelab` and `infra-cloud` stacks must be
        deployed.
    -   Resources: Talos machine configurations, Cilium CNI, Cilium Gateway API,
        BGP peering policies.
    -   Dependencies: Uses stack references to get IPs and URNs from physical
        layers.

4.  **`applications`**:

    -   Manages services running inside the cluster.
    -   Resources: Helm charts, deployments, services for apps like `hath`,
        `authelia`, `jellyfin`, etc.
    -   Dependencies: Uses stack reference to `k8s-base` for cluster connection
        details if needed, or directly uses the generated kubeconfig.

### 3.1 Cross-Stack Output Reuse

To reuse outputs from one stack in another without hardcoding, we use Pulumi's
`StackReference`. This ensures we don't rely on hardcoded information and
maintains clean separation.

**Example**:

```python
import pulumi

# Read outputs from the infra-homelab stack
homelab_stack = pulumi.StackReference("myorg/infra-homelab/dev")
vm_ip = homelab_stack.get_output("vm_ip")

# Use vm_ip in this stack to configure resources
```

## 4. CRD Types Handling

For custom resources (like Cilium's CRDs), we will continue to use the
`src/kluster/scripts/update_crds.py` script to generate Python types using
`crd2pulumi`. This ensures type safety and autocomplete when using CRDs in our
Pulumi code. The framework will assume these generated types are available in
the Python path.
