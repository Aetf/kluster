"""The `github` stack: the forge itself — repositories, environments, gates.

Everything CI runs inside is configuration too, and until now it existed only
as console state: two repositories (`kluster`, `kluster-ops`), the per-stack
Environments that partition the credentials (ci.md §2), which of them a
reviewer gates, the branch protection that makes the zero-diff proof
load-bearing, and the two single-purpose GitHub Apps. None of it was written
down, so none of it could drift-check, review, or be rebuilt.

**Applied from the operator's machine, never from CI.** The credential this
stack needs can change branch protection and environment gates -- that is,
it can switch off the things that guard `main`. Handing it to a workflow
would mean anything that merges to `main` can also unguard `main`, which
undoes the partition ci.md §2 exists to create. The trade is cheap: the
forge changes a few times a year, while the credential would sit in CI
permanently. CI may still *preview* this stack to detect drift; it may not
apply it.

The Apps themselves are console-created (their private keys are §2 seeds,
credentials.md) -- what is declared here is their installation and the
repository state around them.
"""

from __future__ import annotations


async def main() -> None:
    raise NotImplementedError('github stack: see docs/framework/github.md')
