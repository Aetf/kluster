"""The static sites, declared against mocks: the blog's shape with a git source, and the doors' with a NAS source.

Each site is declared with route rows, pins, a repository and a NAS path of
the test's own, so what is held is how the component composes what it is
handed. The blog's documents are matrix's delegation, rendered from the
census's `matrix` client, as the program hands them in. The server's
configuration is the one the declared ConfigMap carries, and the cases that
run the pinned images run it, and git-sync, as the declared pods would.

Literals written here are the upstream projects' contracts: git-sync's flags,
nginx's directives and answers, the Gateway API's filter and match spellings,
the Kubernetes and node-selector fields, the label the scheduler spreads pods
over, and the CORS header a browser reads.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import uuid
from collections.abc import Mapping
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import process_sessions
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from pinned_image import COMMAND_TIMEOUT, CONTAINER_TIMEOUT, pinned, pulled, require_podman

from kluster.components.apps import Fronts
from kluster.components.apps.static_site import GitSource, NasSource, StaticSite
from kluster.components.cilium import InternetPoolMembers
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import PROXIED, Apex, Balancer, Device, Direct, Exposure, Held, Passthrough, Port, Route
from kluster.lib.versions import ImagePin

DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
VOLUME = 'kubernetes:core/v1:PersistentVolume'
HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'

#: The blog's apex documents: two JSON bodies of the test's own, at paths
#: without an extension, as a delegation's are.
DOCUMENTS = {
    '/.well-known/example/server': json.dumps({'server': 'home.blog.example:443'}),
    '/.well-known/example/client': json.dumps({'base_url': 'https://home.blog.example'}),
}

#: The zones of the test's sites: the blog's two, and the doors' apex and the
#: zone of its two other names.
BLOG_ZONES = ('blog.example', 'blog-cohost.example')
DOORS_APEX = 'doors.example'
DOORS_ZONE = 'games.example'

#: The blog: its apex and `www` in each zone.
BLOG = Exposure(routes=(Apex(BLOG_ZONES), Route('www', internet=PROXIED, zones=BLOG_ZONES)))

#: The blog as its land declares it, `www` still held by the legacy VPS.
HELD = Exposure(routes=(Apex(BLOG_ZONES), Held(Route('www', internet=PROXIED, zones=BLOG_ZONES))))

#: The doors: one apex, and two names in another zone, each with a directory
#: of its own.
DOORS = Exposure(
    routes=(
        Apex((DOORS_APEX,)),
        Route('games', internet=PROXIED, zones=(DOORS_ZONE,)),
        Route('game', internet=PROXIED, zones=(DOORS_ZONE,)),
    )
)
DIRECTORIES = {DOORS_APEX: 'door-a', f'games.{DOORS_ZONE}': 'door-b', f'game.{DOORS_ZONE}': 'door-c'}

#: The repository and branch the blog syncs: a repository the sync's case lays
#: into the container's home before git-sync starts, whose default branch is
#: not the one synced.
REPOSITORY = 'file:///tmp/upstream'
BRANCH = 'built'

#: The pins the sites are declared with here: references that are no
#: published image, so a container carrying one carries the pin.
SERVER = ImagePin('registry.example.invalid/static', 'v9.9.9', f'sha256:{"a" * 64}')
SYNC = ImagePin('registry.example.invalid/git-sync', 'v9.9.9', f'sha256:{"b" * 64}')

NAS_PATH = 'Web/doors'

FRONTS = Fronts(
    internet=InternetPoolMembers(
        node_private_ips={'cp-1': '10.0.1.11'},
        node_guas={'cp-1': '2001:db8:1::11'},
        dedicated_vip='10.0.1.99',
        balancer_v4='203.0.113.10',
        balancer_v6='2001:db8::10',
    ),
    worker_gua='2001:db8:70::10',
)
ZONE_IDS = {zone: f'{zone}-id' for zone in (*BLOG_ZONES, DOORS_APEX, DOORS_ZONE)}

#: The labels of a cloud node and of the worker, as the node selectors read them.
CLOUD_NODE = {CONTROL_PLANE_LABEL: '', 'kubernetes.io/hostname': 'cp-1'}
WORKER = {'kubernetes.io/hostname': 'worker'}


def declare_blog(**overrides: Any) -> StaticSite:
    arguments: dict[str, Any] = {
        'exposure': BLOG,
        'source': GitSource(REPOSITORY, BRANCH, SYNC),
        'image': SERVER,
        'fronts': FRONTS,
        'zone_ids': ZONE_IDS,
        'directories': {},
        'documents': DOCUMENTS,
    }
    return StaticSite('blog', **(arguments | overrides))


def declare_doors(**overrides: Any) -> StaticSite:
    arguments: dict[str, Any] = {
        'exposure': DOORS,
        'source': NasSource(NAS_PATH),
        'image': SERVER,
        'fronts': FRONTS,
        'zone_ids': ZONE_IDS,
        'directories': DIRECTORIES,
        'documents': {},
    }
    return StaticSite('doors', **(arguments | overrides))


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def blog() -> Recorder:
    """The blog's shape: a git source, its apex and `www` in two zones, and documents at the apex."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = declare_blog()
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def held() -> Recorder:
    """The blog's shape with `www` held, as the land declares it before the cut."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = declare_blog(exposure=HELD)
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def doors() -> Recorder:
    """The doors' shape: a NAS source, three names, each with a directory of its own."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = declare_doors()
    return monitor


