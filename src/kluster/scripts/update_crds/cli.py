"""Regenerate `packages/crds` from the pinned chart set.

`uv run update_crds`. The bindings are generated, not written, so this is the
only supported way to change anything under `packages/crds`.
"""

# `tqdm` is only partially typed, and `pulumi_kubernetes._utilities` is where
# the SDK keeps its plugin version, with no public equivalent.
# pyright: reportUnknownMemberType=false, reportPrivateUsage=false

from __future__ import annotations

import argparse
import logging
import logging.config
import shutil
import subprocess as sp
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pulumi_kubernetes
from tqdm.contrib.logging import logging_redirect_tqdm

from kluster.scripts.update_crds import pins, sources

#: The package logger the configuration below attaches the console to. Every
#: module here logs to `__name__`, which is a child of it, so the handler and
#: the level are stated once.
LOG_NAME = 'kluster.scripts.update_crds'

LOGGING = {
    'version': 1,
    'formatters': {
        'standard': {'format': '%(asctime)s %(levelname)s: %(message)s', 'datefmt': '%Y-%m-%d - %H:%M:%S'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'standard', 'level': 'DEBUG', 'stream': sys.stdout},
    },
    'loggers': {
        LOG_NAME: {'level': 'INFO', 'handlers': ['console'], 'propagate': False},
    },
}

#: Spelled out rather than taken from `__name__`, which is `'__main__'` when
#: this file is run directly and would then sit outside the tree configured
#: above -- so a direct run would print nothing. Sibling modules take
#: `__name__`, which for them is always a child of `LOG_NAME`.
log = logging.getLogger(f'{LOG_NAME}.cli')


def collect_documents(workdir: Path) -> list[str]:
    """Every YAML document the pinned sources produce, unfiltered.

    Fetching is announced step by step because all of it is network: a chart
    set this size takes a couple of minutes, and a silent one looks hung.
    """
    charts = [chart for chart in pins.CHARTS if chart.crds]
    log.info(
        f'Collecting from {len(charts)} charts, '
        f'{len(pins.MANIFESTS)} release manifests and {len(pins.SOURCE_TREES)} source trees'
    )

    # One source at a time and `extend` throughout: a release manifest is one
    # document and a source tree is many, and the fetches stay sequential so
    # that the log above stays a running commentary rather than a summary.
    documents: list[str] = []
    documents.extend(sources.fetch_release_manifest(manifest) for manifest in pins.MANIFESTS)
    for tree in pins.SOURCE_TREES:
        documents.extend(sources.fetch_source_tree(tree))

    helm = sources.fetch_helm(workdir)
    documents.extend(sources.render_chart(helm, chart, workdir=workdir) for chart in charts)
    return documents


def generate(crd_files: list[Path], output: Path, crd2pulumi: Path) -> None:
    """Replace `output` with bindings for exactly `crd_files`.

    The old tree is moved aside rather than merged into: a group that left the
    chart set has to disappear, and a generator that only ever adds would keep
    retired bindings alive forever.
    """
    backup = output.with_suffix('.bak')
    log.info(f'Moving the existing bindings aside to {backup}')
    shutil.rmtree(backup, ignore_errors=True)
    output.replace(backup)

    # The bindings declare the SDK they were generated against, which is the
    # one this environment resolves rather than one written down twice.
    sdk_version = pulumi_kubernetes._utilities.get_version()
    log.info(f'Generating bindings for {len(crd_files)} CRDs against pulumi-kubernetes {sdk_version}')
    try:
        _ = sp.check_call(
            [str(crd2pulumi), '--python', '--pythonPath', str(output), '--version', sdk_version]
            + [str(file) for file in crd_files]
        )
    except BaseException:
        log.error('Generation failed; restoring the previous bindings')
        shutil.rmtree(output, ignore_errors=True)
        backup.replace(output)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    logging.config.dictConfig(LOGGING)
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        '--output',
        type=Path,
        default=Path('./packages/crds'),
        help='the bindings package to replace (default: %(default)s)',
    )
    _ = parser.add_argument(
        '--bundle',
        type=Path,
        help='write the rendered CRD bundle here and stop, without generating bindings',
    )
    _ = parser.add_argument(
        '--from-bundle',
        type=Path,
        help='generate from an already rendered bundle instead of fetching the pinned sources',
    )
    args = parser.parse_args(argv)

    output: Path = args.output
    bundle: Path | None = args.bundle
    from_bundle: Path | None = args.from_bundle

    with logging_redirect_tqdm():
        with TemporaryDirectory(prefix='update_crds-') as name:
            workdir = Path(name)
            log.info(f'Working directory: {workdir}')

            if from_bundle is not None:
                log.info(f'Reading the rendered bundle from {from_bundle}')
                documents = [from_bundle.read_text()]
            else:
                documents = collect_documents(workdir)

            crds = sources.select_crds(documents)
            groups = sorted({crd.group for crd in crds})
            log.info(f'Selected {len(crds)} CRDs in {len(groups)} groups: {", ".join(groups)}')

            if bundle is not None:
                _ = bundle.write_text(sources.dump_bundle(crds))
                log.info(f'Wrote the bundle to {bundle}')
                return 0

            crd_files = sources.write_crd_files(crds, workdir)
            generate(crd_files, output.resolve(), sources.fetch_crd2pulumi(workdir))
            log.info(f'Regenerated {output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
