"""Regenerate the CRD bundle and the SDK generated from it, from the pinned chart set.

`mise x -- uv run update_crds`, which puts the `pulumi` CLI `mise.toml` pins on
`PATH`. The bundle (`packages/crds/crds.yaml`) and the SDK (`sdks/crds`) are
generated, not written, so this is the only supported way to change anything
under either. The pins are the `versions:` block of `Pulumi.yaml`; the run
reads the operator version each chart it reads declares and checks the floors,
reads the value paths every pinned chart has, renders the definitions, writes
the bundle to the path the same file's `packages:` entry names, writes beside it
the record of the pins it read, of the operator versions the charts declare and
of the charts' value paths (`record`), and regenerates the SDK from that entry.
"""

# `tqdm` is only partially typed.
# pyright: reportUnknownMemberType=false

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess as sp
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from tqdm.contrib.logging import logging_redirect_tqdm

from kluster.lib.release_assets import fetch_manifest
from kluster.lib.versions import CHART, MANIFEST, ProjectFile, Versions
from kluster.scripts.update_crds import pins, record, sources
from kluster.scripts.update_crds.values import ValuePaths

if TYPE_CHECKING:
    from collections.abc import Generator

#: The package logger `console` attaches the console to. Every module here
#: logs to `__name__`, which is a child of it, so the handler and the level
#: are stated once.
LOG_NAME = 'kluster.scripts.update_crds'

#: How a line the package logs reads on the console.
LINE_FORMAT = '%(asctime)s %(levelname)s: %(message)s'
DATE_FORMAT = '%Y-%m-%d - %H:%M:%S'

#: Spelled out rather than taken from `__name__`, which is `'__main__'` when
#: this file is run directly and would then sit outside the tree `console`
#: configures -- so a direct run would print nothing. Sibling modules take
#: `__name__`, which for them is always a child of `LOG_NAME`.
log = logging.getLogger(f'{LOG_NAME}.cli')


def collect_documents(workdir: Path, project: ProjectFile) -> tuple[list[str], dict[str, str], dict[str, ValuePaths]]:
    """Every YAML document the pinned sources produce, unfiltered, the operator version each chart declares,
    and the value paths every pinned chart has.

    The operator version is the `appVersion` of every chart the script reads
    (`record.read_charts`), keyed by the chart's name. It is read first, and
    each chart's floor checked against it, before anything is rendered, so a
    pin below one stops the run before it has fetched the rest. The value
    paths are read off every chart pin, keyed by name, whether or not the
    script renders anything from it (`record.chart_values`). Fetching is
    announced step by step because all of it is network: a chart set this size
    takes a couple of minutes, and a silent one looks hung.
    """
    versions = Versions(project)
    charts = [versions.chart[name] for name in project.names(CHART)]
    manifests = [versions.manifest[name] for name in project.names(MANIFEST)]
    rendered = [chart for chart in charts if chart.definitions]
    read = [versions.chart[name] for name in record.read_charts(project)]
    log.info(
        f'Collecting from {len(rendered)} charts, '
        f'{len(manifests)} release manifests and {len(pins.SOURCE_TREES)} source trees'
    )

    helm = sources.fetch_helm(workdir)
    log.info(f'Reading the operator versions of {len(read)} charts and checking their floors')
    declared: dict[str, str] = {}
    for chart in read:
        declared[chart.name] = sources.chart_app_version(helm, chart, workdir=workdir)
        sources.check_floor(chart, declared[chart.name])
    log.info(f'Reading the value paths of {len(charts)} charts')
    value_paths = {chart.name: sources.chart_value_paths(helm, chart, workdir=workdir) for chart in charts}

    # One source at a time and `extend` throughout: a release manifest is one
    # document and a source tree is many, and the fetches stay sequential so
    # that the log above stays a running commentary rather than a summary.
    documents: list[str] = []
    for manifest in manifests:
        log.info(f'Downloading {manifest.repository} {manifest.release} {manifest.asset}')
        documents.append(fetch_manifest(manifest))
    for tree in pins.SOURCE_TREES:
        documents.extend(sources.fetch_source_tree(tree, tree.ref(versions.chart[tree.chart])))
    documents.extend(sources.render_chart(helm, chart, workdir=workdir) for chart in rendered)
    return documents, declared, value_paths


