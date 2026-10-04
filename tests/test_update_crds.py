"""The pins, the selection rule and the regeneration behind `packages/crds` and `sdks/crds`.

Nothing here reaches the network: what is worth holding still is which CRDs
survive the filter, that the committed bundle is the script's own output, that
the pins the script reads are the block's and the bundle was rendered from
them, that a chart below its floor is refused, that the operator version each chart
declares is recorded and `kubeseal` is the sealed-secrets one, that renovate's
managers read the block, that a tool download nothing vouches for is refused, and what the
script leaves behind when it regenerates the SDK. The cases that download or
run a tool at all are handed a stand-in. That the SDK was generated from the
bundle is `test_conventions`', beside the other generated SDKs.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import subprocess
import sys
import tarfile
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
import requests
from renovate_text import as_python_spells_it, as_renovate_spells_it, group, listed, package_rules, scalar

from kluster.lib.versions import CHART, MANIFEST, ChartPin, Floor, ProjectFile, Versions
from kluster.scripts.update_crds import cli, pins, record, sources
from kluster.scripts.update_crds.values import ValuePaths, read_chart

ROOT = Path(__file__).parent.parent


def project_config() -> dict[str, object]:
    """The `config:` block of the repository's own `Pulumi.yaml`, loaded as the script loads it."""
    return sources.read_config(ROOT / 'Pulumi.yaml')


def with_version(config: Mapping[str, object], key: str, version: str) -> dict[str, object]:
    """`config` with the pin at `key` moved to `version`, and nothing else changed."""
    pin = cast('dict[str, dict[str, object]]', config[key])
    return dict(config) | {key: {'value': pin['value'] | {'version': version}}}


CRD = """
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
status:
  acceptedNames:
    kind: Widget
spec:
  group: example.com
  names:
    kind: Widget
"""

DROPPED = """
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: acceleratorfunctions.fpga.intel.com
spec:
  group: fpga.intel.com
  names:
    kind: AcceleratorFunction
"""

DEPLOYMENT = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: controller
"""


def test_select_crds_keeps_only_custom_resource_definitions() -> None:
    selected = sources.select_crds([f'{DEPLOYMENT}---{CRD}'])

    assert [crd.name for crd in selected] == ['widgets.example.com']


def test_select_crds_drops_retired_groups() -> None:
    """`fpga.intel.com` rides along in the Intel operator chart and is retired."""
    selected = sources.select_crds([DROPPED, CRD])

    assert [crd.group for crd in selected] == ['example.com']


def test_select_crds_strips_the_cluster_written_status() -> None:
    (selected,) = sources.select_crds([CRD])

    assert 'status' not in selected.document


def test_select_crds_deduplicates_by_name() -> None:
    """Two sources may legitimately ship the same definition; the bundle carries it once."""
    selected = sources.select_crds([CRD, CRD])

    assert len(selected) == 1


def test_select_crds_orders_by_name() -> None:
    other = CRD.replace('widgets.example.com', 'anvils.example.com')

    selected = sources.select_crds([CRD, other])

    assert [crd.name for crd in selected] == ['anvils.example.com', 'widgets.example.com']


def test_select_crds_tolerates_empty_documents() -> None:
    """A rendered chart whose values disabled everything is a stream of nothing."""
    assert sources.select_crds(['---\n# Source: chart/templates/empty.yaml\n---\n']) == []


def test_select_crds_names_a_definition_that_carries_no_name() -> None:
    nameless = """
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
spec:
  group: example.com
