# Unit Testing Pulumi Code

Objective: Verify infrastructure logic without creating real cloud resources
using Pulumi mocks and `pytest`.

## 1. Setup

The project uses `pytest` and `pytest-asyncio` for unit testing.

> [!WARNING] There might be issues resolving dependencies (e.g.,
> `pulumi-kubernetes`) due to private registry authentication issues in this
> environment. If `uv run pytest` fails to resolve dependencies, you may need to
> configure your registry credentials or run it in an environment with proper
> access.

To run the tests:

```bash
uv run pytest
```

## 2. Writing Tests

Tests should use `pulumi.runtime.set_mocks` to intercept resource creation and
calls.

### 2.1 Example Test

Create a file named `test_*.py` (e.g., `test_network.py`) in a `tests`
directory.

```python
import asyncio
import pulumi
import pytest

# Define the Mocks
class MyMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        # Return a mock ID and the inputs as the resource state
        outputs = args.inputs
        return [args.name + "_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}

# Setup the test environment
@pytest.fixture(autouse=True)
def setup_mocks():
    pulumi.runtime.set_mocks(
        MyMocks(),
        project="my-project",
        stack="dev",
        preview=True,
    )

# Write the test
@pytest.mark.asyncio
async def test_my_component():
    # Import your Pulumi code here
    # from src.kluster.network import MyVpcComponent
    # component = MyVpcComponent("test-vpc")

    # For now, let's just assert something simple
    assert True
```

## 3. Testing Next-Gen Components (RFC-001)

When writing unit tests for components using the `putils.Component` base class
with `async_output`/`resolve` inputs:

### 3.1 Mocking `propertyDependencies`

During registration, the Pulumi engine needs to know which outputs a resource
property depends on. Your mock monitor's `new_resource` (or `RegisterResource`
override) must preserve and return `propertyDependencies` from the request to
the response, otherwise downstream dependency resolution will fail.

```python
class MyMockMonitor(pulumi.runtime.MockMonitor):
    def register_resource(self, req):
        # Register resource and obtain URN and ID
        urn = f"urn:pulumi:stack::project::type::{req.name}"
        id_ = f"{req.name}_id"
        # Crucial: Echo back propertyDependencies
        return pulumi.runtime.RegisterResourceResponse(
            urn=urn,
            id=id_,
            object=req.object,
            property_dependencies=req.propertyDependencies,
        )
```

### 3.2 Asserting Dependencies via URNs

When Pulumi tracks dynamic dependencies inside `async_output` coroutines, it
recreates dependency instances as `DependencyResource` synthetic resources.
**Do not use object identity (`is` or `==`)** to compare resource dependencies
returned by `Output.resources()`. Instead, extract and assert on their `urn`
strings:

```python
# Correct assertion pattern:
deps = await my_component.subnet.network_id.resources()
dep_urns = {await resolve(d.urn) for d in deps}
assert vpc.urn in dep_urns
```

### 3.3 Dry-Run/Preview Safety

In dry-run tests (where `preview=True`), unresolved output properties return the
`UNKNOWN` sentinel. Awaiting such outputs via `resolve` raises
`UnknownValueException`, which `async_output` converts into an unknown output.
If testing preview behavior, retrieve the output's future using
`.future(with_unknowns=True)` to inspect if it's an instance of `Unknown`:

```python
from pulumi.output import Unknown

val = await my_component.subnet.network_id.future(with_unknowns=True)
assert isinstance(val, Unknown)
```

## 4. Best Practices

1.  **Mock Early**: Always set mocks before importing or executing any Pulumi
    code that creates resources.
2.  **Test Outputs**: Use `.apply()`, `pulumi.Output.all()`, or `resolve` helper
    to check values of outputs, as they resolve asynchronously.
3.  **Always Use Timers**: When running unit tests, wrap the execution with a
    timeout (e.g. `timeout 15` in shell or using a test runner timeout) to
    prevent hanging tests when coroutines fail to resolve their futures.
4.  **Keep it Fast**: Unit tests should not make network calls or create real
    resources.