class NoPulumi(RuntimeError):
    """The `pulumi` CLI the SDK is generated with is not on `PATH`."""


def find_pulumi() -> Path:
    """The `pulumi` CLI on `PATH`, refused by name before anything is fetched.

    Asked before the render rather than after it, because the render is a
    couple of minutes of network that a missing CLI would throw away.
    """
    found = shutil.which('pulumi')
    if found is None:
        raise NoPulumi(
            'no `pulumi` on PATH to generate the SDK with; run `mise x -- uv run update_crds`, '
            'which puts the CLI mise.toml pins there'
        )
    return Path(found)


def generate(project_file: Path, *, pulumi: Path, workdir: Path) -> None:
    """Regenerate every SDK the `packages:` block of `project_file` declares, `sdks/crds` among them.

    `pulumi install` generates each entry into `sdks/<name>`, replacing the
    tree whole, so a group that left the bundle leaves the SDK with it. The
    bridged SDKs come out of it byte for byte as they went in, since their
    entries did not move. Two of its effects are not a regeneration, and the
    run undoes them:

    -   It links each SDK through `uv add`, which rewrites `[tool.uv.sources]`
        in `pyproject.toml` (the entries come back in another order). The file
        gets back the bytes it had, whether or not the command succeeded,
        because a `jj` working copy would otherwise snapshot the rewrite into
        the change.
    -   It re-locks `uv.lock` with whichever `uv` it finds. The lock is made
        again by the `uv` on `PATH`, which under `mise x` is the pinned one.

    The command reads the block and the manifest and nothing else, so it is
    handed an empty `file://` backend of its own: the CLI answers a package
    lookup through the current backend, and with none logged in, in an
    environment it takes for a coding agent's, it signs up an account on
    Pulumi Cloud to answer it (`currentOrSignupAgentAccount` in
    `pkg/backend/httpstate/backend.go`, pulumi v3.267.0). A `file://` backend
    answers with the unauthenticated registry instead
    (`GetReadOnlyCloudRegistry` in `pkg/backend/diy/backend.go`), and never
    reaches the backend a caller's environment names.
    """
    project = project_file.resolve().parent
    pyproject = project / 'pyproject.toml'
    before = pyproject.read_bytes()
    backend = workdir / 'backend'
    backend.mkdir()
    environment = dict(os.environ, PULUMI_BACKEND_URL=backend.as_uri(), PULUMI_SKIP_UPDATE_CHECK='true')

    log.info('Regenerating the SDKs of the packages block with pulumi install (downloads the provider plugins)')
    try:
        _ = sp.check_call(
            [str(pulumi), '--non-interactive', 'install', '--no-dependencies', '--no-plugins'],
            cwd=project,
            env=environment,
        )
    except BaseException:
        log.error('pulumi install failed; the bundle is written and the SDK does not match it until a run succeeds')
        raise
    finally:
        _ = pyproject.write_bytes(before)

    log.info('Re-locking uv.lock')
    _ = sp.check_call(['uv', 'lock'], cwd=project)