def the_one(monitor: Recorder, typ: str) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(typ)]
    return found


def pod_of(monitor: Recorder) -> dict[str, Any]:
    return the_one(monitor, DEPLOYMENT)['spec']['template']['spec']


def container(monitor: Recorder, name: str, *, init: bool = False) -> dict[str, Any]:
    (found,) = [it for it in pod_of(monitor)['initContainers' if init else 'containers'] if it['name'] == name]
    return found


def volume(monitor: Recorder, name: str) -> dict[str, Any]:
    (found,) = [it for it in pod_of(monitor)['volumes'] if it['name'] == name]
    return found


def mount_of(entry: Mapping[str, Any], name: str) -> dict[str, Any]:
    (found,) = [it for it in entry['volumeMounts'] if it['name'] == name]
    return found


def flags(entry: Mapping[str, Any]) -> dict[str, str]:
    """A container's `--name=value` arguments, by name."""
    return dict(argument.removeprefix('--').partition('=')[::2] for argument in entry['args'])


def server_file(monitor: Recorder) -> tuple[str, str]:
    """The configuration the server reads, as its path in the server and its text: its ConfigMap's one file, where the server mounts it."""
    server = container(monitor, 'server')
    mount = mount_of(server, 'config')
    config = volume(monitor, mount['name'])
    (config_map,) = [
        it.inputs for it in monitor.of_type(CONFIG_MAP) if it.inputs['metadata']['name'] == config['configMap']['name']
    ]
    ((key, text),) = config_map['data'].items()
    return f'{mount["mountPath"]}/{key}', text


def server_configuration(monitor: Recorder) -> str:
    return server_file(monitor)[1]


def roots(monitor: Recorder) -> dict[str, str]:
    """Each name the configuration serves, and the root it reads from, as nginx resolves `server_name` to `root`."""
    found: dict[str, str] = {}
    for block in re.findall(r'server \{(.*?)\n\}', server_configuration(monitor), re.S):
        names = re.search(r'^\s*server_name ([^;]+);', block, re.M)
        root = re.search(r'^\s*root ([^;]+);', block, re.M)
        if names is not None and root is not None:
            found |= dict.fromkeys(names[1].split(), root[1])
    return found


