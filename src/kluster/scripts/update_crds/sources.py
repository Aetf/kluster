"""Getting CRD YAML out of the pinned sources, without a cluster.

Three shapes, because upstream ships CRDs three ways: inside a chart, as a
release asset, and — Cilium — as YAML that exists only in the source tree.
None of them reads a live cluster: what the bundle describes is the chart set
this repository pins, not whatever happens to be installed somewhere.

The pins are read out of `Pulumi.yaml` (`read_project`), through the parser
the stack program reads them with, so the script and the program cannot
disagree on a pin's shape.
"""

# `ruamel.yaml` and `tqdm` are only partially typed, and this module is mostly
# glue over both.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess as sp
import tarfile
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path
from typing import IO, Any, cast

import requests
from ruamel.yaml import YAML
from tqdm import tqdm

from kluster.lib.versions import ChartPin, ProjectFile
from kluster.scripts.update_crds import pins
from kluster.scripts.update_crds.pins import SourceTree

log = logging.getLogger(__name__)

#: Anything else in a rendered chart is a workload, and not our business.
CRD_KIND = 'CustomResourceDefinition'

#: The file names a source tree contributes. Anything else in a definitions
#: directory is a README or a script.
YAML_SUFFIXES = ('.yaml', '.yml')

_GITHUB_API = 'https://api.github.com'


class SourceError(RuntimeError):
    """A fetched document is not shaped the way the thing it claims to be is."""


class BelowFloor(RuntimeError):
    """A chart pin ships an operator older than the floor its design document states."""


@dataclass(frozen=True)
class Definition:
    """One CustomResourceDefinition, with the two fields the selection turns on.

    The document is carried whole because the bundle carries it whole, but
    nothing downstream indexes it again: what the pipeline decides with —
    which group a definition belongs to, and which name makes it a duplicate —
    is read once, where the YAML stops being untyped.
    """

    name: str
    group: str
    document: dict[str, Any]


@contextmanager
def _progress_read(fileobj: IO[bytes], /, *, desc: str, total: int) -> Generator[Any, None, None]:
    """`fileobj` with a byte-counting progress bar wrapped around its reads."""
    with tqdm.wrapattr(
        fileobj, 'read', unit='B', unit_scale=True, unit_divisor=1024, miniters=1, desc=desc, total=total
    ) as wrapped:
        yield wrapped


#: Wider than any line a definition holds, so the dumper never folds one.
UNFOLDED = 2**31 - 1


def _yaml() -> YAML:
    """A loader that keeps key order and refuses the round-trip machinery, and a dumper that folds nothing.

    CRD schemas are large and nothing here edits them in place, so the
    round-trip representer's comment bookkeeping is pure cost. The width is
    what keeps a dumped bundle a fixed point of the selection: folding a plain
    scalar, the dumper breaks a line inside a run of spaces, and the loader
    reads the spaces left at the end of that line as nothing -- so a
    description written `from.  Must` would come back `from. Must`, and the
    bundle would not be the text the script writes from it.
    """
    yaml = YAML(typ='safe')
    yaml.default_flow_style = False
    yaml.width = UNFOLDED
    return yaml


# --- Tools ----------------------------------------------------------------


def _fetch_tool(workdir: Path, *, binary: str, version: str, url: str, sha256: str) -> Path:
    """A pinned tool archive, unpacked under `workdir` once its digest matches.

    The archive is held whole rather than unpacked straight out of the
    response: a digest only checks anything if nothing is extracted before it
    matches. A mismatch names both digests, because the one thing the caller
    has to decide is whether the pin is stale or the download is not the
    artifact it claims to be.
    """
    log.info(f'Downloading {binary} {version} from {url}')
    with requests.get(url, stream=True, timeout=60) as response:
        _ = response.raise_for_status()
        total = int(response.headers.get('content-length', 0))
        with _progress_read(response.raw, desc=f'Downloading {binary} {version}', total=total) as stream:
            archive = stream.read()

    digest = hashlib.sha256(archive).hexdigest()
    if digest != sha256:
        raise ValueError(f'{binary} {version} digest is {digest}, expected {sha256}')

    with tarfile.open(fileobj=BytesIO(archive), mode='r:gz') as tarobj:
        tarobj.extractall(workdir, filter='data')

    found = _extracted_binary(workdir, binary, source=url)
    log.info(f'{binary} binary: {found}')
    return found


