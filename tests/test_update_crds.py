"""The register and the selection rule behind `packages/crds`.

Nothing here reaches the network: what is worth holding still is which CRDs
survive the filter, that every pin still clears the floor its design doc put
under it, and that a tool download nothing vouches for is refused. The one
case that downloads at all is handed its bytes by a stand-in.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
import requests

from kluster.scripts.update_crds import pins, sources

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

    with pytest.raises(sources.SourceError, match='no metadata.name'):
        _ = sources.select_crds([nameless])


def test_select_crds_names_a_definition_that_carries_no_group() -> None:
    groupless = """
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
"""

    with pytest.raises(sources.SourceError, match='widgets.example.com has no spec.group'):
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
    with pytest.raises(sources.SourceError, match='the entry widget.yaml carries no download_url'):
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
    assert pins.version_tuple(version) == expected


@pytest.mark.parametrize('chart', [chart for chart in pins.CHARTS if chart.min_app_version is not None], ids=str)
def test_every_pin_clears_its_floor(chart: pins.Chart) -> None:
    """A bump that drops a chart below a floor the design docs record fails here.

    The floor is on the operator, so it is checked against `app_version`: the
    `cloudnative-pg` chart is versioned 0.x and ships CNPG 1.x.
    """
    assert chart.min_app_version is not None
    assert pins.version_tuple(chart.app_version) >= pins.version_tuple(chart.min_app_version), (
        f'{chart.name} {chart.app_version} is below its floor {chart.min_app_version}: {chart.floor}'
    )


def test_every_pin_records_a_floor_or_says_there_is_none() -> None:
    """`floor` is prose a reviewer reads, so the only thing to hold is that it is there."""
    assert all(chart.floor for chart in pins.CHARTS)
    assert all(manifest.floor for manifest in pins.MANIFESTS)
    assert all(tree.floor for tree in pins.SOURCE_TREES)


def test_a_floor_is_stated_only_where_it_is_checkable() -> None:
    """A chart claiming a numeric floor states the number, and one without does not."""
    for chart in pins.CHARTS:
        assert (chart.min_app_version is not None) == (chart.floor != 'NO FLOOR')


def test_cilium_crds_come_from_the_source_tree_at_the_pinned_chart_version() -> None:
    """The chart installs none, so the bindings would silently describe the wrong release."""
    (cilium_chart,) = [chart for chart in pins.CHARTS if chart.name == 'cilium']
    (cilium_tree,) = [tree for tree in pins.SOURCE_TREES if tree.repo == 'cilium/cilium']

    assert not cilium_chart.crds
    assert cilium_tree.ref == f'v{cilium_chart.version}'


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
    config = (Path(__file__).parent.parent / 'renovate.json5').read_text()
    module = Path(pins.__file__).read_text()

    # `json.dumps` is the escaping renovate.json5 holds the pattern in.
    assert json.dumps(CRD2PULUMI_MATCH_STRING) in config

    # Python spells a named group `(?P<...>`, renovate's regex engine `(?<...>`.
    found = re.search(CRD2PULUMI_MATCH_STRING.replace('(?<', '(?P<'), module)

    assert found is not None
    assert found.group('currentValue') == pins.CRD2PULUMI_VERSION
    assert found.group('currentDigest') == pins.CRD2PULUMI_SHA256


def test_no_pin_carries_an_annotation_comment() -> None:
    """The register announces no automation it does not have.

    The one manager that reads this module matches the `crd2pulumi` constants
    by name and needs no comment to find them; every other pin here moves by
    hand, and for the pins the render reads the bump is only finished by
    regenerating `packages/crds`. An annotation above one of them would
    therefore be inert — and inert ones are worse than none, because they read
    as a working mechanism and stop anyone from building the real one.

    Anywhere on the line, not only at its start: an annotation trailing a
    version is just as inert and reads just as much like automation.
    """
    module = Path(pins.__file__).read_text()

    # An emptied or renamed register would satisfy a purely negative assertion.
    assert f"version='{pins.CHARTS[0].version}'" in module

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
    config = (Path(__file__).parent.parent / 'renovate.json5').read_text()

    assert config.count("'github-actions'") == 1
    assert config.count("'jdx/mise-action'") == 1

    assert config.index("'jdx/mise-action'") > config.index("'github-actions'")
