"""The record of the pins `packages/crds` was generated from.

Renovate moves a chart or manifest pin in `Pulumi.yaml`, and cannot run
`update_crds`, so a bump arrives with the bindings still describing the release
before it. The record is what makes that visible: `update_crds` writes into
`packages/crds` every pin it read, as the file held it, and a test holds the
record to the block. A bump of a pin the script reads is therefore red in
`checks` until someone runs `update_crds` on the branch, and a bump of one it
reads nothing from changes nothing here and needs nothing more.

The pins the script reads are the chart pins it renders definitions from, the
chart pins whose version names a source tree's ref, the chart pins carrying a
floor it checks, and every manifest pin.
"""

from __future__ import annotations

import json
from pathlib import Path

from kluster.lib.versions import CHART, MANIFEST, NAMESPACE, ProjectFile, Versions
from kluster.scripts.update_crds import pins

#: Beside the generated package's `pyproject.toml`, outside the module tree.
FILE_NAME = 'rendered-from.json'


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


def render(entries: dict[str, object]) -> str:
    """The record's text: sorted, indented JSON, so a diff of it reads pin by pin."""
    return json.dumps(entries, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def write(project: ProjectFile, package: Path) -> Path:
    """Write the record into the generated package at `package`."""
    path = package / FILE_NAME
    _ = path.write_text(render(record(project)), encoding='utf-8')
    return path