"""

    with pytest.raises(sources.SourceError, match=re.escape('no metadata.name')):
        _ = sources.select_crds([nameless])


def test_select_crds_names_a_definition_that_carries_no_group() -> None:
    groupless = """
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
"""

    with pytest.raises(sources.SourceError, match=re.escape('widgets.example.com has no spec.group')):
        _ = sources.select_crds([groupless])


def test_yaml_file_urls_keeps_the_yaml_entries_of_a_contents_listing() -> None:
    listing = [
        {'name': 'widget.yaml', 'download_url': 'https://example.com/widget.yaml'},
        {'name': 'anvil.yml', 'download_url': 'https://example.com/anvil.yml'},
        {'name': 'README.md', 'download_url': 'https://example.com/README.md'},
    ]

    assert sources.yaml_file_urls(listing, what='example/repo@v1:crds') == [
        'https://example.com/widget.yaml',
        'https://example.com/anvil.yml',
    ]


def test_yaml_file_urls_names_the_directory_when_the_answer_is_not_a_listing() -> None:
    """A rate-limited contents call answers an object, and every entry lookup would then fail."""
    with pytest.raises(sources.SourceError, match='example/repo@v1:crds: the contents API answered a dict'):
        _ = sources.yaml_file_urls({'message': 'API rate limit exceeded'}, what='example/repo@v1:crds')


def test_yaml_file_urls_skips_the_entries_that_are_not_files() -> None:
    """A directory or a submodule is listed with `download_url: null`, legitimately."""
    listing = [
        {'name': 'subdir', 'type': 'dir', 'download_url': None},
        {'name': 'widget.yaml', 'type': 'file', 'download_url': 'https://example.com/widget.yaml'},
    ]

    assert sources.yaml_file_urls(listing, what='example/repo@v1:crds') == ['https://example.com/widget.yaml']


def test_yaml_file_urls_names_the_yaml_entry_that_has_no_download_url() -> None:
    """Demanded of the files that survive the filter, and of nothing else."""
    with pytest.raises(sources.SourceError, match=re.escape('the entry widget.yaml carries no download_url')):
        _ = sources.yaml_file_urls([{'name': 'widget.yaml', 'download_url': None}], what='example/repo@v1:crds')


def test_yaml_file_urls_names_the_directory_when_an_entry_has_no_name() -> None:
    with pytest.raises(sources.SourceError, match='example/repo@v1:crds: an entry carries no name'):
        _ = sources.yaml_file_urls([{'download_url': 'https://example.com/widget.yaml'}], what='example/repo@v1:crds')


@pytest.mark.parametrize(
    ('version', 'expected'),
    [
        ('1.20.1', (1, 20, 1)),
        ('v1.21.1', (1, 21, 1)),
        ('1.26', (1, 26)),
        ('1.30.0-rc1', (1, 30, 0)),
    ],
)
def test_version_tuple(version: str, expected: tuple[int, ...]) -> None:
    assert sources.version_tuple(version) == expected


# -- the pins the script reads ------------------------------------------------


def test_no_stack_file_overrides_a_pin_the_script_reads() -> None:
    """A stack's own file can override a project value, and the script reads only `Pulumi.yaml`.

    A chart or manifest pin overridden in `Pulumi.<stack>.yaml` would install
    one release while the bundle describes another, with every check green.
    """
    stack_files = sorted(ROOT.glob('Pulumi.*.yaml'))
    # Not vacuous: the stacks have files of their own.
    assert stack_files
    overriding = [
        f'{path.name}: {key}'
        for path in stack_files
        for key in sources.read_config(path, required=False)
        if key.startswith((f'versions:{CHART}-', f'versions:{MANIFEST}-'))
    ]
    assert overriding == []


def test_the_record_is_the_pins_the_bundle_was_rendered_from() -> None:
    """`packages/crds` records the pins `update_crds` read, and they are the block's.

    Renovate moves a pin and cannot run `update_crds`, so a bump arrives with
    the bundle and the SDK describing the release before it. This is what
    makes such a bump red until someone regenerates on its branch.
    """
    written = json.loads((sources.bundle_path(ROOT / 'Pulumi.yaml').parent / record.FILE_NAME).read_text())

    assert written == record.record(ProjectFile(project_config())), (
        'packages/crds was rendered from other pins than Pulumi.yaml holds; run `mise x -- uv run update_crds`'
    )


def app_versions_written() -> dict[str, dict[str, str]]:
    """`packages/crds/app-versions.json` as `update_crds` last wrote it."""
    path = sources.bundle_path(ROOT / 'Pulumi.yaml').parent / record.APP_VERSIONS_FILE_NAME
    return cast('dict[str, dict[str, str]]', json.loads(path.read_text()))


def test_the_operator_versions_are_of_the_charts_the_script_reads_at_the_pinned_versions() -> None:
    """Each chart the script reads, at the version the block pins, so a bump leaves the record visibly stale.

    The `appVersion` is a fact of the chart, which no test can read without
    the network, so what is held here is that the record was written for the
    pins the block holds -- the same charts the pin record names, each at its
    version. A bump of one is red until `update_crds` reads it again.
    """
    project = ProjectFile(project_config())
    versions = Versions(project)

    assert {key: entry['version'] for key, entry in app_versions_written().items()} == {
        f'versions:{CHART}-{name}': versions.chart[name].version for name in record.read_charts(project)
    }, 'packages/crds records operator versions of other chart pins than Pulumi.yaml holds; run update_crds'


def test_the_record_holds_the_operator_version_the_run_read_beside_the_version_it_read_it_at() -> None:
    project = ProjectFile(project_config())
    versions = Versions(project)
    declared = {name: f'{position}.0.0' for position, name in enumerate(record.read_charts(project))}

    assert record.app_versions(project, declared) == {
        f'versions:{CHART}-{name}': {'version': versions.chart[name].version, 'appVersion': declared[name]}
        for name in record.read_charts(project)
    }


def chart_values_written() -> dict[str, dict[str, object]]:
    """`packages/crds/chart-values.json` as `update_crds` last wrote it."""
    path = sources.bundle_path(ROOT / 'Pulumi.yaml').parent / record.CHART_VALUES_FILE_NAME
    return cast('dict[str, dict[str, object]]', json.loads(path.read_text()))


def test_the_value_paths_are_of_every_chart_pin_at_its_pinned_version() -> None:
    """Every chart pin, at the version the block pins, so any chart bump leaves the record visibly stale.

    The paths are facts of the charts, which no test can read without the
    network, so what is held here is that the record was written for the pins
    the block holds. Every chart pin rather than the ones read for definitions:
    a component can install a chart that has none (`test_chart_values.py`).
    """
    project = ProjectFile(project_config())
    versions = Versions(project)

    assert {key: entry['version'] for key, entry in chart_values_written().items()} == {
        f'versions:{CHART}-{name}': versions.chart[name].version for name in project.names(CHART)
    }, 'packages/crds records value paths of other chart pins than Pulumi.yaml holds; run update_crds'


def test_the_value_record_holds_what_the_run_read_beside_the_version_it_read_it_at() -> None:
    project = ProjectFile(project_config())
    versions = Versions(project)
    read = {
        name: ValuePaths(paths=frozenset({f'{name}.b', f'{name}.a'}), free_form=frozenset({f'{name}.a'}))
        for name in project.names(CHART)
    }

    assert record.chart_values(project, read) == {
        f'versions:{CHART}-{name}': {
            'version': versions.chart[name].version,
            'paths': [f'{name}.a', f'{name}.b'],
            'free-form': [f'{name}.a'],
        }
        for name in project.names(CHART)
    }
    cilium = cast('Mapping[str, object]', record.chart_values(project, read)[f'versions:{CHART}-cilium'])
    assert ValuePaths.from_entry(cilium) == read['cilium']


def chart(directory: Path, *, name: str, values: str, schema: object = None, dependencies: str = '') -> Path:
    """An unpacked chart: its `Chart.yaml`, its `values.yaml` and, when given, its schema."""
    directory.mkdir(parents=True)
    _ = (directory / 'Chart.yaml').write_text(f'apiVersion: v2\nname: {name}\nversion: 1.0.0\n{dependencies}')
    _ = (directory / 'values.yaml').write_text(values)
    if schema is not None:
        _ = (directory / 'values.schema.json').write_text(json.dumps(schema))
    return directory


def test_the_value_paths_of_a_chart_are_every_key_its_values_set(tmp_path: Path) -> None:
    """Each key from the root is a path; a list is a value at its path, not walked into."""
    directory = chart(
        tmp_path / 'c',
        name='c',
        values='image:\n  repository: example\n  tag: v1\nreplicas: 1\ntolerations:\n  - key: a\n',
    )

    read = read_chart(directory)

    assert read.paths == {'image', 'image.repository', 'image.tag', 'replicas', 'tolerations'}
    assert read.free_form == set()


def test_an_empty_map_and_a_null_default_are_free_form(tmp_path: Path) -> None:
    """A map the chart leaves for its user to fill, spelled `{}` or left without a default."""
    directory = chart(
        tmp_path / 'c',
        name='c',
        values='podAnnotations: {}\nmanager:\n  devices:\n    # gpu: true\n  image: x\n',
    )

    read = read_chart(directory)

    assert read.free_form == {'podAnnotations', 'manager.devices'}
    assert read.accepts(('manager', 'devices', 'gpu'))
    assert not read.accepts(('manager', 'device', 'gpu'))


def test_the_schema_adds_the_properties_it_declares_through_its_references(tmp_path: Path) -> None:
    """A path only the schema declares is one the chart has; `additionalProperties` makes a map free-form.

    cert-manager's schema reaches every property through `$ref` into `$defs`,
    so a local reference is followed, and a reference to itself ends.
    """
    schema = {
        '$ref': '#/$defs/root',
        '$defs': {
            'root': {
                'properties': {
                    'crds': {'$ref': '#/$defs/crds'},
                    'labels': {'type': 'object', 'additionalProperties': {'type': 'string'}},
                    'tree': {'$ref': '#/$defs/root'},
                },
                'allOf': [{'properties': {'webhook': {'type': 'object'}}}],
            },
            'crds': {'properties': {'enabled': {'type': 'boolean'}, 'keep': {'type': 'boolean'}}},
        },
    }
    directory = chart(tmp_path / 'c', name='c', values='crds:\n  enabled: false\n', schema=schema)

    read = read_chart(directory)

    assert read.paths == {'crds', 'crds.enabled', 'crds.keep', 'labels', 'tree', 'webhook'}
    assert read.free_form == {'labels'}


def test_a_subcharts_paths_are_below_the_key_the_parent_passes_its_values_under(tmp_path: Path) -> None:
    """A dependency's alias, or its name when it has none."""
    parent = chart(
        tmp_path / 'p',
        name='p',
        values='enabled: true\n',
        dependencies='dependencies:\n  - name: cluster\n    alias: monitoring\n  - name: plain\n',
    )
    _ = chart(parent / 'charts' / 'cluster', name='cluster', values='dashboard:\n  create: false\nlabels: {}\n')
    _ = chart(parent / 'charts' / 'plain', name='plain', values='size: 1\n')

    read = read_chart(parent)

    assert read.paths == {
        'enabled',
        'monitoring',
        'monitoring.dashboard',
        'monitoring.dashboard.create',
        'monitoring.labels',
        'plain',
        'plain.size',
    }
    assert read.free_form == {'monitoring.labels'}


