# Pulumi Python Framework Design

Objective: Design a Python framework for Pulumi that reduces boilerplate, handles async operations gracefully, and avoids "callback hell" when dealing with Outputs.

## 1. Core Concepts

### 1.1 Async Generators for Resource Definition

To avoid chaining `.apply()` calls or dealing with complex async setup in components, we propose using Python generators. A generator function defines the stack or component, yielding resource definitions or awaitables. A framework driver handles execution.

This pattern is especially useful inside `ComponentResource` setup to provide a clean experience.

**Pattern for Stack**:
```python
def my_stack():
    # Yield an async operation (e.g., fetching data, reading files)
    data = yield read_file_async("config.json")
    
    # Yield a resource definition
    vpc = yield (Resource, "my-vpc", {"cidr": "10.0.0.0/16", "data": data})
    
    # Use the resource (and its outputs) in the next step
    subnet = yield (Resource, "my-subnet", {"vpc_id": vpc.id, "cidr": "10.0.1.0/24"})
```

**Pattern for Component**:
```python
class MyComponent(Component):
    def setup(self, name, opts):
        # setup can be a generator too!
        data = yield read_file_async("config.json")
        vpc = yield (Resource, "my-vpc", {"cidr": "10.0.0.0/16", "data": data})
        
        # The framework driver will handle resolving these yields
```

**Driver**: A driver function iterates over the generator. If a yielded value is an awaitable, it awaits it. If it's a resource definition (e.g., a tuple of Class, name, and args), it creates the resource and sends the result back to the generator.

### 1.2 Context Managers for Parent Propagation

To avoid passing `opts=ResourceOptions(parent=self)` to every child resource in a component, we propose using a context manager and a custom resource constructor wrapper that uses context variables. This works seamlessly inside `ComponentResource` setup.

**Pattern**:
```python
class MyComponent(Component):
    def setup(self, name, opts):
        # Set this component as the parent for all resources created within the block
        with parent(self):
            vpc = Resource("vpc", cidr="10.0.0.0/16")
            subnet = Resource("subnet", vpc_id=vpc.id, cidr="10.0.1.0/24")
```

**Implementation**: A `ContextVar` stores the current parent. A wrapper around resource constructors checks this variable and applies it to `ResourceOptions` if not explicitly set.

## 2. Integration with `putils`

The existing proof-of-concept library in `src/putils` already provides a base for this framework:
-   `paio.py`: Handles bridging `asyncio` with Pulumi.
-   `component.py`: Provides a `Component` class that reduces boilerplate for `ComponentResource` and supports async `setup`.

We will enhance this library to include the generator driver and context manager patterns described above, ensuring they work well within the `Component` class.

## 3. Layering & Stack Structure

To balance DRY principles and stack isolation, we propose a layered stack structure. This allows changing one layer without necessarily affecting others.

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
    -   Prerequisites: `infra-homelab` and `infra-cloud` stacks must be deployed.
    -   Resources: Talos machine configurations, Cilium CNI, Cilium Gateway API, BGP peering policies.
    -   Dependencies: Uses stack references to get IPs and URNs from physical layers.

4.  **`applications`**:
    -   Manages services running inside the cluster.
    -   Resources: Helm charts, deployments, services for apps like `hath`, `authelia`, `jellyfin`, etc.
    -   Dependencies: Uses stack reference to `k8s-base` for cluster connection details if needed, or directly uses the generated kubeconfig.

### 3.5 Cross-Stack Output Reuse

To reuse outputs from one stack in another without hardcoding, we use Pulumi's `StackReference`. This ensures we don't rely on hardcoded information and maintains clean separation.

**Example**:
```python
import pulumi

# Read outputs from the infra-homelab stack
homelab_stack = pulumi.StackReference("myorg/infra-homelab/dev")
vm_ip = homelab_stack.get_output("vm_ip")

# Use vm_ip in this stack to configure resources
```

## 4. CRD Types Handling

For custom resources (like Cilium's CRDs), we will continue to use the `src/kluster/scripts/update_crds.py` script to generate Python types using `crd2pulumi`. This ensures type safety and autocomplete when using CRDs in our Pulumi code. The framework will assume these generated types are available in the Python path.
