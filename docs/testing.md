# Unit Testing Pulumi Code

Objective: Verify infrastructure logic without creating real cloud resources using Pulumi mocks and `pytest`.

## 1. Setup

The project uses `pytest` and `pytest-asyncio` for unit testing.

> [!WARNING]
> There might be issues resolving dependencies (e.g., `pulumi-kubernetes`) due to private registry authentication issues in this environment. If `uv run pytest` fails to resolve dependencies, you may need to configure your registry credentials or run it in an environment with proper access.

To run the tests:
```bash
uv run pytest
```

## 2. Writing Tests

Tests should use `pulumi.runtime.set_mocks` to intercept resource creation and calls.

### 2.1 Example Test

Create a file named `test_*.py` (e.g., `test_network.py`) in a `tests` directory.

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

## 3. Best Practices

1.  **Mock Early**: Always set mocks before importing or executing any Pulumi code that creates resources.
2.  **Test Outputs**: Use `.apply()` or `pulumi.Output.all()` to check values of outputs, as they resolve asynchronously.
3.  **Keep it Fast**: Unit tests should not make network calls or create real resources.
