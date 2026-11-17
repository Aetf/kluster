"""A change to Cilium's values rolls its agent, its operator and its Envoy, held on the pinned chart as Helm renders it.

The agent, the operator and Envoy each read a ConfigMap the values are
rendered into when they start -- the agent and the operator `cilium-config`,
Envoy its bootstrap in `cilium-envoy-config` -- so a values change takes effect only through new pods,
and what makes new pods is a change to their pod templates
(`components.cilium.chart_values`). Whether the templates follow the
ConfigMaps is the chart's own templating, which no mock renders, so the case
renders the chart itself: the pinned Cilium chart, pulled by its digest with
the Helm `update_crds` pins and renders charts with, and rendered with the
component's values -- as they are, as they are again, and once for each
ConfigMap with one value changed that only that ConfigMap carries.

The render is Helm's own rather than an `up` on `tests/chart_engine.py`'s
harness, because the harness cannot render this chart as the stack installs
it: with no cluster the provider renders for Kubernetes v1.20.0, below the
chart's floor; the chart's Hubble certificates `lookup` an existing Secret,
which the provider refuses without a cluster to ask; and the harness hands its
chart no values. `helm template` renders for a supported release, answers a
`lookup` with nothing, and takes the values as a values file, the way the
provider is handed them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import process_sessions
import pytest
import yaml

from kluster.components.cilium import NAMESPACE, chart_values
from kluster.lib.versions import Versions
from kluster.scripts.update_crds import sources

ROOT = Path(__file__).parent.parent

#: How long one `helm` command may take before the case fails naming it: the
#: pull downloads the chart, a render reads it from disk. A stop-loss, which
#: nothing asserts on. On four cores the slowest, the pull, took 1.4 s, idle
#: and with four busy processes per core alike, and a render 0.2 s and 0.6 s.
COMMAND_TIMEOUT = 120

#: How long the case may take: Helm's download, the pull and every render,
#: above every command's own bound so that a stalled one fails naming itself
#: first. A stop-loss, which nothing asserts on. On four cores the case took
#: 2.7 s idle and 4.4 s with four busy processes per core, downloads included.
CASE_TIMEOUT = 300

#: The kinds whose pods a rollout replaces: every workload kind the chart can
#: render.
WORKLOAD_KINDS = frozenset({'DaemonSet', 'Deployment', 'StatefulSet'})

#: The keys a pod template names a ConfigMap under: a volume or a projected
#: volume's source, an environment variable's source, and `envFrom`.
CONFIG_MAP_REFERENCES = frozenset({'configMap', 'configMapKeyRef', 'configMapRef'})


@dataclass(frozen=True)
class Rolled:
    """A ConfigMap the values are rendered into, the workloads that read it, and a change only it carries."""

    config: tuple[str, str]
    workloads: tuple[tuple[str, str], ...]
    #: Values Helm lays over the component's, as a second values file: a value
    #: the ConfigMap carries, and that neither the workloads' pod templates nor
    #: the other ConfigMap read.
    change: Mapping[str, Any]


ROLLED = (
    Rolled(
        config=('ConfigMap', 'cilium-config'),
        workloads=(('DaemonSet', 'cilium'), ('Deployment', 'cilium-operator')),
        change={'debug': {'enabled': True}},
    ),
    # The bootstrap reads `envoy.log.format`, and no template does
    # (`files/cilium-envoy/configmap/bootstrap-config.yaml`).
    Rolled(
        config=('ConfigMap', 'cilium-envoy-config'),
        workloads=(('DaemonSet', 'cilium-envoy'),),
        change={'envoy': {'log': {'format': '[%l] %v'}}},
    ),
)


class Helm:
    """The pinned Helm, with its configuration and caches under a directory of the case's own."""

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._binary = sources.fetch_helm(workdir)

    def __call__(self, *arguments: str) -> str:
        """What `helm <arguments>` prints; a failure fails the case with Helm's own words."""
        env = os.environ | {
            'HELM_CONFIG_HOME': str(self._workdir / 'helm-config'),
            'HELM_CACHE_HOME': str(self._workdir / 'helm-cache'),
            'HELM_DATA_HOME': str(self._workdir / 'helm-data'),
        }
        ran = process_sessions.run([str(self._binary), *arguments], env=env, text=True, timeout=COMMAND_TIMEOUT)
        assert ran.returncode == 0, ran.stderr
        return cast('str', ran.stdout)


