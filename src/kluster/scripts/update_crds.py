import logging
import logging.config
import os
import re
import subprocess as sp
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Union

import pulumi_kubernetes
import requests
import yaml

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


def download_github_release(repo_name: str, pattern_str: str, output_dir: Path = Path('.')) -> Path:
    pattern = re.compile(pattern_str)
    resp = requests.get(f'https://api.github.com/repos/{repo_name}/releases/latest')
    for asset in resp.json()['assets']:
        if pattern.search(asset['name']) is not None:
            break
    else:
        raise ValueError(f'Not found: {repo_name}, {pattern}, {output_dir}')

    with requests.get(asset['browser_download_url'], stream=True) as rx, tarfile.open(
        fileobj=rx.raw, mode='r:gz'
    ) as tarobj:
        tarobj.extractall(output_dir, filter='data')

    # find the binary executable
    for path in output_dir.rglob('*'):
        if path.is_file() and os.access(path, os.X_OK):
            log.info(f'Downloaded binary from {repo_name}: {path.resolve()}')
            return path.resolve()
    raise ValueError('No executable found')


def fix_crds(crds) -> List[Dict[str, Any]]:
    items = crds['items']

    # remove unwanted fields
    for item in items:
        del item['status']

    # remove unwanted crd
    items = [item for item in items if item['spec']['group'] != 'traefik.containo.us']

    # fix crd with object default, which crd2pulumi can't yet handle (fix not released yet)
    # See https://github.com/pulumi/crd2pulumi/pull/136
    def recursive_remove_default(obj: Union[List[Dict[str, Any]], Dict[str, Any]], prev_key: str):
        if isinstance(obj, dict):
            for k in list(obj.keys()):
                next_key = f'{prev_key}.{k}'

                if k == 'default' and isinstance(obj[k], dict):
                    del obj[k]
                else:
                    recursive_remove_default(obj[k], next_key)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj):
                recursive_remove_default(item, f'{prev_key}.[{idx}]')

    recursive_remove_default(items, 'items')

    return items


def fix_generated(output: Path):
    # fix version not actually set in setup.py
    log.info('Fix version not actually set in setup.py')
    sp.check_call(
        [
            'sed',
            '-E',
            f's/VERSION = "0.0.0"/VERSION = "{pulumi_kubernetes._utilities.get_version()}"/',
            '-i',
            str(output / 'setup.py'),
        ]
    )
    log.info('Fix requests version')
    sp.check_call(
        [
            'sed',
            '-E',
            f"""s/'requests>=2.21.0,<2.22.0'/'requests>=2.21.0'/""",
            '-i',
            str(output / 'setup.py'),
        ]
    )


def main():
    output = Path('./lib/crds')

    log.info(f'CRDs directory: {output.resolve()}')

    with TemporaryDirectory(prefix='update_crds-', delete=True) as dir:
        dir = Path(dir)
        log.info(f'Working directory: {dir.resolve()}')
        crd2pulumi = download_github_release('pulumi/crd2pulumi', 'linux-amd64', output_dir=dir)

        crds_yaml = sp.check_output(['kubectl', 'get', 'crds', '-o', 'yaml'])
        crds = fix_crds(yaml.safe_load(crds_yaml))
        log.info(f'Loaded {len(crds)} CRD from k8s')

        # write to multiple yml files, as crd2pulumi can't work with combined doc
        files: List[str] = []
        for idx, crd in enumerate(crds):
            yml_file = dir / f'crd_{idx}.yml'
            with yml_file.open('w') as f:
                yaml.safe_dump(crd, f)
            files.append(str(yml_file))
        log.info(f'Wrote to {len(files)} yml files')

        log.info('Generating new crd bindings')
        pulumi_kubernetes_version = pulumi_kubernetes._utilities.get_version()
        sp.check_call(
            [crd2pulumi, '--force', '--python', '--pythonPath', str(output), '--version', pulumi_kubernetes_version]
            + files
        )

        fix_generated(output)

        log.info('All done')
