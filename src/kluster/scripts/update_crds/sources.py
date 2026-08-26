"""Getting CRD YAML out of the pinned sources, without a cluster.

Three shapes, because upstream ships CRDs three ways: inside a chart, as a
release asset, and — Cilium — as YAML that exists only in the source tree.
None of them reads a live cluster: what the bindings describe is the chart set
this repository pins, not whatever happens to be installed somewhere.
"""

# `ruamel.yaml` and `tqdm` are only partially typed, and this module is mostly
# glue over both.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import logging
import os
import subprocess as sp
import tarfile
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from io import BytesIO, StringIO
from pathlib import Path
from typing import IO, Any

import requests
from ruamel.yaml import YAML
from tqdm import tqdm

from kluster.scripts.update_crds import pins
from kluster.scripts.update_crds.pins import Chart, ReleaseManifest, SourceTree

log = logging.getLogger('kluster.scripts.update_crds')

#: Anything else in a rendered chart is a workload, and not our business.
CRD_KIND = 'CustomResourceDefinition'

_GITHUB_API = 'https://api.github.com'


@contextmanager
def _progress_read(fileobj: IO[bytes], /, **kwargs: Any) -> Generator[Any, None, None]:
    kwargs = {'unit': 'B', 'unit_scale': True, 'unit_divisor': 1024, 'miniters': 1, **kwargs}
    with tqdm.wrapattr(fileobj, 'read', **kwargs) as wrapped:
        yield wrapped


def _yaml() -> YAML:
    """A loader that keeps key order and refuses the round-trip machinery.

    CRD schemas are large and nothing here edits them in place, so the
    round-trip representer's comment bookkeeping is pure cost.
    """
    yaml = YAML(typ='safe')
    yaml.default_flow_style = False
    return yaml


# --- Tools ----------------------------------------------------------------


def fetch_helm(workdir: Path) -> Path:
    """The pinned Helm 3 binary, verified against its published digest."""
    log.info(f'Downloading Helm {pins.HELM_VERSION} from {pins.HELM_URL}')
    with requests.get(pins.HELM_URL, stream=True, timeout=60) as response:
        _ = response.raise_for_status()
        total = int(response.headers.get('content-length', 0))
        with _progress_read(response.raw, desc=f'Downloading helm {pins.HELM_VERSION}', total=total) as stream:  # pyright: ignore[reportArgumentType]
            archive = stream.read()

    digest = hashlib.sha256(archive).hexdigest()
    if digest != pins.HELM_SHA256:
        raise ValueError(f'helm {pins.HELM_VERSION} digest is {digest}, expected {pins.HELM_SHA256}')

    with tarfile.open(fileobj=BytesIO(archive), mode='r:gz') as tarobj:
        tarobj.extractall(workdir, filter='data')
    helm = next(path for path in workdir.rglob('helm') if path.is_file() and os.access(path, os.X_OK))
    log.info(f'Helm binary: {helm}')
    return helm.resolve()


def fetch_crd2pulumi(workdir: Path) -> Path:
    """The pinned `crd2pulumi` binary."""
    url = (
        f'https://github.com/pulumi/crd2pulumi/releases/download/{pins.CRD2PULUMI_VERSION}'
        f'/crd2pulumi-{pins.CRD2PULUMI_VERSION}-linux-amd64.tar.gz'
    )
    log.info(f'Downloading crd2pulumi {pins.CRD2PULUMI_VERSION} from {url}')
    with requests.get(url, stream=True, timeout=60) as response:
        _ = response.raise_for_status()
        total = int(response.headers.get('content-length', 0))
        with _progress_read(response.raw, desc=f'Downloading crd2pulumi {pins.CRD2PULUMI_VERSION}', total=total) as f:  # pyright: ignore[reportArgumentType]
            with tarfile.open(fileobj=f, mode='r:gz') as tarobj:
                tarobj.extractall(workdir, filter='data')

    binary = next(path for path in workdir.rglob('crd2pulumi') if path.is_file() and os.access(path, os.X_OK))
    log.info(f'crd2pulumi binary: {binary}')
    return binary.resolve()


# --- Sources --------------------------------------------------------------


