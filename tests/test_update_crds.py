"""The register and the selection rule behind `packages/crds`.

Nothing here reaches the network: what is worth holding still is which CRDs
survive the filter, and that every pin still clears the floor its design doc
put under it.
"""

from __future__ import annotations

import pytest

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

    assert [crd['metadata']['name'] for crd in selected] == ['widgets.example.com']


def test_select_crds_drops_retired_groups() -> None:
    """`fpga.intel.com` rides along in the Intel operator chart and is retired."""
    selected = sources.select_crds([DROPPED, CRD])

    assert [crd['spec']['group'] for crd in selected] == ['example.com']


def test_select_crds_strips_the_cluster_written_status() -> None:
    (selected,) = sources.select_crds([CRD])

    assert 'status' not in selected


def test_select_crds_deduplicates_by_name() -> None:
    """Two sources may legitimately ship the same definition; `crd2pulumi` may not see it twice."""
    selected = sources.select_crds([CRD, CRD])

    assert len(selected) == 1


def test_select_crds_orders_by_name() -> None:
    other = CRD.replace('widgets.example.com', 'anvils.example.com')

    selected = sources.select_crds([CRD, other])

    assert [crd['metadata']['name'] for crd in selected] == ['anvils.example.com', 'widgets.example.com']


def test_select_crds_tolerates_empty_documents() -> None:
    """A rendered chart whose values disabled everything is a stream of nothing."""
    assert sources.select_crds(['---\n# Source: chart/templates/empty.yaml\n---\n']) == []


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
