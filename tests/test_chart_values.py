"""Every value a component hands its chart is one the pinned chart has.

Helm ignores a value the chart does not have. A chart bump that renamed or
removed a key a component sets therefore installs as before, passes every
other check, and changes what runs. `update_crds` records the value paths
every chart pin's chart has at its pinned version
(`packages/crds/chart-values.json`, `kluster.scripts.update_crds.values`), and
this suite holds every path a component sets to that record. The program runs
whole under mocks (`k8s_base_installation`), so a component added later is
held here without being named.

A path below a map the chart declares free-form -- an empty `{}`, a null
default, or a schema object open to further properties -- is accepted below
that map, since the chart fixes no keys there.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest_asyncio
from k8s_base_installation import CHART, Run, applied

from kluster.lib.versions import CHART as CHART_PIN
from kluster.lib.versions import NAMESPACE
from kluster.scripts.update_crds import record, sources
from kluster.scripts.update_crds.values import SEPARATOR, ValuePaths

ROOT = Path(__file__).parent.parent


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The whole program against a `physical` that has published every output."""
    return await applied()


def chart_values_written() -> dict[str, dict[str, object]]:
    """`packages/crds/chart-values.json` as `update_crds` last wrote it."""
    path = sources.bundle_path(ROOT / 'Pulumi.yaml').parent / record.CHART_VALUES_FILE_NAME
    return cast('dict[str, dict[str, object]]', json.loads(path.read_text()))


def set_paths(values: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Each path a component's values set: a non-empty map is walked into, anything else is a value at its path."""
    for key, value in values.items():
        path = (*prefix, key)
        if isinstance(value, Mapping) and value:
            yield from set_paths(cast('Mapping[str, Any]', value), path)
        else:
            yield path


def pin_name(reference: str) -> str:
    """The `versions:chart-<name>` a chart registration was installed from, read off its `chart` input.

    An OCI chart is located as `<repository>/<name>@<digest>`, an HTTP chart as
    its name (`ChartPin.reference`).
    """
    return reference.split('@', 1)[0].rsplit('/', 1)[-1]


def installed(run: Run) -> list[tuple[str, str, Mapping[str, Any]]]:
    """Each chart the program installs: the component that installs it, its pin's name, and its values."""
    charts: list[tuple[str, str, Mapping[str, Any]]] = []
    for request in run.monitor.registrations.values():
        if request.type != CHART:
            continue
        inputs = run.inputs(CHART, request.name)
        component = run.monitor.registrations[request.parent].type if request.parent else '<the stack>'
        charts.append((f'{component} ({request.name})', pin_name(inputs['chart']), inputs.get('values') or {}))
    return charts


def test_every_value_a_component_sets_is_one_its_pinned_chart_has(applied: Run) -> None:
    """A key the pinned chart lacks is red, naming the component, the chart and the path.

    The record is read for the chart at the version the block pins
    (`test_update_crds.py` holds the record to those versions), so a bump
    that dropped a key a component sets is red once `update_crds` has
    rewritten the record on its branch.
    """
    written = chart_values_written()
    charts = installed(applied)
    # Not vacuous: the program installs charts, and hands values to some.
    assert charts
    assert any(values for _, _, values in charts)

    missing: list[str] = []
    for component, name, values in charts:
        entry = written.get(f'{NAMESPACE}:{CHART_PIN}-{name}')
        if entry is None:
            missing.append(f'{component} installs chart {name}, which the record does not hold; run update_crds')
            continue
        chart = ValuePaths.from_entry(entry)
        missing.extend(
            f'{component} sets {SEPARATOR.join(path)} on chart {name} {entry["version"]}, which has no such value'
            for path in set_paths(values)
            if not chart.accepts(path)
        )
    assert missing == []


def test_a_path_below_a_free_form_map_is_accepted_and_a_sibling_is_not() -> None:
    """Below a map the chart leaves open any key is accepted; beside it the keys are the chart's."""
    chart = ValuePaths(
        paths=frozenset({'podAnnotations', 'image', 'image.tag'}), free_form=frozenset({'podAnnotations'})
    )

    assert chart.accepts(('podAnnotations', 'reloader.stakater.com/auto'))
    assert chart.accepts(('image', 'tag'))
    assert not chart.accepts(('image', 'tags'))
    assert not chart.accepts(('podAnnotation', 'reloader.stakater.com/auto'))