def fetch_helm(workdir: Path) -> Path:
    """The pinned Helm 3 binary, verified against its published digest."""
    return _fetch_tool(workdir, binary='helm', version=pins.HELM_VERSION, url=pins.HELM_URL, sha256=pins.HELM_SHA256)


def _extracted_binary(workdir: Path, name: str, *, source: str) -> Path:
    """The one executable called `name` an unpacked archive left under `workdir`.

    An upstream that changes an archive's layout is refused by name here: the
    bare search would otherwise end in a `StopIteration` raised inside a
    generator, which says neither what was looked for nor where it came from.
    """
    found = next((path for path in workdir.rglob(name) if path.is_file() and os.access(path, os.X_OK)), None)
    if found is None:
        raise SourceError(f'{source} unpacked no executable called {name}')
    return found.resolve()


# --- The project file -------------------------------------------------------


def read_config(path: Path, *, required: bool = True) -> dict[str, object]:
    """The `config:` block of the Pulumi project or stack file at `path`, loaded.

    A stack file `pulumi stack init` has just written holds no block yet,
    which is a file configuring nothing rather than a broken one: `required`
    says which reading the caller wants.
    """
    document = _yaml().load(path.read_text())
    config = document.get('config') if isinstance(document, dict) else None
    if config is None and not required:
        return {}
    if not isinstance(config, dict):
        raise SourceError(f'{path} has no `config:` block')
    return cast('dict[str, object]', config)


def read_project(path: Path) -> ProjectFile:
    """The `versions:` block of the `Pulumi.yaml` at `path`, as `kluster.lib.versions` reads it.

    The file rather than `pulumi config`: the CLI answers only for a selected
    stack, which means reaching the state backend and holding its passphrase,
    and a pin needs neither.
    """
    return ProjectFile(read_config(path))


#: The `packages:` entry of `Pulumi.yaml` the bundle is the manifest of, and
#: the extension parameter of that entry that names the manifest's path.
EXTENSION = 'crds'
MANIFEST_PARAMETER = 'crd-manifest'


def bundle_path(path: Path) -> Path:
    """The CRD manifest the `packages:` entry of the `Pulumi.yaml` at `path` generates its SDK from.

    The entry is the one place that names it: `pulumi install` reads the
    manifest from there, relative to the project, so the script writes the
    bundle to that path rather than to one of its own. Refused by name when the
    entry is missing or names no manifest, or more than one.
    """
    document = _yaml().load(path.read_text())
    packages = document.get('packages') if isinstance(document, dict) else None
    entry = packages.get(EXTENSION) if isinstance(packages, dict) else None
    extensions = entry.get('extensions') if isinstance(entry, dict) else None
    if not isinstance(extensions, list):
        raise SourceError(f'{path} has no `packages:` entry `{EXTENSION}` carrying `extensions:`')
    manifests = [
        value
        for item in extensions
        if isinstance(item, str)
        for key, separator, value in [item.partition('=')]
        if key == MANIFEST_PARAMETER and separator and value
    ]
    if len(manifests) != 1:
        raise SourceError(
            f'the `packages:` entry `{EXTENSION}` of {path} names {len(manifests)} `{MANIFEST_PARAMETER}=` paths, not one'
        )
    return path.parent / manifests[0]


# --- Charts ---------------------------------------------------------------


def _chart_location(pin: ChartPin) -> list[str]:
    """The arguments that make Helm fetch exactly the pinned chart.

    An OCI chart by its digest-pinned reference, which Helm pulls by digest and
    refuses if the version's tag names any other; an HTTP chart by name within
    its repository, at its version.
    """
    location = [pin.reference, '--version', pin.version]
    if not pin.oci:
        location += ['--repo', pin.repository]
    return location


