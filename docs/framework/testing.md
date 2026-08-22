# Unit Testing Pulumi Code

Objective: Verify infrastructure logic without creating real cloud resources
using Pulumi mocks and `pytest`.

## 1. Setup

The project uses `pytest` and `pytest-asyncio` for unit testing.

To run the tests (always with a timeout — a coroutine that never resolves its
futures hangs forever instead of failing):

```bash
timeout 60 mise x uv -- uv run pytest
```

## 2. Writing Tests

Tests should use `pulumi.runtime.set_mocks` to intercept resource creation and
calls.

### 2.1 Example Test

Create a file named `test_*.py` (e.g., `test_network.py`) in a `tests`
directory.

```python
import pulumi
import pytest
import pytest_asyncio

# Define the Mocks
class MyMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        # Return a mock ID and the inputs as the resource state
        outputs = args.inputs
        return [args.name + "_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}

# Setup the test environment
# Must be an async fixture: set_mocks needs the test's running event loop.
@pytest_asyncio.fixture(autouse=True)
async def setup_mocks():
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
property depends on. The SDK's `MockMonitor` drops `propertyDependencies` from
the response, so downstream dependency assertions would always come back
empty. The test suite patches it once at module import — before any Pulumi
code runs (this is the actual pattern used in
`tests/test_async_properties.py`):

```python
import pulumi.runtime.mocks
from pulumi.runtime.proto import resource_pb2

original_register_resource = pulumi.runtime.mocks.MockMonitor.RegisterResource


def patched_register_resource(self, request):
    resp = original_register_resource(self, request)
    if isinstance(resp, resource_pb2.RegisterResourceResponse):
        for k, v in request.propertyDependencies.items():
            resp.propertyDependencies[k].urns.extend(v.urns)
    return resp


pulumi.runtime.mocks.MockMonitor.RegisterResource = patched_register_resource
```

### 3.2 Asserting Dependencies via URNs

When Pulumi tracks dynamic dependencies inside `async_output` coroutines, it
recreates dependency instances as `DependencyResource` synthetic resources.
**Do not use object identity (`is` or `==`)** to compare resource dependencies
returned by `Output.resources()`. Instead, extract and assert on their `urn`
strings. Note that `resolve` cannot be used in test code — it raises
`RuntimeError` outside an `async_output` coroutine (RFC-001 Rev 3); await
output futures directly:

```python
# Correct assertion pattern:
deps = await my_component.subnet.network_id.resources()
dep_urns = {await d.urn.future() for d in deps}
assert await vpc.urn.future() in dep_urns
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
2.  **Test Outputs**: Await `.future()` (or use `.apply()` /
    `pulumi.Output.all()`) to check values of outputs, as they resolve
    asynchronously; `resolve` is only usable inside `async_output` coroutines.
3.  **Always Use Timers**: When running unit tests, wrap the execution with a
    timeout (e.g. `timeout 15` in shell or using a test runner timeout) to
    prevent hanging tests when coroutines fail to resolve their futures.
4.  **Keep it Fast**: Unit tests should not make network calls or create real
    resources.
