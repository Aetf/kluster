"""The doors, declared against mocks, with their server run from its pin.

The rows here are the test's own, in zones no census row carries: an apex, and
`game` and `games` in a second zone, as the doors' rows are shaped. So what is
held is how the doors read the rows they are handed, never what the census
happens to say. The pin is the test's own too, and the case that runs the
server runs the pinned one.

Literals written here are the upstream projects' contracts (Kubernetes' Pod
Security label and volume fields, nginx's answers), and the legacy server's
webroot: which directory it served each name from, as kluster-code's
`src/index.ts` declares its `staticSites`, which is the content the doors are
copied from.
"""

from __future__ import annotations

import io
import tarfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import process_sessions
import pytest
import pytest_asyncio
from apps_doors import APEX, COHOST, FRONTS, LAND, SERVER, ZONE_IDS
from mock_monitor import Recorder, declaring, run_with
from node_selection import CLOUD, NODES, schedulable
from pinned_image import COMMAND_TIMEOUT, CONTAINER_TIMEOUT, pinned, pulled, require_podman

from kluster.components.apps import doors as doors_app
from kluster.components.apps.doors import directories, doors
from kluster.conventions.routes import PROXIED, Apex, Exposure, Route
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PodSecurity

DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
NAMESPACE = 'kubernetes:core/v1:Namespace'
VOLUME = 'kubernetes:core/v1:PersistentVolume'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'


#: The doors once cut.
CUT = Exposure(
    routes=(
        Apex((APEX,)),
        Route('games', internet=PROXIED, zones=(COHOST,)),
        Route('game', internet=PROXIED, zones=(COHOST,)),
    )
)

#: The legacy webroot's directory each name was served from, which the copy
#: onto the NAS keeps.
LEGACY_WEBROOT = {APEX: 'door-jiahui', f'games.{COHOST}': 'door-shiyu', f'game.{COHOST}': 'door'}


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """The doors as their land declares them."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = doors(exposure=LAND, image=SERVER, fronts=FRONTS, zone_ids=ZONE_IDS)
    return monitor


def in_namespace(monitor: Recorder, typ: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == doors_app.NAME]


def pod_of(monitor: Recorder) -> dict[str, Any]:
    (deployment,) = in_namespace(monitor, DEPLOYMENT)
    return deployment['spec']['template']['spec']


def server_of(monitor: Recorder) -> dict[str, Any]:
    (found,) = pod_of(monitor)['containers']
    return found


def mount_of(entry: Mapping[str, Any], name: str) -> dict[str, Any]:
    (found,) = [it for it in entry['volumeMounts'] if it['name'] == name]
    return found


def volume(monitor: Recorder, name: str) -> dict[str, Any]:
    (found,) = [it for it in pod_of(monitor)['volumes'] if it['name'] == name]
    return found


# -- the rows ----------------------------------------------------------------------


@pytest.mark.parametrize('exposure', [LAND, CUT], ids=['held', 'cut'])
def test_each_name_reads_its_legacy_directory_held_or_not(exposure: Exposure) -> None:
    assert directories(exposure) == LEGACY_WEBROOT


def test_a_row_the_doors_hold_no_directory_for_is_refused_before_anything_registers() -> None:
    """A name with no directory of its own would read the content's root, the three sites side by side."""
    stray = Exposure(routes=(*CUT.routes, Route('www', internet=PROXIED, zones=(APEX,))))

    with pytest.raises(ValueError, match="'www'"):
        _ = doors(exposure=stray, image=SERVER, fronts=FRONTS, zone_ids=ZONE_IDS)


# -- the content -------------------------------------------------------------------


def test_the_content_is_the_nas_read_only_and_nothing_is_backed_up(declared: Recorder) -> None:
    """The files are edited on the NAS, which keeps them; the cluster holds no copy to restore."""
    server = server_of(declared)
    claim = volume(declared, mount_of(server, 'content')['name'])['persistentVolumeClaim']
    (persistent,) = [it.inputs for it in declared.of_type(VOLUME)]

    assert mount_of(server, 'content')['readOnly'] is True
    assert claim['readOnly'] is True
    assert persistent['spec']['nfs']['readOnly'] is True
    assert persistent['spec']['claimRef']['name'] == claim['claimName']
    assert [it['metadata']['name'] for it in in_namespace(declared, CLAIM)] == [claim['claimName']]
    assert not declared.of_type(SOURCE)