def test_kubeseal_is_the_release_of_the_controller_the_sealed_secrets_chart_installs() -> None:
    """`credentials` seals with `mise.toml`'s `kubeseal`, for the controller the chart's pin installs.

    The controller's release is the chart's declared `appVersion`, which the
    record carries. A chart bump to a new controller is red once `update_crds`
    has rewritten the record on its branch, and a `kubeseal` bump at once;
    renovate moves the two in one pull request (the rule below).
    """
    import tomllib

    tools = tomllib.loads((ROOT / 'mise.toml').read_text())['tools']
    controller = app_versions_written()[f'versions:{CHART}-sealed-secrets']['appVersion']

    assert str(tools['kubeseal']) == controller.removeprefix('v')


def test_renovate_moves_kubeseal_with_the_sealed_secrets_chart() -> None:
    """The pair the test above holds equal has to move in one renovate pull request.

    The mise manager reads `mise.toml`, and the rule matching that manager puts
    every tool in the toolchain group; the rule taking `kubeseal` into the
    in-cluster group, where the chart's bump travels, has to come after that one
    -- the last match wins a field -- match that one dependency, spelled the way
    `mise.toml` keys it, and name the in-cluster group. The slug is the branch.
    Renovate reads its configuration from the default branch, so nothing goes
    red there when this breaks.
    """
    import tomllib

    rules = package_rules((ROOT / 'renovate.json5').read_text())
    (toolchain,) = [
        index
        for index, rule in enumerate(rules)
        if listed(rule, 'matchManagers') == ['mise'] and not listed(rule, 'matchDepNames')
    ]
    (kubeseal,) = [index for index, rule in enumerate(rules) if 'kubeseal' in listed(rule, 'matchDepNames')]
    rule = rules[kubeseal]

    assert toolchain < kubeseal
    keys = re.findall(r'^\s*(\w+):', rule, re.MULTILINE)
    assert [key for key in keys if key.startswith(('match', 'exclude'))] == ['matchDepNames']
    (tool,) = listed(rule, 'matchDepNames')
    assert tool in tomllib.loads((ROOT / 'mise.toml').read_text())['tools']
    assert len(group(rule)) == 2
    assert group(rule) == group(rules[in_cluster_rule(rules)])
    # No later rule that sets a group takes it back out.
    for later in rules[kubeseal + 1 :]:
        if group(later):
            assert tool not in listed(later, 'matchDepNames'), later
            assert 'mise' not in listed(later, 'matchManagers') or listed(later, 'matchDepNames'), later


def test_the_record_holds_what_the_script_reads_and_nothing_else() -> None:
    """A bump of a pin the script reads moves the record; a bump of one it reads nothing from does not.

    A chart that renders no definitions, has no floor and names no source tree
    is installed by the stack and nothing more, so its bump needs no
    regeneration and must not be held red waiting for one.
    """
    config = project_config()
    project = ProjectFile(config)
    versions = Versions(project)
    read = record.read_charts(project)
    unread = [name for name in project.names(CHART) if name not in read]
    trees = {tree.chart for tree in pins.SOURCE_TREES}
    # Not vacuous: the block holds charts of both kinds.
    assert read
    assert unread
    for name in project.names(CHART):
        pin = versions.chart[name]
        assert (name in read) == (pin.definitions or pin.floor is not None or name in trees), name

    before = record.record(project)
    for name in read:
        moved = with_version(config, f'versions:chart-{name}', '99.0.0')
        assert record.record(ProjectFile(moved)) != before, name
    for name in unread:
        moved = with_version(config, f'versions:chart-{name}', '99.0.0')
        assert record.record(ProjectFile(moved)) == before, name


