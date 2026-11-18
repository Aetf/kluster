"""The `Control planes` dashboard: every reading, on the history store, beside any earlier period (Aetf/kluster-ops#790 §5).

**A row per resource, a panel per reading**, in the order `readings.READINGS`
lists them, each panel computing the reading's expression over the history
store, which keeps the expression's inputs. So a panel draws whatever the
history holds of a period, series that arrived after their minute included:
OCI's, which the hourly pull imports, and every series imported into a
period already past. And the two periods a panel compares are computed under
one definition, today's. A reading whose inputs the history does not keep is
drawn from its recorded series instead (`readings.Reading.inputs_kept`). A
reading measured per node is shown for the control planes. A reading with a
budget draws it as a line at its limit, so a breach reads at a glance.

**Every panel draws a second period on the same axes**: the same query a
stretch of time earlier, by the dashboard's variable, which takes any
duration, so the operator compares two periods by eye at one scale. The
query is evaluated as of that earlier time, a subquery `offset` by the
variable at the panel's own step, since an expression takes no `offset` of
its own.

**The component declares it as a `ConfigMap`** in Grafana's namespace,
labeled as the stack's dashboard sidecar looks for (`SIDECAR_LABEL`), which
watches the namespace it runs in and hands Grafana each dashboard it finds
(rfc-007 §7.1).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from kluster import conventions
from kluster.conventions.backup import duration
from kluster.lib.monitoring.readings import READINGS, Bound, Reading, Resource

__all__ = (
    'CONFIGMAP',
    'DEFAULT_OFFSET',
    'FILE',
    'OFFSET_VARIABLE',
    'SIDECAR_LABEL',
    'TITLE',
    'UID',
    'earlier',
    'model',
    'query',
)

#: The dashboard's title and its identifier in Grafana.
TITLE = 'Control planes'
UID = 'control-planes'

#: The `ConfigMap` holding it, and its file there.
CONFIGMAP = 'dashboard-control-planes'
FILE = 'control-planes.json'

#: The label, and its value, the stack's dashboard sidecar collects a
#: `ConfigMap` by (`values.yaml`, `grafana.sidecar.dashboards`, L2124-2125 at
#: stack chart 0.91.2).
SIDECAR_LABEL = ('grafana_dashboard', '1')

#: The variable a panel's second period is offset by, and its first value.
OFFSET_VARIABLE = 'offset'
DEFAULT_OFFSET = dt.timedelta(weeks=1)

#: Each row's title.
_ROWS = {
    Resource.ETCD: 'etcd',
    Resource.CPU: 'CPU',
    Resource.MEMORY: 'Memory',
    Resource.DISK: 'Disk',
    Resource.NETWORK: 'Network',
    Resource.LOAD_CLASSES: 'Load classes',
    Resource.API_SERVER: 'API server',
}

#: A panel's size on Grafana's grid, which is 24 wide: two panels a line.
_WIDTH = 12
_HEIGHT = 8


#: The control planes as a vector of one series each, labeled with its `node`,
#: which a reading per node is joined with to keep the control planes' series.
_CONTROL_PLANES = (
    '(' + ' or '.join(f'label_replace(vector(1), "node", "{node}", "", "")' for node in conventions.CLOUD_NODES) + ')'
)


def query(reading: Reading) -> str:
    """The panel's query of the reading: its expression, or its record where the history keeps none of its inputs.

    Of the control planes, where the reading is per node.
    """
    computed = reading.expr if reading.inputs_kept else reading.record
    return f'({computed}) and on (node) {_CONTROL_PLANES}' if 'node' in reading.per else computed


def earlier(reading: Reading) -> str:
    """The panel's query as of `$offset` earlier, at the panel's own step."""
    return f'last_over_time(({query(reading)})[$__interval:$__interval] offset ${OFFSET_VARIABLE})'


def _legend(reading: Reading) -> str:
    return ', '.join(f'{{{{{label}}}}}' for label in reading.per)


def _thresholds(reading: Reading) -> dict[str, Any]:
    """The reading's budget as Grafana's thresholds, drawn as a line: red past the limit, green inside it."""
    if reading.budget is None:
        return {'mode': 'absolute', 'steps': [{'color': 'green', 'value': None}]}
    inside, outside = 'green', 'red'
    below, above = (inside, outside) if reading.budget.bound is Bound.AT_MOST else (outside, inside)
    return {
        'mode': 'absolute',
        'steps': [{'color': below, 'value': None}, {'color': above, 'value': reading.budget.limit}],
    }


def _panel(reading: Reading, panel_id: int, x: int, y: int, datasource: dict[str, str]) -> dict[str, Any]:
    legend = _legend(reading)
    return {
        'id': panel_id,
        'type': 'timeseries',
        'title': reading.title,
        'description': ' '.join(it for it in (f'`{reading.record}`.', reading.note) if it),
        'datasource': datasource,
        'gridPos': {'h': _HEIGHT, 'w': _WIDTH, 'x': x, 'y': y},
        'fieldConfig': {
            'defaults': {
                'unit': reading.unit,
                'thresholds': _thresholds(reading),
                'custom': {'thresholdsStyle': {'mode': 'line' if reading.budget is not None else 'off'}},
            },
            'overrides': [
                {
                    'matcher': {'id': 'byFrameRefID', 'options': 'B'},
                    'properties': [{'id': 'custom.lineStyle', 'value': {'fill': 'dash', 'dash': [10, 10]}}],
                }
            ],
        },
        'targets': [
            {'refId': 'A', 'datasource': datasource, 'expr': query(reading), 'legendFormat': legend},
            {
                'refId': 'B',
                'datasource': datasource,
                'expr': earlier(reading),
                'legendFormat': f'{legend}, ${OFFSET_VARIABLE} earlier',
            },
        ],
    }


def _rows(datasource: dict[str, str]) -> list[dict[str, Any]]:
    panels: list[dict[str, Any]] = []
    y = 0
    for resource in Resource:
        members: Sequence[Reading] = [reading for reading in READINGS if reading.resource is resource]
        panels.append(
            {
                'id': len(panels) + 1,
                'type': 'row',
                'title': _ROWS[resource],
                'collapsed': False,
                'gridPos': {'h': 1, 'w': 2 * _WIDTH, 'x': 0, 'y': y},
                'panels': [],
            }
        )
        y += 1
        for index, reading in enumerate(members):
            column = index % 2
            panels.append(_panel(reading, len(panels) + 1, column * _WIDTH, y, datasource))
            if column == 1 or index == len(members) - 1:
                y += _HEIGHT
    return panels


def model(datasource: str) -> dict[str, Any]:
    """The dashboard, every panel reading the datasource whose identifier is `datasource`."""
    source = {'type': 'prometheus', 'uid': datasource}
    offset = duration(DEFAULT_OFFSET)
    return {
        'uid': UID,
        'title': TITLE,
        'editable': True,
        'schemaVersion': 39,
        'time': {'from': 'now-24h', 'to': 'now'},
        'refresh': '1m',
        'templating': {
            'list': [
                {
                    'name': OFFSET_VARIABLE,
                    'label': 'Compare with the period this much earlier',
                    'type': 'textbox',
                    'query': offset,
                    'current': {'text': offset, 'value': offset},
                    'options': [{'selected': True, 'text': offset, 'value': offset}],
                    'hide': 0,
                }
            ]
        },
        'panels': _rows(source),
    }