def render_chart(helm: Path, chart: Chart, *, workdir: Path) -> str:
    """A chart's manifests, rendered offline.

    `--include-crds` is what reaches the chart's `crds/` directory, which Helm
    otherwise never templates; CRDs a chart ships as ordinary templates
    (cert-manager) come out of the same render as everything else. Both forms
    end up in this one document stream.

    The render is values-aware, so a subchart the values disable contributes
    nothing — which is the reason to prefer it over `helm show crds`.
    """
    command = [
        str(helm),
        'template',
        chart.name,
        chart.name,
        '--repo',
        chart.repo,
        '--version',
        chart.version,
        '--namespace',
        'render',
        '--include-crds',
    ]
    for key, value in chart.values.items():
        command += ['--set', f'{key}={value}']

    log.info(f'Rendering chart {chart.name} {chart.version} from {chart.repo} (downloads the chart)')
    # Helm writes its repository cache and its configuration under these; left
    # to their defaults it would read, and dirty, the caller's own Helm state.
    environment = dict(
        os.environ,
        HELM_CONFIG_HOME=str(workdir / 'helm-config'),
        HELM_CACHE_HOME=str(workdir / 'helm-cache'),
        HELM_DATA_HOME=str(workdir / 'helm-data'),
    )
    return sp.check_output(command, env=environment, text=True)


def fetch_release_manifest(manifest: ReleaseManifest) -> str:
    """A YAML bundle published as a release asset."""
    url = f'https://github.com/{manifest.repo}/releases/download/{manifest.tag}/{manifest.asset}'
    log.info(f'Downloading {manifest.repo} {manifest.tag} {manifest.asset}')
    response = requests.get(url, timeout=60)
    _ = response.raise_for_status()
    return response.text


def fetch_source_tree(tree: SourceTree) -> list[str]:
    """Every YAML file under the pinned directories of a source repository.

    Listed through the contents API rather than by a hard-coded file list: the
    set of definitions changes between releases, and a stale list would drop
    one silently.
    """
    documents: list[str] = []
    for path in tree.paths:
        log.info(f'Listing {tree.repo}@{tree.ref}:{path}')
        listing = requests.get(
            f'{_GITHUB_API}/repos/{tree.repo}/contents/{path}',
            params={'ref': tree.ref},
            headers={'Accept': 'application/vnd.github+json'},
            timeout=60,
        )
        _ = listing.raise_for_status()
        entries = [entry for entry in listing.json() if entry['name'].endswith(('.yaml', '.yml'))]

        log.info(f'Downloading {len(entries)} CRD files from {tree.repo}@{tree.ref}:{path}')
        for entry in tqdm(entries, desc=f'{tree.repo}:{path}'):
            file = requests.get(entry['download_url'], timeout=60)
            _ = file.raise_for_status()
            documents.append(file.text)
    return documents


# --- Selection ------------------------------------------------------------


def select_crds(documents: Iterable[str]) -> list[dict[str, Any]]:
    """The CustomResourceDefinitions worth generating bindings from.

    Pure, so what it decides is testable without the network: keep only CRDs,
    drop the groups `pins.DROPPED_GROUPS` names, drop the `status` a cluster
    would have written, and keep the first definition of any name — the same
    CRD can legitimately arrive from two sources, and generating it twice is
    what `crd2pulumi` cannot do.
    """
    yaml = _yaml()
    selected: dict[str, dict[str, Any]] = {}
    for document in documents:
        for item in yaml.load_all(document):
            if not isinstance(item, dict) or item.get('kind') != CRD_KIND:
                continue
            crd: dict[str, Any] = item
            group = crd['spec']['group']
            if group in pins.DROPPED_GROUPS:
                continue
            name = crd['metadata']['name']
            if name in selected:
                continue
            _ = crd.pop('status', None)
            selected[name] = crd
    return [selected[name] for name in sorted(selected)]


def write_crd_files(crds: Iterable[dict[str, Any]], directory: Path) -> list[Path]:
    """One file per CRD, because `crd2pulumi` cannot read a multi-document one."""
    yaml = _yaml()
    files: list[Path] = []
    for index, crd in enumerate(crds):
        file = directory / f'crd_{index:03d}.yaml'
        with file.open('w') as handle:
            yaml.dump(crd, handle)
        files.append(file)
    return files


def dump_bundle(crds: Iterable[dict[str, Any]]) -> str:
    """The selected CRDs as one multi-document YAML stream."""
    yaml = _yaml()
    buffer = StringIO()
    yaml.dump_all(crds, buffer)
    return buffer.getvalue()