def test_a_chart_the_script_reads_only_for_its_floor_is_recorded() -> None:
    """A floor is checked by the run, so a chart carrying one is read even when it renders nothing.

    Bumped, such a chart could fall below its floor with nothing checking it
    but the next run of `update_crds`, so its bump waits for one.
    """
    floored = {
        'versions:chart-floored': {
            'value': {
                'repository': 'https://charts.example.invalid/',
                'version': '1.0.0',
                'definitions': False,
                'floor': {'operator': '1.0', 'document': 'a section that states it'},
            }
        }
    }

    assert record.read_charts(ProjectFile(floored)) == ['floored']


def test_a_chart_the_script_reads_only_for_its_source_tree_is_recorded() -> None:
    """A chart whose version is a source tree's ref is read even when it renders nothing and has no floor.

    Cilium's carries a floor today, which would hide a record that forgot the
    tree: once the floor were dropped, a Cilium bump would leave the record
    as it was, and the bundle would keep describing the previous release.
    """
    (tree,) = pins.SOURCE_TREES
    tree_only = {
        f'versions:chart-{tree.chart}': {
            'value': {'repository': 'https://charts.example.invalid/', 'version': '1.0.0', 'definitions': False}
        }
    }

    assert record.read_charts(ProjectFile(tree_only)) == [tree.chart]


def test_the_cilium_source_tree_is_read_at_the_cilium_chart_version() -> None:
    """The chart installs no definitions, so the bundle would silently describe the wrong release.

    The tree's ref is derived from the chart pin rather than written beside it,
    so a chart bump moves the tree with it.
    """
    (tree,) = [tree for tree in pins.SOURCE_TREES if tree.repo == 'cilium/cilium']
    versions = Versions(ProjectFile(project_config()))
    chart = versions.chart[tree.chart]

    assert not chart.definitions
    assert tree.ref(chart) == f'v{chart.version}'
    assert (
        tree.ref(
            ChartPin(
                name=tree.chart, repository=chart.repository, version='9.9.9', digest=chart.digest, definitions=False
            )
        )
        == 'v9.9.9'
    )


FLOORED = ChartPin(
    name='cloudnative-pg',
    repository='https://charts.example.invalid/',
    version='0.29.0',
    digest=None,
    definitions=True,
    floor=Floor(operator='1.26', document='cluster-infra.md §1 item 5'),
)


@pytest.mark.parametrize('app_version', ['1.26', '1.26.0', '1.30.0', 'v1.26.1'])
def test_a_chart_whose_operator_clears_its_floor_passes(app_version: str) -> None:
    sources.check_floor(FLOORED, app_version)


@pytest.mark.parametrize('app_version', ['1.25.9', 'v1.20.1'])
def test_a_chart_whose_operator_is_below_its_floor_is_refused_by_name(app_version: str) -> None:
    """The floor is on the operator the chart declares, not on the chart's own version.

    `cloudnative-pg` 0.29.0 ships CNPG 1.30.0, so the version the check reads
    is the chart's `appVersion`, which the refusal names beside the document
    stating the floor.
    """
    with pytest.raises(sources.BelowFloor, match='versions:chart-cloudnative-pg') as refused:
        sources.check_floor(FLOORED, app_version)
    assert app_version in str(refused.value)
    assert 'cluster-infra.md §1 item 5' in str(refused.value)


