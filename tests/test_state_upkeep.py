"""The state table's daily upkeep: the program, its settings and its bundle, where the job's command reads them.

Read off the whole `k8s-base` program with every sealed value it declares
sealed in, which is when it declares the CronJob (`k8s_base_installation`).
The job's pod is laid out on disk the way the kubelet lays it out -- each
ConfigMap and Secret it mounts, a file per key, under its mount path -- and
the command's paths are read against that tree as the program reads them.
What a run does against a table is `table`'s own suite
(`tests/test_state_roles.py`); the family's watch over the CronJob is
`tests/test_monitoring.py`'s.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest_asyncio
from k8s_base_installation import SEALED, Run, applied

from kluster import conventions
from kluster.conventions.schedule import STATE_UPKEEP
from kluster.lib import bundle
from kluster.lib.state_backend import settings, table

CRONJOB = 'kubernetes:batch/v1:CronJob'
CONFIGMAP = 'kubernetes:core/v1:ConfigMap'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'

BUNDLE = conventions.sealed.STATE_UPKEEP_BUNDLE


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def sealed() -> Run:
    """Every sealed value the stack declares sealed in, the upkeep's bundle among them."""
    return await applied(sealed=SEALED)


def _in_namespace(run: Run, typ: str) -> list[dict[str, Any]]:
    return [
        it.inputs for it in run.monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == STATE_UPKEEP.namespace
    ]


def _container(run: Run) -> tuple[dict[str, Any], dict[str, Any]]:
    """The upkeep's pod spec and its one container."""
    (job,) = [it for it in _in_namespace(run, CRONJOB) if it['metadata']['name'] == STATE_UPKEEP.cronjob]
    pod = job['spec']['jobTemplate']['spec']['template']['spec']
    (container,) = pod['containers']
    return pod, container


#: The connection string the bundle's Secret stands in with, in the form a
#: bundle stores it.
_URL = 'postgres://upkeep@192.0.2.1:5432/pulumi_state?sslmode=verify-full\n'


def _pod_tree(run: Run, root: Path) -> None:
    """The files the upkeep's container sees under its mounts, written beneath `root` as the kubelet writes them.

    A ConfigMap's volume holds a file per key of the ConfigMap the program
    declares under the volume's name, with that key's data; a Secret's
    holds a file per key the SealedSecret of that name opens into. A
    Secret's file holds a stand-in, but the connection string's, which holds
    a string `table.Settings.client` reads as one.
    """
    pod, container = _container(run)
    config_maps = {it['metadata']['name']: it['data'] for it in _in_namespace(run, CONFIGMAP)}
    secrets = {it['metadata']['name']: it['spec']['encryptedData'] for it in _in_namespace(run, SEALED_SECRET)}
    volumes = {it['name']: it for it in pod['volumes']}
    for mount in container['volumeMounts']:
        volume = volumes[mount['name']]
        directory = root / mount['mountPath'].lstrip('/')
        directory.mkdir(parents=True)
        if 'configMap' in volume:
            files = dict(config_maps[volume['configMap']['name']])
        else:
            files = {
                key: _URL if key == bundle.URL_FILE else f'stand-in for {key}\n'
                for key in secrets[volume['secret']['secretName']]
            }
        for key, text in files.items():
            (directory / key).write_text(text)


def _arguments(run: Run, root: Path) -> tuple[Path, Path, Path]:
    """The program, settings and bundle paths the command names, beneath `root`, with the interpreter's own flags read."""
    _, container = _container(run)
    interpreter, *flags, program, settings_file, bundle_dir = container['command']
    assert (interpreter, flags) == ('python', ['-I', '-B'])
    return root / program.lstrip('/'), root / settings_file.lstrip('/'), root / bundle_dir.lstrip('/')


def test_the_command_names_the_paths_the_pod_mounts_the_program_its_settings_and_the_bundle_at(
    sealed: Run, tmp_path: Path
) -> None:
    """Every path the command hands the program is a file or directory the pod mounts, and the bundle opens there.

    The settings are read and the bundle opened as `table.main` reads and
    opens them, so a file of the bundle the settings name and the Secret
    lacks is no file under the mount, and fails here.
    """
    _pod_tree(sealed, tmp_path)
    program, settings_file, bundle_dir = _arguments(sealed, tmp_path)

    client = table.Settings.load(settings_file).client(bundle_dir)

    assert program.is_file()
    assert client.url.startswith('postgres://upkeep@')
    assert client.env
    for variable, path in client.env.items():
        assert Path(path).is_file(), f'{variable} names {path}, which the pod does not mount'


def test_the_config_map_carries_the_module_the_workstation_imports(sealed: Run, tmp_path: Path) -> None:
    """The file the command runs is `table` as it is on disk, so the job runs what `state-backend census` runs."""
    _pod_tree(sealed, tmp_path)
    program, _, _ = _arguments(sealed, tmp_path)

    assert program.read_text() == Path(table.__file__).read_text()


def test_the_rendered_settings_carry_the_values_of_settings_main_reads(sealed: Run, tmp_path: Path) -> None:
    """The database, the retention rule's lengths and the ceiling are `settings`', and the bundle's layout `bundle`'s."""
    _pod_tree(sealed, tmp_path)
    _, settings_file, bundle_dir = _arguments(sealed, tmp_path)

    read = table.Settings.load(settings_file)

    assert read.database == settings.DATABASE
    assert read.expiry_age == settings.EXPIRY_AGE
    assert read.history_records == settings.HISTORY_RECORDS
    assert read.largest_row_bytes == settings.LARGEST_ROW_BYTES
    assert read.url_file == bundle.URL_FILE
    assert read.client(bundle_dir).env == bundle.ssl_env(bundle_dir)


def test_the_bundle_is_the_rows_sealed_secret_in_the_jobs_namespace(sealed: Run) -> None:
    """The SealedSecret the pod mounts is the row's, holding the ciphertext the stack's configuration holds for it."""
    pod, _ = _container(sealed)
    (secret,) = [it for it in _in_namespace(sealed, SEALED_SECRET) if it['metadata']['name'] == BUNDLE.name]
    mounted = [it['secret']['secretName'] for it in pod['volumes'] if 'secret' in it]

    assert BUNDLE.namespace == STATE_UPKEEP.namespace
    assert mounted == [BUNDLE.name]
    assert secret['spec']['encryptedData'] == SEALED[BUNDLE.name]
