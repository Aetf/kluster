"""The held `PriorityClass` `k8s-base` declares, against what a held pod names and what names it.

The run is the whole `k8s-base` program, with every sealed value the stack
declares sealed in. A held pod's constraints name the class
(`kluster.lib.k8s.Constraints`), and the API server refuses a pod naming a
class it does not hold, so the class is read for the name a held pod
carries, and for what the scheduler does with a pod at it: preempts a free
pod, which runs at the default, and never a pod of the system's own classes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest_asyncio
from k8s_base_installation import Run, applied

from kluster import conventions
from kluster.lib.k8s import at_home

PRIORITY_CLASS = 'kubernetes:scheduling.k8s.io/v1:PriorityClass'

#: The lowest value of the classes the system reserves for its own critical
#: pods, `system-cluster-critical` among them (Kubernetes'
#: `scheduling.HighestUserDefinablePriority` is one billion; the system's
#: classes are two billion and above).
HIGHEST_USER_DEFINABLE_PRIORITY = 1_000_000_000

#: The priority of a pod that names no class, since no class is the global
#: default.
DEFAULT_PRIORITY = 0


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The stack as its file holds it: every sealed value it declares, sealed in."""
    return await applied()


def test_the_class_a_held_pod_names_is_the_one_the_stack_declares(applied: Run) -> None:
    """A held pod is refused by the API server unless the class it names exists."""
    named = at_home('a pod held at home').pod_fields_json()['priorityClassName']

    (declared,) = [it.inputs for it in applied.monitor.of_type(PRIORITY_CLASS)]

    assert declared['metadata']['name'] == named


def test_a_held_pod_preempts_a_free_one_and_no_pod_of_the_system(applied: Run) -> None:
    """Above a pod that names no class, below every class the system reserves, and allowed to preempt."""
    (declared,) = [it.inputs for it in applied.monitor.of_type(PRIORITY_CLASS)]

    assert DEFAULT_PRIORITY < declared['value'] <= HIGHEST_USER_DEFINABLE_PRIORITY
    assert declared['preemptionPolicy'] == 'PreemptLowerPriority'
    assert declared.get('globalDefault', False) is False


def _names_the_class(value: object, name: str) -> bool:
    """Whether a declaration's inputs name the class anywhere, as a pod spec or a chart's values carry it."""
    if isinstance(value, Mapping):
        fields = cast('Mapping[str, object]', value)
        return fields.get('priorityClassName') == name or any(_names_the_class(it, name) for it in fields.values())
    if isinstance(value, list):
        return any(_names_the_class(it, name) for it in cast('list[object]', value))
    return False


def _behind(run: Run, urn: str) -> set[str]:
    """Every registration `urn` waits for, through the dependencies of each it waits for in turn."""
    registrations = run.monitor.registrations
    reached: set[str] = set()
    frontier = list(registrations[urn].dependencies)
    while frontier:
        dependency = frontier.pop()
        if dependency in reached:
            continue
        reached.add(dependency)
        if dependency in registrations:
            frontier.extend(registrations[dependency].dependencies)
    return reached


def test_everything_that_names_the_class_is_installed_behind_it(applied: Run) -> None:
    """Whatever renders a pod at the class waits for it, whichever component declares it."""
    (declared,) = applied.monitor.of_type(PRIORITY_CLASS)
    held = applied.urn(PRIORITY_CLASS, declared.name)
    naming = [
        it
        for it in applied.monitor.declared
        if it is not declared and _names_the_class(it.inputs, conventions.HELD_PRIORITY_CLASS)
    ]

    assert naming
    assert [it.name for it in naming if held not in _behind(applied, applied.urn(it.typ, it.name))] == []