def test_a_run_reads_every_source_from_the_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What a run fetches is what the block pins, end to end, with the network replaced.

    The operator version of every chart the script reads is read, and the
    floors checked against it, before anything is rendered, and is handed back
    by chart; the source tree is fetched at the ref its chart's pin names, the
    manifest through the digest-checked fetch, exactly the charts that render
    definitions are rendered, each from its pin, and the value paths of every
    chart pin are read, each from its pin, and handed back by chart.
    """
    config = with_version(project_config(), 'versions:chart-cilium', '9.9.9')
    project = ProjectFile(config)
    versions = Versions(project)
    calls: list[str] = []
    helm = tmp_path / 'helm'

    def app_version(_: Path, pin: ChartPin, *, workdir: Path) -> str:
        calls.append(f'app-version {pin.name}')
        return f'99.0.{len(calls)}'

    def tree(source: pins.SourceTree, ref: str) -> list[str]:
        calls.append(f'tree {source.repo}@{ref}')
        return []

    def manifest(pin: object) -> str:
        calls.append(f'manifest {pin}')
        return ''

    def render(_: Path, pin: ChartPin, *, workdir: Path) -> str:
        calls.append(f'render {pin.name} {pin.version}')
        return ''

    def value_paths(_: Path, pin: ChartPin, *, workdir: Path) -> ValuePaths:
        calls.append(f'values {pin.name} {pin.version}')
        return ValuePaths(paths=frozenset({pin.name}), free_form=frozenset())

    def fetch_helm(_: Path) -> Path:
        return helm

    monkeypatch.setattr(sources, 'fetch_helm', fetch_helm)
    monkeypatch.setattr(sources, 'chart_value_paths', value_paths)
    monkeypatch.setattr(sources, 'chart_app_version', app_version)
    monkeypatch.setattr(sources, 'fetch_source_tree', tree)
    monkeypatch.setattr(cli, 'fetch_manifest', manifest)
    monkeypatch.setattr(sources, 'render_chart', render)

    _, declared, read_values = cli.collect_documents(tmp_path, project)

    charts = [versions.chart[name] for name in project.names(CHART)]
    read = record.read_charts(project)
    assert calls[: len(read)] == [f'app-version {name}' for name in read]
    assert declared == {name: f'99.0.{position}' for position, name in enumerate(read, start=1)}
    assert 'tree cilium/cilium@v9.9.9' in calls
    assert [call for call in calls if call.startswith('manifest')] == [
        f'manifest {versions.manifest[name]}' for name in project.names(MANIFEST)
    ]
    assert [call for call in calls if call.startswith('render')] == [
        f'render {chart.name} {chart.version}' for chart in charts if chart.definitions
    ]
    assert [call for call in calls if call.startswith('values')] == [
        f'values {chart.name} {chart.version}' for chart in charts
    ]
    assert read_values == {
        chart.name: ValuePaths(paths=frozenset({chart.name}), free_form=frozenset()) for chart in charts
    }


def test_a_pinned_chart_is_fetched_from_where_its_pin_says() -> None:
    """An OCI chart by its digest-pinned reference, an HTTP chart by name within its repository."""
    versions = Versions(ProjectFile(project_config()))
    oci = versions.chart['cert-manager']
    http = versions.chart['volsync']
    assert oci.oci
    assert not http.oci

    assert sources._chart_location(oci) == [  # pyright: ignore[reportPrivateUsage] -- the seam under test
        f'oci://quay.io/jetstack/charts/cert-manager@{oci.digest}',
        '--version',
        oci.version,
    ]
    assert sources._chart_location(http) == [  # pyright: ignore[reportPrivateUsage] -- the seam under test
        'volsync',
        '--version',
        http.version,
        '--repo',
        http.repository,
    ]


def test_update_crds_starts_when_the_sdk_does_not_import() -> None:
    """The script regenerates `sdks/crds`, so it is what repairs an SDK that no longer imports.

    It therefore imports nothing of the SDK, directly or through `kluster.lib`:
    run with `pulumi_crds` made unimportable, `--help` still answers.
    """
    broken = (
        'import sys; sys.modules["pulumi_crds"] = None; '
        'from kluster.scripts.update_crds import main; sys.exit(main(["--help"]))'
    )

    run = subprocess.run([sys.executable, '-c', broken], capture_output=True, text=True, timeout=120, check=False)

    assert run.returncode == 0, run.stderr
    assert '--project' in run.stdout


def test_the_clis_logging_setup_leaves_a_logger_made_before_it_working() -> None:
    """A logger another module made before the CLI configured logging still logs after it.

    `dictConfig` disables every existing logger it does not name unless told
    otherwise, so a process that imported another `kluster` module first -- the
    test suite, running this module before `test_sealing` -- lost that module's
    log lines to the CLI's setup.
    """
    before = logging.getLogger('kluster.scripts.credentials.made_before_update_crds')
    try:
        with pytest.raises(SystemExit):
            _ = cli.main(['--help'])
        assert not before.disabled
    finally:
        before.disabled = False


def test_a_stack_file_with_no_config_block_overrides_nothing(tmp_path: Path) -> None:
    """What `pulumi stack init` writes before any `config set`: a salt and no block."""
    stack = tmp_path / 'Pulumi.fresh.yaml'
    _ = stack.write_text('encryptionsalt: v1:abc\n')

    assert sources.read_config(stack, required=False) == {}
    with pytest.raises(sources.SourceError, match='has no `config:` block'):
        _ = sources.read_config(stack)


# -- renovate's managers on the block ---------------------------------------------

#: What `renovate.json5` reads the block with, one pattern per manager, spelled
#: as Python spells the pattern; `as_renovate_spells_it` turns each into the
#: line the file holds.
HTTP_CHART_MATCH_STRING = (
    r'versions:chart-(?<depName>[\w-]+):\s+value:\s+repository: (?<registryUrl>https://\S+)'
    r'\s+version: (?<currentValue>\S+)'
)
OCI_CHART_MATCH_STRING = (
    r'versions:chart-(?<depName>[\w-]+):\s+value:\s+repository: oci://(?<registry>\S+)'
    r'\s+version: (?<currentValue>\S+)\s+digest: (?<currentDigest>sha256:[0-9a-f]{64})'
)
MANIFEST_MATCH_STRING = (
    r'versions:manifest-[\w-]+:\s+value:\s+repository: (?<depName>[\w.-]+/[\w.-]+)'
    r'\s+release: (?<currentValue>\S+)\s+asset: \S+\s+sha256: (?<currentDigest>[0-9a-f]{64})'
)


def managers() -> list[str]:
    """Each `customManagers` entry of `renovate.json5` that reads `Pulumi.yaml`, as text.

    An entry is cut at the next `customType:`, since a template may carry the
    braces a brace scan would cut at.
    """
    config = (ROOT / 'renovate.json5').read_text()
    block = config[config.index('customManagers: [') : config.index('customDatasources: {')]
    return [entry for entry in block.split('customType:')[1:] if "'/^Pulumi\\\\.yaml$/'" in entry]


def manager(pattern: str) -> str:
    """The one manager entry holding `pattern`."""
    (entry,) = [entry for entry in managers() if as_renovate_spells_it(pattern) in entry]
    return entry


def test_the_http_chart_manager_reads_every_http_chart_pin_and_nothing_else() -> None:
    """The chart, its repository and its version, from one match, through the Helm data source."""
    text = (ROOT / 'Pulumi.yaml').read_text()
    versions = Versions(ProjectFile(project_config()))
    entry = manager(HTTP_CHART_MATCH_STRING)

    found = {
        match['depName']: (match['registryUrl'], match['currentValue'])
        for match in as_python_spells_it(HTTP_CHART_MATCH_STRING).finditer(text)
    }
    expected = {
        name: (pin.repository, pin.version)
        for name in ProjectFile(project_config()).names(CHART)
        if not (pin := versions.chart[name]).oci
    }
    assert expected
    assert found == expected
    assert scalar(entry, 'datasourceTemplate') == 'helm'


def test_the_oci_chart_manager_reads_every_oci_chart_pin_and_nothing_else() -> None:
    """The version and the digest from one match, through the docker data source, so a bump moves both.

    The package is the reference Helm pulls: the registry path with the
    chart's name, which is the key's `<name>`, appended.
    """
    text = (ROOT / 'Pulumi.yaml').read_text()
    versions = Versions(ProjectFile(project_config()))
    entry = manager(OCI_CHART_MATCH_STRING)

    found = {
        match['depName']: (f'{match["registry"]}/{match["depName"]}', match['currentValue'], match['currentDigest'])
        for match in as_python_spells_it(OCI_CHART_MATCH_STRING).finditer(text)
    }
    expected = {
        name: (f'{pin.repository.removeprefix("oci://")}/{name}', pin.version, pin.digest)
        for name in ProjectFile(project_config()).names(CHART)
        if (pin := versions.chart[name]).oci
    }
    assert expected
    assert found == expected
    assert scalar(entry, 'packageNameTemplate') == '{{{registry}}}/{{{depName}}}'
    assert scalar(entry, 'datasourceTemplate') == 'docker'


def test_the_manifest_manager_moves_the_release_and_its_digest_in_one_match() -> None:
    """Apart, a release bump would land with a digest the fetch then refuses."""
    text = (ROOT / 'Pulumi.yaml').read_text()
    versions = Versions(ProjectFile(project_config()))
    entry = manager(MANIFEST_MATCH_STRING)

    found = [
        (match['depName'], match['currentValue'], match['currentDigest'])
        for match in as_python_spells_it(MANIFEST_MATCH_STRING).finditer(text)
    ]
    expected = [
        ((pin := versions.manifest[name]).repository, pin.release, pin.sha256)
        for name in ProjectFile(project_config()).names(MANIFEST)
    ]
    assert expected
    assert found == expected
    assert scalar(entry, 'datasourceTemplate') == 'github-release-attachments'


#: The data sources the block's in-cluster pins are read through: the two chart
#: managers', the manifest manager's, and the image manager's.
IN_CLUSTER_DATASOURCES = {'helm', 'docker', 'github-release-attachments'}


def in_cluster_rule(rules: list[str]) -> int:
    """The index of the rule that groups the block's in-cluster pins: by the file, not by a dependency.

    Its group is shared: the `kubeseal` rule names it too, so the slug alone
    does not find it.
    """
    (found,) = [
        index
        for index, rule in enumerate(rules)
        if scalar(rule, 'groupSlug') == 'in-cluster' and listed(rule, 'matchFileNames') == ['Pulumi.yaml']
    ]
    return found


def test_the_in_cluster_rule_groups_what_the_block_managers_read() -> None:
    """The group the chart, manifest and in-cluster image bumps travel in.

    Matched by the file the managers read and the data sources they read
    through. The gateway's root filesystems are images in the same file, so
    the rule that takes them into their own group has to sit after this one:
    the last matching rule wins a field. Nothing goes red in renovate itself
    when this breaks -- renovate reads its configuration from the default
    branch -- so it is held here.
    """
    config = (ROOT / 'renovate.json5').read_text()
    rules = package_rules(config)
    in_cluster = in_cluster_rule(rules)
    (gateway,) = [index for index, rule in enumerate(rules) if scalar(rule, 'groupSlug') == 'gateway-rootfs']
    rule = rules[in_cluster]

    assert listed(rule, 'matchFileNames') == ['Pulumi.yaml']
    assert set(listed(rule, 'matchDatasources')) == IN_CLUSTER_DATASOURCES
    read_through = {
        scalar(entry, 'datasourceTemplate')
        for pattern in (HTTP_CHART_MATCH_STRING, OCI_CHART_MATCH_STRING, MANIFEST_MATCH_STRING)
        for entry in [manager(pattern)]
    }
    assert read_through <= IN_CLUSTER_DATASOURCES
    assert in_cluster < gateway


# -- the pinned tool downloads ------------------------------------------------


#: Contents for the one file the fetch cares about. Nothing runs it.
TOOL = b'#!/bin/sh\nexit 0\n'


def archive(binary: str) -> bytes:
    """A tar.gz holding one executable called `binary`, and nothing else."""
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tarobj:
        entry = tarfile.TarInfo(binary)
        entry.size = len(TOOL)
        entry.mode = 0o755
        tarobj.addfile(entry, BytesIO(TOOL))
    return buffer.getvalue()


class FakeDownload:
    """A streamed response that hands back the bytes it was given."""

    def __init__(self, payload: bytes) -> None:
        self.headers: dict[str, str] = {'content-length': str(len(payload))}
        self.raw: BytesIO = BytesIO(payload)

    def __enter__(self) -> FakeDownload:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None


def serve(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> list[str]:
    """Replace the download seam, and hand back the list of URLs it was asked for."""
    requested: list[str] = []

    def get(url: str, **_: object) -> requests.Response:
        requested.append(url)
        return cast('requests.Response', FakeDownload(payload))

    monkeypatch.setattr(sources.requests, 'get', get)
    return requested


def test_fetch_helm_refuses_an_archive_that_is_not_the_pinned_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A well-formed tarball that is not the pinned release is refused by digest.

    Refused *before* it is unpacked, which the empty directory is the claim
    about: an archive already extracted has had its say whatever the digest
    turns out to be.
    """
    _ = serve(monkeypatch, archive('helm'))

    with pytest.raises(ValueError, match=pins.HELM_SHA256):
        _ = sources.fetch_helm(tmp_path)

    assert list(tmp_path.iterdir()) == []


