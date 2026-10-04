"""The record of the pins the CRD bundle in `packages/crds` was rendered from.

Renovate moves a chart or manifest pin in `Pulumi.yaml`, and cannot run
`update_crds`, so a bump arrives with the bundle and the SDK generated from it
still describing the release before it. The record is what makes that visible:
`update_crds` writes beside the bundle every pin it read, as the file held it,
and a test holds the record to the block. A bump of a pin the script reads is
therefore red in `checks` until someone runs `update_crds` on the branch, and a
bump of one it reads nothing else from leaves this record as it is.

The pins the script reads are the chart pins it renders definitions from, the
chart pins whose version names a source tree's ref, the chart pins carrying a
floor it checks, and every manifest pin.

Beside the pin record, in a file of its own, the script records the operator
version each chart it reads declares, its `Chart.yaml`'s `appVersion`, at the
version the pin names. That is a fact of the chart rather than of the block,
so it cannot live in the pin record, which a test rebuilds from the block
alone; it is what a pin elsewhere that has to agree with a chart's operator is
held to -- `kubeseal` in `mise.toml`, which seals for the sealed-secrets
controller the chart installs.

In a third file, the value paths every chart pin's chart has at the pinned
version (`values`). Helm ignores a value a chart does not have, so a component
handing its chart a key the chart dropped installs as before and says nothing;
a test holds every path a component sets to this record. It covers every chart
pin rather than the ones the script reads for definitions, since a component
can install a chart that has none, and each entry carries the version it was
read at, so any chart bump leaves it visibly stale.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from kluster.lib.versions import CHART, MANIFEST, NAMESPACE, ProjectFile, Versions
from kluster.scripts.update_crds import pins
from kluster.scripts.update_crds.values import ValuePaths

#: Beside the bundle, in the directory the `packages:` entry names it in.
FILE_NAME = 'rendered-from.json'

#: Beside the pin record: the operator version each chart it reads declares.
APP_VERSIONS_FILE_NAME = 'app-versions.json'

#: Beside the pin record: the value paths each chart pin's chart has.
CHART_VALUES_FILE_NAME = 'chart-values.json'


def read_charts(project: ProjectFile) -> list[str]:
    """The `<name>` of every chart pin the script reads, in the file's order."""
    versions = Versions(project)
    trees = {tree.chart for tree in pins.SOURCE_TREES}
    read: list[str] = []
    for name in project.names(CHART):
        pin = versions.chart[name]
        if pin.definitions or pin.floor is not None or name in trees:
            read.append(name)
    return read


def record(project: ProjectFile) -> dict[str, object]:
    """Every pin the script reads, keyed as the file keys it, with its value as the file holds it.

    Each value is parsed on the way in, so a record is only ever written of a
    pin the program would accept.
    """
    versions = Versions(project)
    entries: dict[str, object] = {}
    for name in read_charts(project):
        entries[f'{NAMESPACE}:{CHART}-{name}'] = project.structured(f'{CHART}-{name}')
    for name in project.names(MANIFEST):
        _ = versions.manifest[name]
        entries[f'{NAMESPACE}:{MANIFEST}-{name}'] = project.structured(f'{MANIFEST}-{name}')
    return entries


def app_versions(project: ProjectFile, declared: Mapping[str, str]) -> dict[str, object]:
    """Each chart the script reads, keyed as the pin record keys it, with the `appVersion` it declares.

    `declared` is chart name to `appVersion`, as the run read it off each
    chart's `Chart.yaml`. Each entry carries the chart version it was read at,
    so a test can hold the file to the block's versions and a bump leaves it
    visibly stale, as it does the pin record.
    """
    versions = Versions(project)
    return {
        f'{NAMESPACE}:{CHART}-{name}': {'version': versions.chart[name].version, 'appVersion': declared[name]}
        for name in read_charts(project)
    }


def chart_values(project: ProjectFile, read: Mapping[str, ValuePaths]) -> dict[str, object]:
    """Every chart pin, keyed as the pin record keys it, with the value paths its chart has.

    `read` is chart name to the paths the run read off that chart. Each entry
    carries the chart version it was read at, as `app_versions` does.
    """
    versions = Versions(project)
    return {
        f'{NAMESPACE}:{CHART}-{name}': read[name].entry(versions.chart[name].version) for name in project.names(CHART)
    }


def render(entries: dict[str, object]) -> str:
    """The record's text: sorted, indented JSON, so a diff of it reads pin by pin."""
    return json.dumps(entries, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def write(project: ProjectFile, directory: Path) -> Path:
    """Write the record into `directory`, the bundle's."""
    path = directory / FILE_NAME
    _ = path.write_text(render(record(project)), encoding='utf-8')
    return path


def write_app_versions(project: ProjectFile, declared: Mapping[str, str], directory: Path) -> Path:
    """Write the operator versions the charts declare into `directory`, beside the pin record."""
    path = directory / APP_VERSIONS_FILE_NAME
    _ = path.write_text(render(app_versions(project, declared)), encoding='utf-8')
    return path


def write_chart_values(project: ProjectFile, read: Mapping[str, ValuePaths], directory: Path) -> Path:
    """Write the value paths every chart pin's chart has into `directory`, beside the pin record."""
    path = directory / CHART_VALUES_FILE_NAME
    _ = path.write_text(render(chart_values(project, read)), encoding='utf-8')
    return path
