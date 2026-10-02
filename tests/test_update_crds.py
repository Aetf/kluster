"""The pins and the selection rule behind `packages/crds`.

Nothing here reaches the network: what is worth holding still is which CRDs
survive the filter, that the pins the script reads are the block's and the
bindings were generated from them, that a chart below its floor is refused,
that renovate's managers read the block, and that a tool download nothing
vouches for is refused. The cases that download at all are handed their bytes
by a stand-in.
"""

from __future__ import annotations

import fnmatch
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
import tarfile
import tomllib
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
import requests
from renovate_text import as_python_spells_it, as_renovate_spells_it, listed, package_rules, scalar

from kluster.lib.versions import CHART, MANIFEST, ChartPin, Floor, ProjectFile, Versions
from kluster.scripts.update_crds import cli, pins, record, sources

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
    """Two sources may legitimately ship the same definition; `crd2pulumi` may not see it twice."""
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
    one release while the bindings describe another, with every check green.
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


def test_the_record_is_the_pins_the_bindings_were_generated_from() -> None:
    """`packages/crds` records the pins `update_crds` read, and they are the block's.

    Renovate moves a pin and cannot run `update_crds`, so a bump arrives with
    the bindings describing the release before it. This is what makes such a
    bump red until someone regenerates on its branch.
    """
    written = json.loads((ROOT / 'packages/crds' / record.FILE_NAME).read_text())

    assert written == record.record(ProjectFile(project_config())), (
        'packages/crds was generated from other pins than Pulumi.yaml holds; run `uv run update_crds`'
    )


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
    as it was, and the bindings would keep describing the previous release.
    """
    (tree,) = pins.SOURCE_TREES
    tree_only = {
        f'versions:chart-{tree.chart}': {
            'value': {'repository': 'https://charts.example.invalid/', 'version': '1.0.0', 'definitions': False}
        }
    }

    assert record.read_charts(ProjectFile(tree_only)) == [tree.chart]


def test_the_cilium_source_tree_is_read_at_the_cilium_chart_version() -> None:
    """The chart installs no definitions, so the bindings would silently describe the wrong release.

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

    The floors are checked against the declared operator before anything is
    rendered, the source tree is fetched at the ref its chart's pin names, the
    manifest through the digest-checked fetch, and exactly the charts that
    render definitions are rendered, each from its pin.
    """
    config = with_version(project_config(), 'versions:chart-cilium', '9.9.9')
    project = ProjectFile(config)
    versions = Versions(project)
    calls: list[str] = []
    helm = tmp_path / 'helm'

    def app_version(_: Path, pin: ChartPin, *, workdir: Path) -> str:
        calls.append(f'floor {pin.name}')
        return '99.0.0'

    def tree(source: pins.SourceTree, ref: str) -> list[str]:
        calls.append(f'tree {source.repo}@{ref}')
        return []

    def manifest(pin: object) -> str:
        calls.append(f'manifest {pin}')
        return ''

    def render(_: Path, pin: ChartPin, *, workdir: Path) -> str:
        calls.append(f'render {pin.name} {pin.version}')
        return ''

    def fetch_helm(_: Path) -> Path:
        return helm

    monkeypatch.setattr(sources, 'fetch_helm', fetch_helm)
    monkeypatch.setattr(sources, 'chart_app_version', app_version)
    monkeypatch.setattr(sources, 'fetch_source_tree', tree)
    monkeypatch.setattr(cli, 'fetch_manifest', manifest)
    monkeypatch.setattr(sources, 'render_chart', render)

    _ = cli.collect_documents(tmp_path, project)

    charts = [versions.chart[name] for name in project.names(CHART)]
    floored = [f'floor {chart.name}' for chart in charts if chart.floor is not None]
    assert calls[: len(floored)] == floored
    assert 'tree cilium/cilium@v9.9.9' in calls
    assert [call for call in calls if call.startswith('manifest')] == [
        f'manifest {versions.manifest[name]}' for name in project.names(MANIFEST)
    ]
    assert [call for call in calls if call.startswith('render')] == [
        f'render {chart.name} {chart.version}' for chart in charts if chart.definitions
    ]


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


def test_update_crds_starts_when_the_bindings_do_not_import() -> None:
    """The script regenerates `packages/crds`, so it is what repairs a package that no longer imports.

    It therefore imports nothing of the bindings, directly or through
    `kluster.lib`: run with `pulumi_crds` made unimportable, `--help` still
    answers.
    """
    broken = (
        'import sys; sys.modules["pulumi_crds"] = None; '
        'from kluster.scripts.update_crds import main; sys.exit(main(["--help"]))'
    )

    run = subprocess.run([sys.executable, '-c', broken], capture_output=True, text=True, timeout=120, check=False)

    assert run.returncode == 0, run.stderr
    assert '--project' in run.stdout


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
    (in_cluster,) = [index for index, rule in enumerate(rules) if scalar(rule, 'groupSlug') == 'in-cluster']
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


#: What `renovate.json5` matches the pin with, spelled exactly as that file
#: holds it. Both constants are captured by one pattern because a bump has to
#: move them together.
CRD2PULUMI_MATCH_STRING = (
    "CRD2PULUMI_VERSION = '(?<currentValue>v[\\d.]+)'[\\s\\S]*?CRD2PULUMI_SHA256 = '(?<currentDigest>[0-9a-f]{64})'"
)

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


def test_fetch_crd2pulumi_refuses_an_archive_that_is_not_the_pinned_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A well-formed tarball that is not the pinned release is refused by digest.

    Refused *before* it is unpacked, which the empty directory is the claim
    about: an archive already extracted has had its say whatever the digest
    turns out to be.
    """
    _ = serve(monkeypatch, archive('crd2pulumi'))

    with pytest.raises(ValueError, match=pins.CRD2PULUMI_SHA256):
        _ = sources.fetch_crd2pulumi(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_fetch_helm_refuses_an_archive_that_is_not_the_pinned_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both tools answer to the same check, so both are held to it here."""
    _ = serve(monkeypatch, archive('helm'))

    with pytest.raises(ValueError, match=pins.HELM_SHA256):
        _ = sources.fetch_helm(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_fetch_crd2pulumi_unpacks_the_archive_whose_digest_matches_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = archive('crd2pulumi')
    monkeypatch.setattr(pins, 'CRD2PULUMI_SHA256', hashlib.sha256(payload).hexdigest())
    requested = serve(monkeypatch, payload)

    binary = sources.fetch_crd2pulumi(tmp_path)

    assert requested == [pins.CRD2PULUMI_URL]
    assert binary == (tmp_path / 'crd2pulumi').resolve()


def test_the_pinned_asset_is_named_after_the_pinned_version() -> None:
    """What renovate substitutes into to find the next release's asset and checksum."""
    assert pins.CRD2PULUMI_URL.endswith(f'crd2pulumi-{pins.CRD2PULUMI_VERSION}-linux-amd64.tar.gz')


def test_renovate_moves_the_crd2pulumi_version_and_digest_together() -> None:
    """The manager's pattern is the one this module answers to.

    A stale digest cannot be caught by the next `update_crds` run alone — that
    run is what the pin exists to stop — so the link between the file and the
    manager is held here: a rename, or a line inserted between the two
    constants, fails here rather than in a pull request nobody can merge.
    """
    config = (ROOT / 'renovate.json5').read_text()
    module = Path(pins.__file__).read_text()

    # `json.dumps` is the escaping renovate.json5 holds the pattern in.
    assert json.dumps(CRD2PULUMI_MATCH_STRING) in config

    # Python spells a named group `(?P<...>`, renovate's regex engine `(?<...>`.
    found = re.search(CRD2PULUMI_MATCH_STRING.replace('(?<', '(?P<'), module)

    assert found is not None
    assert found.group('currentValue') == pins.CRD2PULUMI_VERSION
    assert found.group('currentDigest') == pins.CRD2PULUMI_SHA256


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
    (in_cluster,) = [index for index, rule in enumerate(rules) if scalar(rule, 'groupSlug') == 'in-cluster']
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

    The one manager that reads this module matches the `crd2pulumi` constants
    by name and needs no comment to find them; the Helm binary moves by hand.
    An annotation above it would therefore be inert — and inert ones are worse
    than none, because they read as a working mechanism and stop anyone from
    building the real one.

    Anywhere on the line, not only at its start: an annotation trailing a
    version is just as inert and reads just as much like automation.
    """
    module = Path(pins.__file__).read_text()

    # An emptied or renamed module would satisfy a purely negative assertion.
    assert f"CRD2PULUMI_VERSION = '{pins.CRD2PULUMI_VERSION}'" in module

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
    """
    config = (ROOT / 'renovate.json5').read_text()

    assert config.count("'pep621'") == 1
    assert config.count("'pulumi-kubernetes'") == 1

    assert config.index("'pulumi-kubernetes'") > config.index("'pep621'")


# -- the generated package and the provider it was generated against ----------


#: The `dependencies` line as `crd2pulumi` v1.6.2 writes it, with the version
#: its release was built against baked in.
BAKED_PYPROJECT = """[project]
  name = "pulumi_crds"
  dependencies = ["parver>=0.2.1", "pulumi>=3.231.0,<4.0.0", "pulumi-kubernetes==4.23.0", "requests>=2.21,<3.0"]
  version = "4.34.1"
"""


def fake_crd2pulumi(directory: Path, pyproject: str) -> Path:
    """A generator that writes one file into `--pythonPath`: the `pyproject.toml` the rewrite reads."""
    script = directory / 'crd2pulumi'
    _ = script.write_text(
        '#!/usr/bin/env python3\n'
        'import pathlib, sys\n'
        'output = pathlib.Path(sys.argv[sys.argv.index("--pythonPath") + 1])\n'
        'output.mkdir()\n'
        f'(output / "pyproject.toml").write_text({pyproject!r})\n'
    )
    script.chmod(0o755)
    return script


def test_generate_declares_the_provider_it_generated_against_as_a_floor(tmp_path: Path) -> None:
    """`--version` leaves the baked dependency line alone, so the script rewrites it.

    A floor at the generated-against version rather than the baked pin: the
    root `pyproject.toml` is where the one exact pin lives, and the generated
    package left as `crd2pulumi` wrote it would hold the whole project on the
    release the tool happened to be built with.
    """
    output = tmp_path / 'crds'
    output.mkdir()
    _ = (output / 'stale').write_text('the previous bindings')
    generated_against = importlib.metadata.version('pulumi-kubernetes')

    cli.generate([], output, fake_crd2pulumi(tmp_path, BAKED_PYPROJECT))

    dependencies = tomllib.loads((output / 'pyproject.toml').read_text())['project']['dependencies']
    assert f'pulumi-kubernetes>={generated_against}' in dependencies
    assert not [dep for dep in dependencies if dep.startswith('pulumi-kubernetes==')]
    assert not (output / 'stale').exists()
    assert not output.with_suffix('.bak').exists()


def test_generate_refuses_a_generator_that_wrote_no_dependency_line(tmp_path: Path) -> None:
    """A release whose template changed fails the run by name, and the previous bindings come back."""
    output = tmp_path / 'crds'
    output.mkdir()
    _ = (output / 'previous').write_text('the previous bindings')
    without_the_line = BAKED_PYPROJECT.replace('"pulumi-kubernetes==4.23.0", ', '')

    with pytest.raises(RuntimeError, match='carries 0 pulumi-kubernetes requirements'):
        cli.generate([], output, fake_crd2pulumi(tmp_path, without_the_line))

    assert (output / 'previous').read_text() == 'the previous bindings'
    assert not (output / 'pyproject.toml').exists()


def _pinned_provider(requirements: list[str], *, operator: str) -> str:
    """The version one `pulumi-kubernetes<operator>` requirement in `requirements` names."""
    (version,) = [
        found.group(1)
        for requirement in requirements
        if (found := re.fullmatch(rf'pulumi-kubernetes{re.escape(operator)}([\d.]+)', requirement))
    ]
    return version


def test_the_generated_package_is_held_to_the_pinned_provider() -> None:
    """`pyproject.toml`'s exact pin, the generated floor and `pulumi-plugin.json` name one version.

    The bindings register every resource at the version they were generated
    against, so the version the program installs and the version the package
    carries are one fact written in three places (framework/pulumi.md §4).
    The pin is edited -- by renovate or by hand -- and the package is
    generated, so the three agree only when `update_crds` has run since the
    edit; this is the `uv sync --locked` of that package.
    """
    pinned = _pinned_provider(
        tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['dependencies'], operator='=='
    )
    generated = tomllib.loads((ROOT / 'packages/crds/pyproject.toml').read_text())['project']
    plugin = json.loads((ROOT / 'packages/crds/pulumi_crds/pulumi-plugin.json').read_text())

    stale = f'packages/crds was generated against a different pulumi-kubernetes than pyproject.toml pins ({pinned}); run `uv run update_crds`'
    assert _pinned_provider(generated['dependencies'], operator='>=') == pinned, stale
    assert generated['version'] == pinned, stale
    assert plugin == {'resource': True, 'name': 'crds', 'version': pinned}, stale