def _reaches_cilium(rule: str) -> bool:
    """Whether `rule` could match the Cilium chart pin, by every matcher it sets.

    A matcher this scan cannot read is taken as matching, so a rule is only
    cleared by one that provably misses.
    """
    presents = {
        'matchDepNames': ('cilium',),
        'matchPackageNames': ('quay.io/cilium/charts/cilium',),
        'matchFileNames': ('Pulumi.yaml',),
        'matchDatasources': ('docker',),
        'matchManagers': ('custom.regex', 'regex'),
    }
    for key in re.findall(r'^\s*((?:match|exclude)\w+):', rule, re.MULTILINE):
        patterns = listed(rule, key)
        values = presents.get(key)
        if values is None or not patterns:
            continue
        if not any(fnmatch.fnmatch(value, pattern) for pattern in patterns for value in values):
            return False
    return True


def test_the_cilium_chart_travels_alone() -> None:
    """A Cilium bump replaces every node's datapath, so it is a pull request of its own, as Talos's is.

    The rule names the chart pin's dependency, which the OCI chart manager
    takes from the key's `<name>`, and sits after the in-cluster rule, which
    matches the same pin by file and data source: the last matching rule wins
    a field. No later rule that sets a group may reach it. Renovate reads its
    configuration from the default branch, so nothing goes red there when
    this breaks.
    """
    config = (ROOT / 'renovate.json5').read_text()
    rules = package_rules(config)
    versions = Versions(ProjectFile(project_config()))
    in_cluster = in_cluster_rule(rules)
    (cilium,) = [index for index, rule in enumerate(rules) if 'cilium' in listed(rule, 'matchDepNames')]
    rule = rules[cilium]

    assert versions.chart['cilium'].oci
    assert listed(rule, 'matchDepNames') == ['cilium']
    assert listed(rule, 'matchFileNames') == ['Pulumi.yaml']
    assert scalar(rule, 'groupSlug') == 'cilium'
    assert in_cluster < cilium
    for later in rules[cilium + 1 :]:
        if scalar(later, 'groupName') is None and scalar(later, 'groupSlug') is None:
            continue
        assert not _reaches_cilium(later), f'a later rule regroups the Cilium chart:\n{later}'