def test_the_one_pod_runs_at_the_home_site(declared: Recorder) -> None:
    """The export is reachable from home alone."""
    (deployment,) = in_namespace(declared, DEPLOYMENT)

    assert deployment['spec']['replicas'] == 1
    assert schedulable(pod_of(declared)) == set(NODES) - CLOUD


def test_the_namespace_is_restricted_and_the_server_runs_as_a_numeric_user(declared: Recorder) -> None:
    (namespace,) = [it.inputs for it in declared.of_type(NAMESPACE)]
    context = server_of(declared)['securityContext']

    assert namespace['metadata']['labels'][POD_SECURITY_ENFORCE_LABEL] == PodSecurity.RESTRICTED
    assert pod_of(declared)['securityContext']['runAsNonRoot'] is True
    # The mock hands numbers back as floats, so the user is held to a whole one.
    assert context['runAsUser'] == int(context['runAsUser']) > 0


# -- the pinned server, run as the pod runs it -------------------------------------

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_apps_doors`.
LABEL = f'kluster-test={Path(__file__).stem}'


def archive(files: Mapping[str, str]) -> bytes:
    """A tar of `files`, owned by root and readable by all, as a ConfigMap's and the NAS's files are to the server."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        for path, text in files.items():
            content = text.encode()
            member = tarfile.TarInfo(path.lstrip('/'))
            member.size, member.mode = len(content), 0o644
            tar.addfile(member, io.BytesIO(content))
    return stream.getvalue()


def podman(*argv: str, input: bytes | None = None) -> str:  # noqa: A002 -- `subprocess.run`'s own name
    done = process_sessions.run(['podman', *argv], input=input, timeout=COMMAND_TIMEOUT, check=True)
    return done.stdout.decode()


def configuration(monitor: Recorder) -> dict[str, str]:
    """The server's configuration file, by its path in the server: its ConfigMap's one file, where the server mounts it."""
    mount = mount_of(server_of(monitor), 'config')
    name = volume(monitor, mount['name'])['configMap']['name']
    (config_map,) = [it for it in in_namespace(monitor, CONFIG_MAP) if it['metadata']['name'] == name]
    ((key, text),) = config_map['data'].items()
    return {f'{mount["mountPath"]}/{key}': text}


def ask(name: str, host: str) -> tuple[int, str]:
    """The server's answer to `/` under `host`: its status and its body."""
    written = podman(
        'exec', name, 'curl', '--silent', '--retry', '20', '--retry-connrefused', '--retry-delay', '1',
        '--header', f'Host: {host}', '--write-out', '\n%{http_code}', 'http://127.0.0.1:8080/',
    )  # fmt: skip
    body, _, code = written.rpartition('\n')
    return int(code), body


def test_the_pinned_server_serves_each_name_the_page_the_legacy_server_did(declared: Recorder) -> None:
    """The legacy webroot's directories, laid under the content as the copy lays them, answer each name with its own page.

    An alias of the apex that reached the server would be answered 404: no
    server lists it.
    """
    require_podman('the server is run from the image it is pinned to')
    server = server_of(declared)
    content = mount_of(server, 'content')['mountPath']
    pages = {f'{content}/{directory}/index.html': directory for directory in LEGACY_WEBROOT.values()}
    empty = {it['name'] for it in pod_of(declared)['volumes'] if 'emptyDir' in it}
    image = pulled(pinned('static-site'))
    name = f'kluster-test-doors-{uuid.uuid4().hex[:12]}'

    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        _ = podman(
            'create', '--rm', f'--timeout={CONTAINER_TIMEOUT}', f'--label={LABEL}', process_sessions.LOG_TO_FILE,
            f'--name={name}', '--network=none', '--read-only', '--read-only-tmpfs=false', '--cap-drop=all',
            '--security-opt=no-new-privileges',
            f'--user={int(server["securityContext"]["runAsUser"])}:{int(server["securityContext"]["runAsGroup"])}',
            *(f'--tmpfs={it["mountPath"]}:mode=1777' for it in server['volumeMounts'] if it['name'] in empty),
            image,
        )  # fmt: skip
        _ = podman('cp', '--archive=false', '-', f'{name}:/', input=archive(configuration(declared) | pages))
        _ = podman('start', name)
        answers = {host: ask(name, host) for host in LEGACY_WEBROOT}
        alias = ask(name, f'www.{APEX}')

    assert answers == {host: (200, directory) for host, directory in LEGACY_WEBROOT.items()}
    assert alias[0] == 404
