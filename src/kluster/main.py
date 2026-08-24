"""The Pulumi program: dispatch to the selected stack.

Registered by `__main__.py` through `pulumi.run`, so the async entrypoint runs
on Pulumi's own event loop and `putils.resolve` works throughout.
"""

from __future__ import annotations

from .stacks import run_selected


async def main() -> None:
    await run_selected()