def test_no_pin_carries_an_annotation_comment() -> None:
    """The script's own pins announce no automation they do not have.

    No manager reads this module: the Helm binary moves by hand. An annotation
    above it would therefore be inert — and inert ones are worse than none,
    because they read as a working mechanism and stop anyone from building
    the real one.

    Anywhere on the line, not only at its start: an annotation trailing a
    version is just as inert and reads just as much like automation.
    """
    module = Path(pins.__file__).read_text()

    # An emptied or renamed module would satisfy a purely negative assertion.
    assert f"HELM_VERSION = '{pins.HELM_VERSION}'" in module

    assert re.findall(r'# renovate:.*', module) == []


def test_the_mise_action_rule_outranks_the_github_actions_group() -> None:
    """Order is what puts `jdx/mise-action` in the toolchain group, so order is held here.

    `packageRules` are applied in the order they are written and the last match
    wins a given field, so the rule naming the dependency has to come after the
    rule matching its manager. Reordered, the dependency returns to the actions
    group and nothing goes red: renovate reads its configuration from the
    default branch, so no check on a pull request can see it.

    Held as text, the way the manager pattern above is, because there is no
    JSON5 parser here. Each substring is written exactly once, which the test
    states rather than assumes.
    """
    config = (ROOT / 'renovate.json5').read_text()

    assert config.count("'github-actions'") == 1
    assert config.count("'jdx/mise-action'") == 1

    assert config.index("'jdx/mise-action'") > config.index("'github-actions'")


def test_the_kubernetes_provider_rule_outranks_the_python_group() -> None:
    """The same order rule, for the rule that takes `pulumi-kubernetes` out of the python group.

    The pep621 manager reads the pin, so the rule naming the dependency has to
    come after the rule matching that manager; reordered, the provider bump
    rides in the python group again, where a bump waiting on a regeneration
    holds every other library bump red, and nothing goes red here for it.
    Each rule is found by what it matches, so the dependency's name written
    elsewhere -- the manager on the `packages:` block names it too -- is no
    second candidate.
    """
    rules = package_rules((ROOT / 'renovate.json5').read_text())

    (python,) = [index for index, rule in enumerate(rules) if 'pep621' in listed(rule, 'matchManagers')]
    (provider,) = [index for index, rule in enumerate(rules) if 'pulumi-kubernetes' in listed(rule, 'matchDepNames')]

    assert provider > python
    assert scalar(rules[provider], 'groupSlug') == 'kubernetes-provider'


# -- the bundle -------------------------------------------------------------------


#: The repository's own bundle, at the path its `packages:` entry names.
BUNDLE = sources.bundle_path(ROOT / 'Pulumi.yaml')


def test_the_bundle_is_where_the_packages_entry_names_it() -> None:
    """The script writes the manifest `pulumi install` reads, so it reads the path from the same entry."""
    assert BUNDLE == ROOT / 'packages/crds/crds.yaml'
    assert BUNDLE.is_file()


@pytest.mark.parametrize(
    ('packages', 'refusal'),
    [
        ({}, 'has no `packages:` entry `crds`'),
        ({'crds': {'source': 'kubernetes', 'version': '1.0.0'}}, 'has no `packages:` entry `crds`'),
        ({'crds': {'source': 'kubernetes', 'extensions': ['name=crds']}}, 'names 0 `crd-manifest=` paths'),
        (
            {'crds': {'source': 'kubernetes', 'extensions': ['crd-manifest=a.yaml', 'crd-manifest=b.yaml']}},
            'names 2 `crd-manifest=` paths',
        ),
    ],
)
def test_a_project_whose_entry_names_no_one_manifest_is_refused_by_name(
    tmp_path: Path, packages: dict[str, object], refusal: str
) -> None:
    project = tmp_path / 'Pulumi.yaml'
    _ = project.write_text(json.dumps({'name': 'p', 'packages': packages}))

    with pytest.raises(sources.SourceError, match=re.escape(refusal)):
        _ = sources.bundle_path(project)


def test_the_committed_bundle_is_the_scripts_own_output() -> None:
    """Selecting from the committed bundle and dumping it again gives back the same text.

    The bundle is the one input of the SDK's generation, and what keeps it
    honest is that it has one writer: definitions out of the order the
    selection writes them in, a `status` left in, a definition of a dropped
    group restored, or text laid out other than the dumper lays it out, is not
    a fixed point of the selection. A value edited in the dumper's own layout
    is one, and no offline check reaches it: `test_conventions` holds the
    resources the SDK carries to the bundle's, not their fields.
    """
    text = BUNDLE.read_text()

    assert sources.dump_bundle(sources.select_crds([text])) == text, (
        'packages/crds/crds.yaml is not what update_crds writes; run `mise x -- uv run update_crds`'
    )


#: The Cilium kinds only the agent writes, whose schemas carry hyphenated
#: property names the extension's generator takes since pulumi-kubernetes
#: 4.34.2 (pulumi/pulumi-kubernetes#4611).
AGENT_WRITTEN_CILIUM = (
    'ciliumendpoints.cilium.io',
    'ciliumendpointslices.cilium.io',
    'ciliumidentities.cilium.io',
    'ciliumnodes.cilium.io',
)


