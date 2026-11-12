"""The value paths a chart has, read off its `values.yaml`, its schema, its subcharts and the definitions it ships.

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
    `patternProperties`, or the `x-kubernetes-preserve-unknown-fields` a
    custom resource definition marks an open object with.

**A spec the chart passes through** is a map the chart renders whole as the
`spec` of one of its own custom resources, which the VictoriaMetrics stack
does for each component it installs (`vmsingle.spec` becomes the `VMSingle`'s
spec). Its `values.yaml` sets a few defaults there and nothing fixes the rest,
while a key below it is not ignored but becomes a field of the resource, so
the paths below it are the definition's own: read from the
`openAPIV3Schema` of the definition the chart ships in a `crds/` directory,
its own or a subchart's, at the version the chart renders. At and below the
map the definition replaces the chart's own verdict on what is free-form, so a
`{}` the chart leaves there does not open it. Which maps pass through is
declared rather than guessed (`SPEC_PASS_THROUGH`), for the chart a pin names
and not its subcharts, and a declared map whose definition the chart does not
ship stops the read.
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
class CustomResourceSpec:
    """The spec of one version of a custom resource: what a pass-through map of values becomes."""

    group: str
    version: str
    kind: str


#: Each map a chart passes through whole as a custom resource's spec, by the
#: chart's name, which is its `versions:chart-<name>` pin's, and the map's path.
#: Read in each chart's templates at its pinned version: a bump that stops
#: passing one through is a change to this table.
SPEC_PASS_THROUGH: Mapping[str, Mapping[tuple[str, ...], CustomResourceSpec]] = {
    # `templates/_helpers.tpl` at 0.91.2: each component's `spec` is merged
    # over the chart's computed defaults and rendered as its resource's spec.
    'victoria-metrics-k8s-stack': {
        ('vmsingle', 'spec'): CustomResourceSpec('operator.victoriametrics.com', 'v1beta1', 'VMSingle'),
        ('vmagent', 'spec'): CustomResourceSpec('operator.victoriametrics.com', 'v1beta1', 'VMAgent'),
        ('vmalert', 'spec'): CustomResourceSpec('operator.victoriametrics.com', 'v1beta1', 'VMAlert'),
        ('alertmanager', 'spec'): CustomResourceSpec('operator.victoriametrics.com', 'v1beta1', 'VMAlertmanager'),
    },
}


class MissingDefinition(LookupError):
    """A map a chart is declared to pass through names a definition the chart does not ship."""


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
    if (
        resolved.get('additionalProperties') not in (None, False)
        or resolved.get('patternProperties')
        or resolved.get('x-kubernetes-preserve-unknown-fields') is True
    ):
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


def _definitions(directory: Path) -> Iterator[Mapping[str, object]]:
    """Each custom resource definition the unpacked chart ships in a `crds/` directory, its subcharts' included.

    The files there are plain YAML Helm installs untemplated, so they read as
    the cluster receives them.
    """
    for path in sorted(directory.rglob('crds/*')):
        if path.suffix not in ('.yaml', '.yml') or not (path.parent.parent / 'Chart.yaml').is_file():
            continue
        for document in _yaml().load_all(path.read_text()):
            if isinstance(document, Mapping) and document.get('kind') == 'CustomResourceDefinition':
                yield document


def _spec_schema(directory: Path, spec: CustomResourceSpec) -> Mapping[str, object]:
    """The `openAPIV3Schema` of `spec`'s `spec` field, out of the definitions the chart ships."""
    for definition in _definitions(directory):
        body = definition.get('spec')
        if not isinstance(body, Mapping) or body.get('group') != spec.group:
            continue
        names = body.get('names')
        if not isinstance(names, Mapping) or names.get('kind') != spec.kind:
            continue
        for version in body.get('versions') or []:
            if isinstance(version, Mapping) and version.get('name') == spec.version:
                schema = version.get('schema', {}).get('openAPIV3Schema', {})
                found = schema.get('properties', {}).get('spec') if isinstance(schema, Mapping) else None
                if isinstance(found, Mapping):
                    return found
    raise MissingDefinition(
        f'{directory.name} ships no definition of {spec.kind}.{spec.group} {spec.version} with a spec'
    )


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


def _pass_through(directory: Path, found: dict[tuple[str, ...], bool]) -> None:
    """Hold each map the chart at `directory` passes through to the definition of the resource it becomes.

    The definition replaces the chart's own reading at and below the map:
    whether a path there is free-form is the definition's to say, so a `{}`
    or a null default the chart's `values.yaml` holds there opens nothing. The
    keys `values.yaml` sets there stay paths, since the chart renders them.
    Matched at the root of the chart the pin names, not in its subcharts.
    """
    metadata = _yaml().load((directory / 'Chart.yaml').read_text())
    name = metadata.get('name') if isinstance(metadata, Mapping) else None
    for root, spec in SPEC_PASS_THROUGH.get(str(name), {}).items():
        schema = _spec_schema(directory, spec)
        for path in found:
            if path[: len(root)] == root:
                found[path] = False
        for path, free_form in _from_schema(schema, schema, root, frozenset()):
            found[path] = found.get(path, False) or free_form


def read_chart(directory: Path) -> ValuePaths:
    """The paths the unpacked chart at `directory` has.

    A schema whose root takes `additionalProperties` makes the root free-form,
    recorded as the empty path, and every path is then accepted.
    """
    found = _read(directory)
    _pass_through(directory, found)
    return ValuePaths(
        paths=frozenset(SEPARATOR.join(path) for path in found if path),
        free_form=frozenset(SEPARATOR.join(path) for path, free_form in found.items() if free_form),
    )
