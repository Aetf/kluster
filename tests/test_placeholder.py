from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio


class MyMocks(pulumi.runtime.Mocks):
    """Resources answer with their inputs; no provider is ever contacted."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        # MockResourceArgs.inputs is an untyped dict in the SDK.
        return args.name + '_id', cast('dict[str, Any]', args.inputs)

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        # The failures element must be iterable: the mock monitor builds
        # CheckFailures from it, and the stub's Optional is a lie at runtime.
        return {}, []


# Must be an async fixture: set_mocks needs the test's running event loop.
@pytest_asyncio.fixture(autouse=True)
async def setup_mocks() -> None:
    pulumi.runtime.set_mocks(
        MyMocks(),
        project='my-project',
        stack='dev',
        preview=True,
    )


@pytest.mark.asyncio
async def test_placeholder() -> None:
    assert True