def test_the_bundle_carries_the_agent_written_cilium_kinds() -> None:
    """Nothing declares these four, and the bundle carries them like every definition its sources ship.

    The bundle is the cluster's definitions as the pins render them, not a
    list of what the stacks happen to use, so a kind is missing from it only
    by a rule `select_crds` states. None names these.
    """
    names = {crd.name for crd in sources.select_crds([BUNDLE.read_text()])}

    assert [name for name in AGENT_WRITTEN_CILIUM if name not in names] == []


# -- the regeneration ---------------------------------------------------------------

#: A stand-in for a tool the regeneration runs: it appends what it was asked
#: and from where to the log `FAKE_LOG` names, does what the real `pulumi
#: install` does to `pyproject.toml` -- rewrites it -- and exits with
#: `FAKE_EXIT`.
FAKE_TOOL = """#!{python}
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps({{
        'tool': name,
        'argv': sys.argv[1:],
        'cwd': os.getcwd(),
        'backend': os.environ.get('PULUMI_BACKEND_URL'),
    }}) + '\\n')
if name == 'pulumi':
    pathlib.Path('pyproject.toml').write_text('[tool.uv.sources]\\nrewritten = true\\n')
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
"""

PROJECT = """\
name: project
packages:
  crds:
    source: kubernetes
    version: 1.0.0
    extensions:
      - name=crds
      - crd-manifest=packages/crds/crds.yaml
"""

PYPROJECT = '[project]\nname = "project"\n'


def stand_ins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """`pulumi` and `uv` stand-ins first on `PATH`, and the log they write: the bin directory and the log."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for tool in ('pulumi', 'uv'):
        script = bin_dir / tool
        _ = script.write_text(FAKE_TOOL.format(python=sys.executable))
        script.chmod(0o755)
    log = tmp_path / 'tools.log'
    monkeypatch.setenv('FAKE_LOG', str(log))
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
    # Whatever backend the caller's environment names is not the one the run uses.
    monkeypatch.setenv('PULUMI_BACKEND_URL', 'postgres://the-callers-backend.invalid/state')
    return bin_dir, log


def runs(log: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def project_dir(tmp_path: Path) -> Path:
    project = tmp_path / 'project'
    (project / 'packages/crds').mkdir(parents=True)
    _ = (project / 'Pulumi.yaml').write_text(PROJECT)
    _ = (project / 'pyproject.toml').write_text(PYPROJECT)
    return project


def test_generate_runs_pulumi_install_on_a_backend_of_its_own_then_relocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pulumi install` from the project, offline of any backend the caller's environment names, then `uv lock`.

    The backend it is handed is an empty `file://` directory under the run's
    working directory: with no backend logged in, the CLI in an agent's
    environment signs up a Pulumi Cloud account to answer a package lookup.
    `pyproject.toml` comes back byte for byte, since the rewrite `pulumi
    install` makes of it is not part of a regeneration.
    """
    bin_dir, log = stand_ins(tmp_path, monkeypatch)
    project = project_dir(tmp_path)
    workdir = tmp_path / 'work'
    workdir.mkdir()

    cli.generate(project / 'Pulumi.yaml', pulumi=bin_dir / 'pulumi', workdir=workdir)

    install, lock = runs(log)
    assert install['tool'] == 'pulumi'
    assert install['argv'] == ['--non-interactive', 'install', '--no-dependencies', '--no-plugins']
    assert install['cwd'] == str(project)
    assert install['backend'] == (workdir / 'backend').as_uri()
    assert lock['tool'] == 'uv'
    assert lock['argv'] == ['lock']
    assert lock['cwd'] == str(project)
    assert (project / 'pyproject.toml').read_text() == PYPROJECT


def test_a_failed_install_leaves_pyproject_as_it_was_and_locks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir, log = stand_ins(tmp_path, monkeypatch)
    monkeypatch.setenv('FAKE_EXIT', '1')
    project = project_dir(tmp_path)
    workdir = tmp_path / 'work'
    workdir.mkdir()

    with pytest.raises(subprocess.CalledProcessError):
        cli.generate(project / 'Pulumi.yaml', pulumi=bin_dir / 'pulumi', workdir=workdir)

    assert [run['tool'] for run in runs(log)] == ['pulumi']
    assert (project / 'pyproject.toml').read_text() == PYPROJECT


def test_a_run_with_no_pulumi_on_path_is_refused_before_it_reads_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The render is minutes of network a missing CLI would throw away, so the CLI is asked for first."""
    monkeypatch.setenv('PATH', str(tmp_path))
    missing = tmp_path / 'no-such-project.yaml'

    with pytest.raises(cli.NoPulumi, match=re.escape('mise x -- uv run update_crds')):
        _ = cli.main(['--project', str(missing)])


def test_a_run_from_a_bundle_writes_the_selection_where_the_entry_names_it_and_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--from-bundle` selects, writes the bundle the block names, regenerates from it, and records nothing.

    No record, because the pins did not produce that bundle: a record written
    beside it would vouch for a render that never ran.
    """
    _, log = stand_ins(tmp_path, monkeypatch)
    project = project_dir(tmp_path)
    rendered = tmp_path / 'rendered.yaml'
    _ = rendered.write_text(f'{DEPLOYMENT}---{DROPPED}---{CRD}')

    assert cli.main(['--project', str(project / 'Pulumi.yaml'), '--from-bundle', str(rendered)]) == 0

    written = project / 'packages/crds/crds.yaml'
    assert written.read_text() == sources.dump_bundle(sources.select_crds([CRD]))
    assert not (written.parent / record.FILE_NAME).exists()
    assert [run['tool'] for run in runs(log)] == ['pulumi', 'uv']


def test_a_run_with_bundle_writes_only_there(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--bundle` is a look at the render, and leaves the committed bundle, the record and the SDK alone."""
    _, log = stand_ins(tmp_path, monkeypatch)
    project = project_dir(tmp_path)
    rendered = tmp_path / 'rendered.yaml'
    _ = rendered.write_text(CRD)
    out = tmp_path / 'out.yaml'

    assert (
        cli.main(['--project', str(project / 'Pulumi.yaml'), '--from-bundle', str(rendered), '--bundle', str(out)]) == 0
    )

    assert out.read_text() == sources.dump_bundle(sources.select_crds([CRD]))
    assert list((project / 'packages/crds').iterdir()) == []
    assert runs(log) == []
