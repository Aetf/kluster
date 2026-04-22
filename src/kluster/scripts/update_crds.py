from http.client import HTTPResponse
from io import BytesIO
import logging
import logging.config
import os
import re
import shutil
import subprocess as sp
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, IO, Generator, Mapping
from contextlib import contextmanager
import argparse

import pulumi_kubernetes
import requests
from ruamel.yaml import YAML
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm
from tqdm.contrib import tenumerate

DEFAULT_LOGGING = {
    'version': 1,
    'formatters': {
        'standard': {'format': '%(asctime)s %(levelname)s: %(message)s', 'datefmt': '%Y-%m-%d - %H:%M:%S'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'standard', 'level': 'DEBUG', 'stream': sys.stdout},
    },
    'loggers': {
        __name__: {'level': 'INFO', 'handlers': ['console'], 'propagate': False},
    },
}
logging.config.dictConfig(DEFAULT_LOGGING)
log = logging.getLogger(__name__)


@contextmanager
def tqdm_wrap_file_read(fileobj: IO[bytes], /, **kwargs) -> Generator[IO[bytes], None, None]:
    kwargs = {'unit': 'B', 'unit_scale': True, 'unit_divisor': 1024, 'miniters': 1, **kwargs}
    with tqdm.wrapattr(fileobj, 'read', **kwargs) as wrapped:
        yield wrapped


def download_github_release(repo_name: str, pattern_str: str, output_dir: Path = Path('.')) -> Path:
    pattern = re.compile(pattern_str)
    resp = requests.get(f'https://api.github.com/repos/{repo_name}/releases/latest')
    for asset in resp.json()['assets']:
        if pattern.search(asset['name']) is not None:
            break
    else:
        raise ValueError(f'Not found: {repo_name}, {pattern}, {output_dir}')

    url = asset['browser_download_url']
    log.info(f'Downloading from {url}')
    with requests.get(url, stream=True) as rx:
        total = int(rx.headers.get('content-length', 0))
        # rx.raw is urllib3 HTTPResponse, which is supposed to be fileobj-like
        # according to its documentation, but typing doesn't say so.
        with tqdm_wrap_file_read(rx.raw, desc=f'Downloading {repo_name}', total=total) as f_in:  # type: ignore[reportArgumentType]
            with tarfile.open(fileobj=f_in, mode='r:gz') as tarobj:
                tarobj.extractall(output_dir, filter='data')

    # find the binary executable
    for path in output_dir.rglob('*'):
        if path.is_file() and os.access(path, os.X_OK):
            log.info(f'Downloaded binary from {repo_name}: {path.resolve()}')
            return path.resolve()
    raise ValueError('No executable found')


def fix_crds(crds: Mapping[str, Any]) -> List[Dict[str, Any]]:
    items = crds['items']

    for item in tqdm(items, desc='Removing unwanted fields'):
        del item['status']

    log.info('Removing unwanted CRD')
    items = [item for item in items if item['spec']['group'] != 'traefik.containo.us']

    return items


def fix_generated(output: Path):
    pass


def main():
    parser = argparse.ArgumentParser(description='Update CRD bindings')
    parser.add_argument('--file', type=str, help='Path to crds.yaml file instead of running kubectl')
    parser.add_argument('--version', type=str, default='4.18.0', help='Pulumi Kubernetes version to target')
    args = parser.parse_args()

    with logging_redirect_tqdm():
        output = Path('./packages/crds')
        output_bak = output.with_suffix('.bak')

        log.info(f'CRDs directory: {output.resolve()}')

        with TemporaryDirectory(prefix='update_crds-', delete=True) as dir:
            dir = Path(dir)
            log.info(f'Working directory: {dir.resolve()}')
            crd2pulumi = download_github_release('pulumi/crd2pulumi', 'linux-amd64', output_dir=dir)

            if args.file:
                log.info(f'Reading CRDs from file: {args.file}')
                with open(args.file, 'rb') as f:
                    crds_yaml = f.read()
            else:
                log.info('Downloading CRDs from k8s')
                kubectl = shutil.which('kubectl')
                if kubectl is None:
                    raise ValueError('Can not find kubectl in PATH')
                crds_yaml = sp.check_output([kubectl, 'get', 'crds', '-o', 'yaml'])

            ryaml = YAML(typ='rt')
            with tqdm_wrap_file_read(
                BytesIO(crds_yaml), desc='Loading CRD yaml', total=len(crds_yaml)
            ) as crds_yaml_stream:
                crds = ryaml.load(crds_yaml_stream)
            crds = fix_crds(crds)
            log.info(f'Loaded {len(crds)} CRD')

            # Write to multiple yml files, as crd2pulumi can't work with combined doc
            files: List[str] = []
            for idx, crd in tenumerate(crds, desc='Writing to CRD yml files'):
                yml_file = dir / f'crd_{idx}.yml'
                with yml_file.open('w') as f:
                    ryaml.dump(crd, f)
                files.append(str(yml_file))
            log.info(f'Wrote to {len(files)} yml files')

            log.info('Backup existing crd bindings')
            shutil.rmtree(output_bak, ignore_errors=True)
            output.replace(output_bak)

            log.info('Generating new crd bindings')
            pulumi_kubernetes_version = pulumi_kubernetes._utilities.get_version()
            sp.check_call(
                [crd2pulumi, '--python', '--pythonPath', str(output), '--version', pulumi_kubernetes_version] + files
            )

            fix_generated(output)

            log.info('All done')

            shutil.rmtree(output_bak, ignore_errors=True)