@contextmanager
def console() -> Generator[None]:
    """The package's lines on stdout for the length of a run, and the process's logging as it was found afterwards.

    Set on the package logger alone, and by hand: `logging.config.dictConfig`
    is not incremental, so it would close every handler the process holds -- a
    test run's log file among them -- and what it set would outlast the run.
    `main` runs inside other processes as well as its own, a test run's
    included, so it attaches a handler, sets the level and stops propagation
    for the run, and puts back what it found when the run ends, however it
    ends. Propagation is off so that a process whose root logger prints does
    not print each line twice. The stream is the `sys.stdout` of the moment
    the run starts, rather than the one the module was imported under.

    Every line printed for the length of the run goes through `tqdm.write`,
    which clears the progress bars a download draws, prints the line and draws
    them again; printed straight to the stream, a line lands in the middle of
    a bar. Two loggers print during a run: the package logger, through the
    handler attached here, and the root logger, for every record from outside
    the package -- a library's warning among them -- which a process with no
    handler of its own prints through `logging.lastResort`. Both are
    redirected. `logging_redirect_tqdm` swaps each one's console handler for a
    writer of its own that keeps its formatter and stream, gives a logger that
    has none a writer to stderr, and puts the handlers back as it exits. Its
    default is the root logger alone, which the package's records never reach
    with propagation off, so the package logger is named beside it.
    """
    logger = logging.getLogger(LOG_NAME)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LINE_FORMAT, datefmt=DATE_FORMAT))
    level, propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        with logging_redirect_tqdm(loggers=[logging.root, logger]):
            yield
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(level)
        logger.propagate = propagate


def main(argv: list[str] | None = None) -> int:
    with console():
        return _run(argv)


def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        '--project',
        type=Path,
        default=Path('./Pulumi.yaml'),
        help='the project file whose `versions:` block pins the chart set (default: %(default)s)',
    )
    _ = parser.add_argument(
        '--bundle',
        type=Path,
        help='write the rendered CRD bundle here and stop, touching neither the committed bundle nor the SDK',
    )
    _ = parser.add_argument(
        '--from-bundle',
        type=Path,
        help=(
            'select from an already rendered bundle instead of fetching the pinned sources, and regenerate '
            'from it; no record is written, since the pins did not produce the bundle'
        ),
    )
    args = parser.parse_args(argv)

    project_file: Path = args.project
    bundle: Path | None = args.bundle
    from_bundle: Path | None = args.from_bundle

    # Only a run that regenerates needs the CLI, and it is asked for before
    # the render (`find_pulumi`).
    pulumi = find_pulumi() if bundle is None else None
    with TemporaryDirectory(prefix='update_crds-') as name:
        workdir = Path(name)
        log.info(f'Working directory: {workdir}')

        # The record is written from the block as it was read here, not as it
        # stands when the run ends a few minutes later.
        project: ProjectFile | None = None
        declared: dict[str, str] = {}
        value_paths: dict[str, ValuePaths] = {}
        if from_bundle is not None:
            log.info(f'Reading the rendered bundle from {from_bundle}')
            documents = [from_bundle.read_text()]
        else:
            log.info(f'Reading the pins in {project_file}')
            project = sources.read_project(project_file)
            documents, declared, value_paths = collect_documents(workdir, project)

        crds = sources.select_crds(documents)
        groups = sorted({crd.group for crd in crds})
        log.info(f'Selected {len(crds)} CRDs in {len(groups)} groups: {", ".join(groups)}')

        if bundle is not None:
            _ = bundle.write_text(sources.dump_bundle(crds))
            log.info(f'Wrote the bundle to {bundle}')
            return 0
        assert pulumi is not None, 'found before the render whenever no --bundle is given'

        target = sources.bundle_path(project_file)
        _ = target.write_text(sources.dump_bundle(crds))
        log.info(f'Wrote the bundle to {target}')
        if project is not None:
            written = record.write(project, target.parent)
            log.info(f'Recorded the pins the bundle was rendered from in {written}')
            written = record.write_app_versions(project, declared, target.parent)
            log.info(f'Recorded the operator versions the charts declare in {written}')
            written = record.write_chart_values(project, value_paths, target.parent)
            log.info(f'Recorded the value paths the charts have in {written}')
        generate(project_file, pulumi=pulumi, workdir=workdir)
        log.info('Regenerated the SDK from the bundle')
    return 0


if __name__ == '__main__':
    sys.exit(main())