def _helm(helm: Path, arguments: list[str], *, workdir: Path) -> str:
    """Run `helm` with its own state under `workdir`, and return what it prints.

    Helm writes its repository cache and its configuration under these; left
    to their defaults it would read, and dirty, the caller's own Helm state.
    """
    environment = dict(
        os.environ,
        HELM_CONFIG_HOME=str(workdir / 'helm-config'),
        HELM_CACHE_HOME=str(workdir / 'helm-cache'),
        HELM_DATA_HOME=str(workdir / 'helm-data'),
    )
    return sp.check_output([str(helm), *arguments], env=environment, text=True)


def render_chart(helm: Path, pin: ChartPin, *, workdir: Path) -> str:
    """A chart's manifests, rendered offline.

    `--include-crds` is what reaches the chart's `crds/` directory, which Helm
    otherwise never templates; CRDs a chart ships as ordinary templates
    (cert-manager) come out of the same render as everything else, once the
    pin's `render-values` ask for them. Both forms end up in this one document
    stream.

    The render is values-aware, so a subchart the values disable contributes
    nothing — which is the reason to prefer it over `helm show crds`.
    """
    command = ['template', pin.name, *_chart_location(pin), '--namespace', 'render', '--include-crds']
    for key, value in pin.render_values.items():
        command += ['--set', f'{key}={value}']

    log.info(f'Rendering chart {pin.name} {pin.version} from {pin.repository} (downloads the chart)')
    return _helm(helm, command, workdir=workdir)


def chart_app_version(helm: Path, pin: ChartPin, *, workdir: Path) -> str:
    """The operator version a pinned chart declares, its `Chart.yaml`'s `appVersion`."""
    log.info(f'Reading the operator version chart {pin.name} {pin.version} declares (downloads the chart)')
    metadata = _yaml().load(_helm(helm, ['show', 'chart', *_chart_location(pin)], workdir=workdir))
    app_version = metadata.get('appVersion') if isinstance(metadata, dict) else None
    if not isinstance(app_version, str) or not app_version:
        raise SourceError(f'chart {pin.name} {pin.version} declares no appVersion to check its floor against')
    return app_version


def version_tuple(version: str) -> tuple[int, ...]:
    """The numeric components of a version, for comparing one against a floor.

    Upstream is not consistent about the leading `v` (`v1.21.1` and `1.20.1`
    are both pinned), and a floor is written as far as it is meaningful —
    `1.26` covers every `1.26.x`. Comparing tuples handles both, and stops at
    the first non-numeric component so a pre-release suffix cannot make a
    version sort below the release it precedes.
    """
    components: list[int] = []
    for part in version.lstrip('vV').split('.'):
        match = re.match(r'\d+', part)
        if match is None:
            break
        components.append(int(match.group()))
    return tuple(components)


def check_floor(pin: ChartPin, app_version: str) -> None:
    """Refuse a chart whose operator is older than the floor its pin states.

    Checked against the version the chart itself declares rather than one
    written beside the pin, so a bump cannot leave a hand-kept operator version
    stale and pass on it.
    """
    floor = pin.floor
    if floor is None:
        return
    if version_tuple(app_version) < version_tuple(floor.operator):
        raise BelowFloor(
            f'versions:chart-{pin.name} {pin.version} ships operator {app_version}, '
            f'below its floor {floor.operator}: {floor.document}'
        )
    log.info(f'Chart {pin.name} {pin.version} ships operator {app_version}, clearing its floor {floor.operator}')


# --- Source trees -----------------------------------------------------------


def fetch_source_tree(tree: SourceTree, ref: str) -> list[str]:
    """Every YAML file under the pinned directories of a source repository, at `ref`.

    Listed through the contents API rather than by a hard-coded file list: the
    set of definitions changes between releases, and a stale list would drop
    one silently.
    """
    documents: list[str] = []
    for path in tree.paths:
        log.info(f'Listing {tree.repo}@{ref}:{path}')
        listing = requests.get(
            f'{_GITHUB_API}/repos/{tree.repo}/contents/{path}',
            params={'ref': ref},
            headers={'Accept': 'application/vnd.github+json'},
            timeout=60,
        )
        _ = listing.raise_for_status()
        urls = yaml_file_urls(listing.json(), what=f'{tree.repo}@{ref}:{path}')

        log.info(f'Downloading {len(urls)} CRD files from {tree.repo}@{ref}:{path}')
        for url in tqdm(urls, desc=f'{tree.repo}:{path}'):
            file = requests.get(url, timeout=60)
            _ = file.raise_for_status()
            documents.append(file.text)
    return documents