def pulled(helm: Helm, workdir: Path) -> Path:
    """The Cilium chart at the version and digest `Pulumi.yaml` pins, unpacked; the directory it is in."""
    pin = Versions(sources.read_project(ROOT / 'Pulumi.yaml')).chart['cilium']
    assert pin.oci, 'the chart is pulled by its digest, which only an OCI pin has'
    # The digest-pinned reference, which Helm refuses if the version's tag
    # names other bytes.
    _ = helm('pull', pin.reference, '--version', pin.version, '--untar', '--untardir', str(workdir))
    return workdir / pin.name


def values_file(values: Mapping[str, Any], path: Path) -> Path:
    """`values` written to `path`, for `--values`: JSON is YAML."""
    _ = path.write_text(json.dumps(values))
    return path


def rendered(helm: Helm, chart: Path, *values: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Each object `chart` renders, by kind and name, with the values files in order, a later one over an earlier."""
    flags = [argument for path in values for argument in ('--values', str(path))]
    text = helm('template', 'cilium', str(chart), '--namespace', NAMESPACE, *flags)
    documents = [
        cast('dict[str, Any]', document) for document in yaml.safe_load_all(text) if isinstance(document, dict)
    ]
    return {(str(document['kind']), str(document['metadata']['name'])): document for document in documents}


def config_maps_named(tree: object) -> set[str]:
    """The name of every ConfigMap `tree`, a pod template, refers to."""
    names: set[str] = set()
    if isinstance(tree, dict):
        for key, value in cast('dict[str, object]', tree).items():
            names |= config_maps_named(value)
            if key in CONFIG_MAP_REFERENCES and isinstance(value, dict) and 'name' in value:
                names.add(str(cast('dict[str, object]', value)['name']))
    elif isinstance(tree, list):
        for item in cast('list[object]', tree):
            names |= config_maps_named(item)
    return names


def outside_the_census(objects: Mapping[tuple[str, str], Mapping[str, Any]]) -> list[str]:
    """Each workload `objects` holds that no row of `ROLLED` rolls, and each rendered ConfigMap a workload reads that no row is about."""
    workloads = {key for key in objects if key[0] in WORKLOAD_KINDS}
    read = {
        ('ConfigMap', name)
        for key in workloads
        for name in config_maps_named(objects[key]['spec']['template'])
        if ('ConfigMap', name) in objects
    }
    rolled = {workload for row in ROLLED for workload in row.workloads}
    configs = {row.config for row in ROLLED}
    return [
        *(f'{kind} {name} is rolled by no row' for kind, name in sorted(workloads - rolled)),
        *(f"{kind} {name} is read and is no row's config" for kind, name in sorted(read - configs)),
    ]


@pytest.mark.timeout(CASE_TIMEOUT)
def test_a_values_change_rolls_what_reads_it_and_a_rerender_rolls_nothing(tmp_path: Path) -> None:
    """The pod templates move with the ConfigMap each workload reads, and with nothing else.

    The rows cover every workload the chart renders and every rendered
    ConfigMap one reads, so a workload a later value turns on is held as well,
    or fails here by name. A render of the same values twice is what every `up`
    that changes no value is; the chart's Hubble certificates are generated
    afresh on each such render here, which no pod template may follow.
    """
    helm = Helm(tmp_path)
    chart = pulled(helm, tmp_path / 'chart')
    values = values_file(chart_values(), tmp_path / 'values.json')
    base = rendered(helm, chart, values)
    again = rendered(helm, chart, values)

    assert outside_the_census(base) == []

    for index, rolled in enumerate(ROLLED):
        changed = rendered(helm, chart, values, values_file(rolled.change, tmp_path / f'change-{index}.json'))

        # The change is one the ConfigMap carries, and a render of the same
        # values writes the same ConfigMap.
        assert base[rolled.config]['data'] == again[rolled.config]['data'], rolled.config
        assert base[rolled.config]['data'] != changed[rolled.config]['data'], rolled.config

        for workload in rolled.workloads:
            assert base[workload]['spec']['template'] == again[workload]['spec']['template'], workload
            assert base[workload]['spec']['template'] != changed[workload]['spec']['template'], workload