def holds(expression: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    """Whether `labels` satisfy one requirement of a Kubernetes node selector."""
    present = expression['key'] in labels
    match expression['operator']:
        case 'Exists':
            return present
        case 'DoesNotExist':
            return not present
        case operator:
            raise AssertionError(f'no reading of {operator!r} here')


def admits(monitor: Recorder, labels: Mapping[str, str]) -> bool:
    """Whether the pod's required node affinity admits a node carrying `labels`."""
    terms = pod_of(monitor)['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution'][
        'nodeSelectorTerms'
    ]
    return any(all(holds(it, labels) for it in term['matchExpressions']) for term in terms)


# -- the git source ----------------------------------------------------------------


def test_a_git_source_syncs_its_branch_into_an_empty_directory_the_server_reads(blog: Recorder) -> None:
    """git-sync checks the branch out into an empty directory, once before the server starts and then beside it, and every name is read from behind its link."""
    first, sidecar = container(blog, 'first-sync', init=True), container(blog, 'sync')
    server = container(blog, 'server')
    content = mount_of(server, 'content')

    for sync in (first, sidecar):
        assert flags(sync)['repo'] == REPOSITORY
        assert flags(sync)['ref'] == BRANCH
        assert flags(sync)['root'].startswith(f'{mount_of(sync, "content")["mountPath"]}/')
        assert sync['image'] == str(SYNC)
    assert 'one-time' in flags(first)
    assert 'one-time' not in flags(sidecar)
    assert 'emptyDir' in volume(blog, 'content')
    under = flags(sidecar)['root'].removeprefix(mount_of(sidecar, 'content')['mountPath'])
    synced = f'{content["mountPath"]}{under}/{flags(sidecar)["link"]}'
    assert set(roots(blog).values()) == {synced}
    assert content.get('readOnly') is True


def test_a_git_source_runs_more_than_one_replica_one_per_cloud_node(blog: Recorder) -> None:
    deployment = the_one(blog, DEPLOYMENT)
    labels = deployment['spec']['template']['metadata']['labels']
    (spread,) = pod_of(blog)['topologySpreadConstraints']

    assert deployment['spec']['replicas'] > 1
    assert spread['topologyKey'] == 'kubernetes.io/hostname'
    assert spread['maxSkew'] == 1
    assert spread['whenUnsatisfiable'] == 'DoNotSchedule'
    assert spread['labelSelector']['matchLabels'].items() <= labels.items()
    assert admits(blog, CLOUD_NODE)
    assert not admits(blog, WORKER)


def test_a_failed_fetch_never_stops_the_sidecar(blog: Recorder) -> None:
    """A sidecar that exits makes the pod unready, and the content synced last is still served."""
    assert int(flags(container(blog, 'sync'))['max-failures']) < 0


# -- the NAS source ----------------------------------------------------------------


def test_a_nas_source_mounts_its_directory_read_only(doors: Recorder) -> None:
    server = container(doors, 'server')
    claim = volume(doors, mount_of(server, 'content')['name'])['persistentVolumeClaim']
    (persistent,) = [it.inputs for it in doors.of_type(VOLUME)]

    assert mount_of(server, 'content')['readOnly'] is True
    assert claim['readOnly'] is True
    assert persistent['spec']['nfs']['readOnly'] is True
    assert persistent['spec']['nfs']['path'].endswith(f'/{NAS_PATH}')
    assert persistent['spec']['claimRef']['name'] == claim['claimName']


def test_a_nas_source_runs_one_pod_on_the_worker(doors: Recorder) -> None:
    assert the_one(doors, DEPLOYMENT)['spec']['replicas'] == 1
    assert admits(doors, WORKER)
    assert not admits(doors, CLOUD_NODE)


def test_each_name_is_read_from_its_own_directory(doors: Recorder) -> None:
    content = mount_of(container(doors, 'server'), 'content')['mountPath']

    assert roots(doors) == {name: f'{content}/{directory}' for name, directory in DIRECTORIES.items()}


# -- the documents -----------------------------------------------------------------


def response_headers(route: Mapping[str, Any]) -> list[tuple[list[Any], dict[str, Any]]]:
    """Each rule of `route` that modifies the response, with its matches and its modifier."""
    return [
        (rule.get('matches', []), item['responseHeaderModifier'])
        for rule in route['spec']['rules']
        for item in rule.get('filters', [])
        if item['type'] == 'ResponseHeaderModifier'
    ]


def test_the_cors_header_is_added_on_the_documents_paths_alone(blog: Recorder) -> None:
    """Every route rule that adds a header matches a document's path exactly, and every document's path is one."""
    modifying = [found for it in blog.of_type(HTTP_ROUTE) for found in response_headers(it.inputs)]
    matched = [match['path'] for matches, _ in modifying for match in matches]

    assert modifying
    assert all(matches for matches, _ in modifying)
    assert sorted(matched, key=lambda it: it['value']) == sorted(
        ({'type': 'Exact', 'value': path} for path in DOCUMENTS), key=lambda it: it['value']
    )
    for _, modifier in modifying:
        assert modifier == {'add': [{'name': 'Access-Control-Allow-Origin', 'value': '*'}]}


def test_the_documents_route_attaches_at_the_apex_listeners(blog: Recorder) -> None:
    (documents,) = [it.inputs for it in blog.of_type(HTTP_ROUTE) if response_headers(it.inputs)]

    (apex,) = [it.inputs for it in blog.of_type(HTTP_ROUTE) if it.inputs['metadata']['name'] == 'apex']

    assert documents['spec']['parentRefs'] == apex['spec']['parentRefs']
    assert documents['spec']['hostnames'] == apex['spec']['hostnames']


def test_the_documents_are_served_at_the_apex_alone(blog: Recorder) -> None:
    """The apex's server block serves each document from its ConfigMap's file; `www`'s serves none."""
    blocks = re.findall(r'server \{(.*?)\n\}', server_configuration(blog), re.S)
    (apex,) = [block for block in blocks if f'server_name {" ".join(BLOG_ZONES)};' in block]
    (www,) = [block for block in blocks if 'server_name www.' in block]
    mount = mount_of(container(blog, 'server'), 'documents')['mountPath']

    assert sorted(re.findall(r'location = (\S+) \{', apex)) == sorted(DOCUMENTS)
    assert f'root {mount};' in apex
    assert 'location' not in www


def test_the_documents_reach_the_server_as_the_program_hands_them_in(blog: Recorder) -> None:
    """Each document's file, under the server's mount, holds its body."""
    mount = mount_of(container(blog, 'server'), 'documents')['mountPath']
    source = volume(blog, 'documents')['configMap']
    (config_map,) = [it.inputs for it in blog.of_type(CONFIG_MAP) if it.inputs['metadata']['name'] == source['name']]
    files = {f'{mount}/{item["path"]}': config_map['data'][item['key']] for item in source['items']}

    assert files == {f'{mount}{path}': body for path, body in DOCUMENTS.items()}


# -- what is refused ---------------------------------------------------------------


def test_documents_without_an_apex_are_refused() -> None:
    with pytest.raises(ValueError, match='no apex row'):
        _ = declare_blog(exposure=Exposure(routes=(Route('www', internet=PROXIED, zones=BLOG_ZONES),)))


def test_a_directory_for_a_name_no_row_serves_is_refused() -> None:
    with pytest.raises(ValueError, match='no route row serves'):
        _ = declare_doors(directories={**DIRECTORIES, 'elsewhere.example': 'door-d'})


@pytest.mark.parametrize('directory', ['../outside', '/absolute'], ids=['above the content', 'absolute'])
def test_a_directory_out_of_the_content_is_refused(directory: str) -> None:
    with pytest.raises(ValueError, match='the directory'):
        _ = declare_doors(directories={DOORS_APEX: directory})


@pytest.mark.asyncio
async def test_a_nas_path_above_the_export_is_refused_before_anything_registers() -> None:
    """The site's own refusal: `nas_claim`'s would come after the namespace had registered."""
    monitor = await run_with(Recorder(), stack='apps')

    with pytest.raises(ValueError, match='the NAS path'):
        async with declaring():
            _ = declare_doors(source=NasSource('../outside'))

    assert not monitor.declared


def test_a_raw_port_is_refused() -> None:
    exposure = Exposure(routes=BLOG.routes, ports=(Port('static', 8443, Transports.TCP, internet=Balancer()),))

    with pytest.raises(ValueError, match='never at a raw port'):
        _ = declare_blog(exposure=exposure)


def test_a_device_is_refused() -> None:
    exposure = Exposure(
        routes=(Route('www', internet=PROXIED, zones=BLOG_ZONES),), device=Device(IPv4Address('192.0.2.10'), 80)
    )

    with pytest.raises(ValueError, match='or a device'):
        _ = declare_blog(exposure=exposure, documents={})


def test_a_passthrough_row_is_refused_before_anything_registers() -> None:
    """The site's own refusal, which `expose`'s would otherwise be, after the namespace and the pods had registered."""
    exposure = Exposure(routes=(*BLOG.routes, Passthrough('tls', internet=Direct('its own certificate'))))

    with pytest.raises(ValueError, match='holds no certificate'):
        _ = declare_blog(exposure=exposure)


@pytest.mark.parametrize(
    'path',
    ['.well-known/x', '/../x', '/x/'],
    ids=['a relative path', 'a path above the root', 'a directory'],
)
def test_a_document_that_is_no_file_under_the_root_is_refused(path: str) -> None:
    with pytest.raises(ValueError, match='the document'):
        _ = declare_blog(documents={path: '{}'})


@pytest.mark.parametrize('site', ['blog', 'doors', 'held'])
def test_every_name_a_route_forwards_has_a_server(site: str, request: pytest.FixtureRequest) -> None:
    """A name a route forwards and no server lists is answered by the default server, with a 404."""
    monitor: Recorder = request.getfixturevalue(site)
    hostnames = {name for it in monitor.of_type(HTTP_ROUTE) for name in it.inputs['spec']['hostnames']}

    assert hostnames
    assert set(roots(monitor)) == hostnames


# -- the pods ----------------------------------------------------------------------


@pytest.mark.parametrize('site', ['blog', 'doors'])
def test_every_container_meets_restricted_and_carries_limits(site: str, request: pytest.FixtureRequest) -> None:
    monitor: Recorder = request.getfixturevalue(site)
    pod = pod_of(monitor)
    containers = [*pod['containers'], *pod.get('initContainers', [])]

    assert pod['securityContext']['runAsNonRoot'] is True
    assert pod['securityContext']['seccompProfile'] == {'type': 'RuntimeDefault'}
    for entry in containers:
        context = entry['securityContext']
        assert context['runAsUser'] == int(context['runAsUser']) > 0
        assert context['allowPrivilegeEscalation'] is False
        assert context['readOnlyRootFilesystem'] is True
        assert context['capabilities'] == {'drop': ['ALL']}
        assert {'cpu', 'memory'} <= entry['resources']['limits'].keys()


# -- the pinned images, run as the pods run them ----------------------------------

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_apps_static_site`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: Why these cases need podman.
WHY = 'the server and git-sync are run from the images they are pinned to'

#: What the default branch holds and what the synced one holds, told apart.
DEFAULT_PAGE = 'the default branch'
BRANCH_PAGE = 'the built branch'

#: The upstream repository, made in the sync's home before git-sync starts:
#: its default branch holds one page and `BRANCH` another. Then git-sync runs
#: with the declared container's arguments, everything under its root, `$2`,
#: is checked to be readable by another user, as the server is, and the page is
#: read back from the path the server's configuration names, `$1`.
SYNC_SCRIPT = f"""set -e
page="$1"; root="$2"; shift 2
git init -q -b main /tmp/upstream
cd /tmp/upstream
git config user.name test
git config user.email test@example.invalid
echo '{DEFAULT_PAGE}' > index.html
git add index.html
git commit -q -m default
git checkout -q -b {BRANCH}
echo '{BRANCH_PAGE}' > index.html
git commit -q -a -m built
git checkout -q main
cd /
/git-sync "$@" >&2
unreadable=$(bash -c '
shopt -s globstar dotglob nullglob
for path in "$1" "$1"/**; do
    [ -L "$path" ] && continue
    other=$(( $(stat -c %a "$path") % 10 ))
    if [ -d "$path" ]; then need=5; else need=4; fi
    [ $(( other & need )) -eq "$need" ] || echo "$path"
done' bash "$root")
if [ -n "$unreadable" ]; then
    echo "not readable by another user: $unreadable" >&2
    exit 1
fi
cat "$page"
"""


def archive(files: Mapping[str, str]) -> bytes:
    """A tar of `files`, owned by root and readable by all, as a ConfigMap's and a checkout's files are."""
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


def as_declared(monitor: Recorder, entry: Mapping[str, Any], name: str, *, laid: str | None = None) -> list[str]:
    """`podman create` options that run `entry` as the declared pod runs it: its user, a read-only root, and an empty directory as a tmpfs.

    `laid` is the mount the case lays files into rather than leaving empty.
    """
    empty = {it['name'] for it in pod_of(monitor)['volumes'] if 'emptyDir' in it}
    context = entry['securityContext']
    return [
        'create',
        '--rm',
        f'--timeout={CONTAINER_TIMEOUT}',
        f'--label={LABEL}',
        f'--name={name}',
        '--network=none',
        '--read-only',
        '--read-only-tmpfs=false',
        '--cap-drop=all',
        '--security-opt=no-new-privileges',
        f'--user={int(context["runAsUser"])}:{int(context["runAsGroup"])}',
        *(
            f'--tmpfs={mount["mountPath"]}:mode=1777'
            for mount in entry['volumeMounts']
            if mount['name'] in empty and mount['mountPath'] != laid
        ),
    ]


def test_the_pinned_sync_checks_the_branch_out_where_the_server_reads_it(blog: Recorder) -> None:
    """The first sync's arguments, run by the pinned git-sync, put the branch's page, not the default branch's, at the path the server's root names.

    The server and the sync mount one volume, so a root under the server's
    mount is that path under the sync's.
    """
    require_podman(WHY)
    first = container(blog, 'first-sync', init=True)
    server_mount = mount_of(container(blog, 'server'), 'content')['mountPath']
    sync_mount = mount_of(first, 'content')['mountPath']
    root = roots(blog)[BLOG_ZONES[0]]
    page = f'{sync_mount}{root.removeprefix(server_mount)}/index.html'
    image = pulled(pinned('static-site-git-sync'))
    name = f'kluster-test-static-site-sync-{uuid.uuid4().hex[:12]}'

    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        _ = podman(
            *as_declared(blog, first, name),
            f'--entrypoint={json.dumps(["sh", "-c", SYNC_SCRIPT, "sh"])}',
            image,
            page,
            flags(first)['root'],
            *first['args'],
        )
        read = podman('start', '--attach', name)

    assert read.strip() == BRANCH_PAGE


def redirected(name: str, host: str, path: str) -> tuple[int, str]:
    """The server's status for `path` under `host`, and its `Location` header as it was sent, unresolved."""
    written = podman(
        'exec', name, 'curl', '--silent', '--output', '/dev/null', '--header', f'Host: {host}',
        '--write-out', '%{http_code} %header{location}', f'http://127.0.0.1:8080{path}',
    )  # fmt: skip
    code, _, location = written.partition(' ')
    return int(code), location


def ask(name: str, host: str, path: str) -> tuple[int, str, str]:
    """The server's answer to `path` under `host`: its status, its content type and its body."""
    written = podman(
        'exec', name, 'curl', '--silent', '--retry', '20', '--retry-connrefused', '--retry-delay', '1',
        '--header', f'Host: {host}', '--write-out', '\n%{http_code} %{content_type}',
        f'http://127.0.0.1:8080{path}',
    )  # fmt: skip
    body, _, status = written.rpartition('\n')
    code, _, kind = status.partition(' ')
    return int(code), kind, body


def served(monitor: Recorder, content: Mapping[str, str]) -> dict[str, str]:
    """The server's files as the pod mounts them: the configuration, the documents, and `content` under its mount."""
    server = container(monitor, 'server')
    files = dict([server_file(monitor)])
    if any(it['name'] == 'documents' for it in server['volumeMounts']):
        mount = mount_of(server, 'documents')['mountPath']
        source = volume(monitor, 'documents')['configMap']
        (config_map,) = [
            it.inputs for it in monitor.of_type(CONFIG_MAP) if it.inputs['metadata']['name'] == source['name']
        ]
        files |= {f'{mount}/{item["path"]}': config_map['data'][item['key']] for item in source['items']}
    return files | dict(content)


def test_the_pinned_server_answers_each_name_from_its_own_directory(doors: Recorder) -> None:
    """Each name reads its own directory, a name no site serves is refused, and the probe is answered.

    A directory asked for without its trailing slash is redirected by its path
    alone: the client reached the server through a Gateway, on another scheme
    and port than the server's own.
    """
    require_podman(WHY)
    server = container(doors, 'server')
    content = mount_of(server, 'content')['mountPath']
    pages = {f'{content}/{directory}/index.html': name for name, directory in DIRECTORIES.items()}
    pages[f'{content}/{DIRECTORIES[DOORS_APEX]}/sub/index.html'] = 'below'
    image = pulled(pinned('static-site'))
    name = f'kluster-test-static-site-doors-{uuid.uuid4().hex[:12]}'

    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        _ = podman(*as_declared(doors, server, name, laid=content), image)
        _ = podman('cp', '--archive=false', '-', f'{name}:/', input=archive(served(doors, pages)))
        _ = podman('start', name)
        answers = {host: ask(name, host, '/') for host in DIRECTORIES}
        stranger = ask(name, 'elsewhere.example', '/')
        probe = ask(name, '127.0.0.1', server['readinessProbe']['httpGet']['path'])
        slashless = redirected(name, DOORS_APEX, '/sub')

    assert slashless == (301, '/sub/')
    assert {host: (code, body) for host, (code, _, body) in answers.items()} == {
        host: (200, host) for host in DIRECTORIES
    }
    assert stranger[0] == 404
    assert 200 <= probe[0] < 400


def test_the_pinned_server_serves_the_documents_as_json_at_the_apex_alone(blog: Recorder) -> None:
    """The apex answers each document's path with its body as JSON; `www` reads its path from the content, which has none."""
    require_podman(WHY)
    server = container(blog, 'server')
    root = roots(blog)[BLOG_ZONES[0]]
    pages = {f'{root}/index.html': 'the blog', f'{root}/404.html': 'not here'}
    image = pulled(pinned('static-site'))
    name = f'kluster-test-static-site-blog-{uuid.uuid4().hex[:12]}'

    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        _ = podman(*as_declared(blog, server, name, laid=mount_of(server, 'content')['mountPath']), image)
        _ = podman('cp', '--archive=false', '-', f'{name}:/', input=archive(served(blog, pages)))
        _ = podman('start', name)
        documents = {path: ask(name, BLOG_ZONES[0], path) for path in DOCUMENTS}
        at_www = {path: ask(name, f'www.{BLOG_ZONES[0]}', path) for path in DOCUMENTS}
        page = ask(name, f'www.{BLOG_ZONES[1]}', '/')

    for path, (code, kind, body) in documents.items():
        assert code == 200
        assert kind == 'application/json'
        assert json.loads(body) == json.loads(DOCUMENTS[path])
    assert {code for code, _, _ in at_www.values()} == {404}
    assert {body for _, _, body in at_www.values()} == {'not here'}
    assert page[:3:2] == (200, 'the blog')