def yaml_file_urls(listing: object, *, what: str) -> list[str]:
    """The download URLs of the YAML files in a GitHub contents listing.

    The boundary for that API: `what` names the directory that was listed, so
    a listing that is not a listing — a rate-limit object, a file where a
    directory was expected — is refused by name here rather than raising a
    `KeyError` over an entry nobody can identify.

    **The suffix decides before the URL is required.** A contents listing
    holds directories and submodules as well as files, and those carry
    `download_url: null` — a legitimate shape, and one this function wants
    none of. So the name is what every entry must have, and the URL is
    demanded only of the entries that survive the filter.
    """
    if not isinstance(listing, list):
        raise SourceError(f'{what}: the contents API answered a {type(listing).__name__}, not a list of entries')
    urls: list[str] = []
    for entry in cast('list[Any]', listing):
        name = entry.get('name') if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            raise SourceError(f'{what}: an entry carries no name, and is {entry!r}')
        if not name.endswith(YAML_SUFFIXES):
            continue
        url = entry.get('download_url')
        if not isinstance(url, str) or not url:
            raise SourceError(f'{what}: the entry {name} carries no download_url, and is {entry!r}')
        urls.append(url)
    return urls


# --- Selection ------------------------------------------------------------


def definition(document: dict[str, Any]) -> Definition:
    """A parsed CRD document as a `Definition`, or a refusal naming what it lacks.

    The boundary between YAML and this program: a definition without a name
    or a group cannot be deduplicated, filtered or generated from, so it is
    rejected here rather than three steps later where the traceback would
    name a dictionary instead of a document.
    """
    metadata = document.get('metadata')
    spec = document.get('spec')
    name = metadata.get('name') if isinstance(metadata, dict) else None
    group = spec.get('group') if isinstance(spec, dict) else None
    if not isinstance(name, str) or not name:
        raise SourceError(f'a {CRD_KIND} document has no metadata.name, and holds {sorted(document)}')
    if not isinstance(group, str) or not group:
        raise SourceError(f'{CRD_KIND} {name} has no spec.group')
    return Definition(name=name, group=group, document=document)


def select_crds(documents: Iterable[str]) -> list[Definition]:
    """The CustomResourceDefinitions the bundle carries, in the order it carries them.

    Pure, so what it decides is testable without the network: keep only CRDs,
    drop the groups `pins.DROPPED_GROUPS` names, drop the `status` a cluster
    would have written, and keep the first definition of any name — the same
    CRD can legitimately arrive from two sources, and the bundle holds each
    once. Ordered by name, so the bundle is a function of what was selected and
    not of the order the sources were fetched in.
    """
    yaml = _yaml()
    selected: dict[str, Definition] = {}
    for document in documents:
        for item in yaml.load_all(document):
            if not isinstance(item, dict) or item.get('kind') != CRD_KIND:
                continue
            crd = definition(cast('dict[str, Any]', item))
            if crd.group in pins.DROPPED_GROUPS or crd.name in selected:
                continue
            _ = crd.document.pop('status', None)
            selected[crd.name] = crd
    return [selected[name] for name in sorted(selected)]


def dump_bundle(crds: Iterable[Definition]) -> str:
    """The selected CRDs as one multi-document YAML stream: the manifest the extension is generated from.

    `select_crds` of what this returns selects the same definitions and dumps
    to the same text, so a bundle this wrote is a fixed point of the script; a
    test holds the committed bundle to that.
    """
    yaml = _yaml()
    buffer = StringIO()
    yaml.dump_all([crd.document for crd in crds], buffer)
    return buffer.getvalue()
