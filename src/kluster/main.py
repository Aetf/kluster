"""The Pulumi program: dispatch to the selected stack.

Registered by `__main__.py` through `pulumi.run`, so the async entrypoint runs
on Pulumi's own event loop and `putils.resolve` works throughout.
"""

from __future__ import annotations

from putils import install_parent_backstop

from .stacks import run_selected


async def main() -> None:
    # Before anything is declared, and here rather than in `putils` itself: a
    # stack transformation hangs off the root stack resource, which exists only
    # inside a running program, and a resource carries only the transformations
    # that existed when its parent was built (rfc-002 §8.2).
    install_parent_backstop()
    await run_selected()
