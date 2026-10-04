"""The value paths a chart has, read off its `values.yaml`, its `values.schema.json` and its subcharts.

Helm ignores a value the chart does not have: a component that sets a key a
chart bump renamed or removed installs as before, says nothing, and behaves
differently. `update_crds` records for every chart pin the paths the chart has
at the pinned version (`record.CHART_VALUES_FILE_NAME`), and a test holds every
path a component hands its chart to that record.

A path is the chart's keys from the root, joined with `.`. The chart has a path
when its `values.yaml` sets it, when its schema declares it under
`properties`, or when a subchart has it, below the key the parent chart passes
that subchart's values under (its alias, or its name).

**A free-form map** is one the chart declares without fixing its keys, and every
path below it is accepted. Three shapes say so:

-   an empty `{}` in `values.yaml`, the usual spelling of a map of labels,
    annotations or selectors the user fills in;
-   a key whose `values.yaml` default is null, which leaves the value's shape to
    its user: the Intel device plugins operator ranges over `manager.devices`,
    whose default is nothing but commented-out examples;
-   a schema object carrying `additionalProperties` other than `false`, or
    `patternProperties`.
"""

# `ruamel.yaml` loads untyped documents, and so does `json`; this module walks
# what they load, a chart's values and schema, so it is glue over both.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML

#: The separator a path's keys are joined with in the record.
SEPARATOR = '.'


@dataclass(frozen=True)
class ValuePaths:
    """The paths one chart has, and the free-form maps among them."""

    paths: frozenset[str]
    free_form: frozenset[str]

    def accepts(self, path: Sequence[str]) -> bool:
        """Whether the chart has `path`, or a free-form map above it."""
        if SEPARATOR.join(path) in self.paths:
            return True
        return any(SEPARATOR.join(path[:depth]) in self.free_form for depth in range(len(path)))

    def entry(self, version: str) -> dict[str, object]:
        """The record's entry for the chart, read at `version`."""
        return {'version': version, 'paths': sorted(self.paths), 'free-form': sorted(self.free_form)}

    @classmethod
    def from_entry(cls, entry: Mapping[str, object]) -> ValuePaths:
        """The paths a record's entry holds."""
        paths = entry['paths']
        free_form = entry['free-form']
        assert isinstance(paths, list) and isinstance(free_form, list), f'not a record entry: {entry}'
        return cls(paths=frozenset(str(path) for path in paths), free_form=frozenset(str(path) for path in free_form))


def _yaml() -> YAML:
    return YAML(typ='safe')


def _from_values(values: Mapping[object, object], prefix: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], bool]]:
    """Each path `values` sets below `prefix`, and whether it is a free-form map."""
    for key, value in values.items():
        path = (*prefix, str(key))
        if isinstance(value, Mapping) and value:
            yield path, False
            yield from _from_values(value, path)
        else:
            yield path, value is None or (isinstance(value, Mapping) and not value)


def _resolve(node: object, root: Mapping[str, object]) -> object:
    """`node`, or what its local `$ref` points at within `root`."""
    if not isinstance(node, Mapping):
        return node
    reference = node.get('$ref')
    if not isinstance(reference, str) or not reference.startswith('#/'):
        return node
    target: object = root
    for part in reference.removeprefix('#/').split('/'):
        if not isinstance(target, Mapping):
            return {}
        target = target.get(part.replace('~1', '/').replace('~0', '~'), {})
    return target


def _from_schema(
    node: object, root: Mapping[str, object], prefix: tuple[str, ...], seen: frozenset[str]
) -> Iterator[tuple[tuple[str, ...], bool]]:
    """Each path a schema declares below `prefix`, and whether it is a free-form map.

    A local `$ref` is followed, and one already followed on the way down is
    not followed again, so a recursive definition ends. The branches of
    `allOf`, `anyOf` and `oneOf` each contribute their properties at the same
    path, since a value the chart accepts may take any of them.
    """
    if not isinstance(node, Mapping):
        return
    reference = node.get('$ref')
    if isinstance(reference, str):
        if reference in seen:
            return
        seen = seen | {reference}
    resolved = _resolve(node, root)
    if not isinstance(resolved, Mapping):
        return
    if resolved.get('additionalProperties') not in (None, False) or resolved.get('patternProperties'):
        yield prefix, True
    properties = resolved.get('properties')
    if isinstance(properties, Mapping):
        for key, value in properties.items():
            path = (*prefix, str(key))
            yield path, False
            yield from _from_schema(value, root, path, seen)
    for combinator in ('allOf', 'anyOf', 'oneOf'):
        branches = resolved.get(combinator)
        if isinstance(branches, list):
            for branch in branches:
                yield from _from_schema(branch, root, prefix, seen)


def _subcharts(directory: Path) -> Iterator[tuple[str, Path]]:
    """Each subchart a chart carries, with the key the chart passes its values under."""
    metadata = _yaml().load((directory / 'Chart.yaml').read_text())
    dependencies = metadata.get('dependencies') if isinstance(metadata, dict) else None
    aliases: dict[str, str] = {}
    for dependency in dependencies if isinstance(dependencies, list) else []:
        if isinstance(dependency, dict) and isinstance(dependency.get('name'), str):
            name = str(dependency['name'])
            alias = dependency.get('alias')
            aliases[name] = alias if isinstance(alias, str) else name
    charts = directory / 'charts'
    if not charts.is_dir():
        return
    for subchart in sorted(charts.iterdir()):
        if (subchart / 'Chart.yaml').is_file():
            yield aliases.get(subchart.name, subchart.name), subchart


def _read(directory: Path) -> dict[tuple[str, ...], bool]:
    """Each path the unpacked chart at `directory` has, and whether it is a free-form map."""
    found: dict[tuple[str, ...], bool] = {}

    def add(entries: Iterator[tuple[tuple[str, ...], bool]]) -> None:
        for path, free_form in entries:
            found[path] = found.get(path, False) or free_form

    values = _yaml().load((directory / 'values.yaml').read_text()) if (directory / 'values.yaml').is_file() else None
    if isinstance(values, Mapping):
        add(_from_values(values, ()))
    schema_file = directory / 'values.schema.json'
    if schema_file.is_file():
        schema = json.loads(schema_file.read_text())
        if isinstance(schema, dict):
            add(_from_schema(schema, schema, (), frozenset()))
    for key, subchart in _subcharts(directory):
        add(iter([((key,), False)]))
        add(((key, *path), free_form) for path, free_form in _read(subchart).items())
    return found


def read_chart(directory: Path) -> ValuePaths:
    """The paths the unpacked chart at `directory` has.

    A schema whose root takes `additionalProperties` makes the root free-form,
    recorded as the empty path, and every path is then accepted.
    """
    found = _read(directory)
    return ValuePaths(
        paths=frozenset(SEPARATOR.join(path) for path in found if path),
        free_form=frozenset(SEPARATOR.join(path) for path, free_form in found.items() if free_form),
    )
