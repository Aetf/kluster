"""The four stacks of one project (docs/framework/pulumi.md §3).

`physical` exists before the Kubernetes API does; `dns` owns zones and the
estate records that belong to no app; `k8s-base` owns everything
cluster-scoped; `apps` owns the applications, their namespaces, and their DNS
records. Which one a program run declares is decided by the selected stack,
not by configuration — a run of `pulumi up -s apps` cannot touch a node.

Conventions travel as code (`kluster.conventions`); a StackReference carries
only machine facts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pulumi

from . import apps, dns, k8s_base, physical

__all__ = ('STACKS', 'run_selected')

#: Stack name → the program that declares it. The names are the Pulumi stack
#: names, so `pulumi stack select physical` is the whole dispatch mechanism.
STACKS: dict[str, Callable[[], Awaitable[None]]] = {
    'physical': physical.main,
    'dns': dns.main,
    'k8s-base': k8s_base.main,
    'apps': apps.main,
}


async def run_selected() -> None:
    """Declare the selected stack, or fail loudly on an unknown one."""
    name = pulumi.get_stack()
    program = STACKS.get(name)
    if program is None:
        raise ValueError(f'no program for stack {name!r}; expected one of {", ".join(sorted(STACKS))}')
    await program()
