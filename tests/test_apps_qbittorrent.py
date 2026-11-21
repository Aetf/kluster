"""qBittorrent and seedwatch, declared against mocks, the settings merge run over profiles, and both run as their pods run them.

Every row here is the test's own: an interface route gated from outside and on
`lan`, a peer port through the site's forward, and seedwatch's home route, each
under a name no census row carries. So are the pins, the writer key and
seedwatch's policy: what is held is how the component composes what it is
handed, never what the census happens to say.

The merge is exercised two ways: in-process, over configurations written here,
for what it keeps and what it refuses; and as the init container runs it, in
the server's image, ahead of the server, which then answers its callers. The
cases that run an image run the one `Pulumi.yaml` pins, with podman, as the pod
runs it: the declared command, user and environment, with every capability
dropped, on a read-only root that podman adds no scratch filesystem and no
password-file entry to, with a scratch filesystem at each mount. The callers
ask from a container of the curl image `Pulumi.yaml` pins, which holds the
network the others join and gives it the callers' addresses. They run as
`pinned_image` has it: skipped where podman is not on `PATH`, as in a cloud
session, and failed on CI's runner, which has podman; each image pulled by its
digest; and every container carrying `--rm`, a `--timeout` and the module's
label (framework/testing.md §1).

Literals written here are the upstream projects' contracts: the sections and
keys of qBittorrent's configuration at the pinned release, and those an older
release wrote (`MigrationVersion` 8, the interface's settings under
`Preferences`), the API path it answers its version at and the statuses it
answers a request with no sign-in, the `X-Forwarded-For` header it judges a
forwarded request by, the shape of seedwatch's report, Kubernetes' Pod Security
label, and the Gateway API's spellings.
"""

from __future__ import annotations

import json
import shlex
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import process_sessions
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from pinned_image import COMMAND_TIMEOUT, CONTAINER_TIMEOUT, PULL_TIMEOUT, pinned, pulled, require_podman

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import qbittorrent as qbittorrent_app
from kluster.components.apps.qbittorrent import Qbittorrent, seedwatch, settings
from kluster.components.apps.qbittorrent.seedwatch import Policy, Tracker, policy_file
from kluster.components.apps.qbittorrent.settings import OwnedSetting, Refused, merge
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.backup import BACKED_NAMESPACES, BackupFamily
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import (
    PROXIED,
    REWRITE,
    Balancer,
    Direct,
    Exposure,
    Gate,
    Port,
    Proxied,
    Route,
    SiteForward,
    Vip,
)
from kluster.lib.k8s import HOME_NODE_SELECTOR_JSON, POD_SECURITY_ENFORCE_LABEL, PodSecurity
from kluster.lib.versions import ImagePin

HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
SERVICE = 'kubernetes:core/v1:Service'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
NAMESPACE = 'kubernetes:core/v1:Namespace'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
VOLUME = 'kubernetes:core/v1:PersistentVolume'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'

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
ZONE_IDS = {conventions.ZONE_PRIMARY: 'the-primary-zones-id'}

#: The server's interface, gated from outside and at home, and its peer port.
PEER = Port('qb-peer', 41234, Transports.TCP_AND_UDP, internet=SiteForward())
SERVER = Exposure(routes=(Route('qb-ui', internet=Proxied(Gate.SESSION), lan=REWRITE),), ports=(PEER,))
#: seedwatch's interface, at home.
WATCH = Exposure(routes=(Route('qb-watch', lan=REWRITE),))

IMAGE = ImagePin('registry.example.invalid/qbittorrent', 'v9.9.9', f'sha256:{"7" * 64}')
WATCH_IMAGE = ImagePin('registry.example.invalid/seedwatch', 'v9.9.9', f'sha256:{"8" * 64}')
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'qb-id', 'AWS_SECRET_ACCESS_KEY': 'qb-secret'}

#: A policy with a string each of TOML's quoting has to carry: a category
#: outside ASCII, and a tracker whose name holds a quote.
POLICY = Policy(
    managed_categories=('动漫', 'films'),
    grace_hours=24,
    interval_minutes=60,
    dry_run=True,
    default_ratio=4,
    trackers={'tracker.example': Tracker(ratio=1.0), 'odd"name.example': Tracker(ratio=2.5, or_seed_days=30)},
)

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_apps_qbittorrent`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: Why the cases that run an image need podman.
WHY = 'the pods are run from the pinned images with it'

#: The pin of the curl image the callers ask from.
PROBER = 'auth-canary'

#: The environment variable prefix the files a container is handed travel in.
FILE_VARIABLE = 'KLUSTER_TEST_FILE_'

#: A configuration an older release wrote, as the host's profile holds it: the
#: interface's settings under `Preferences`, which the pinned server moves to
#: `WebUI` when it starts, each at a value the pod cannot run with -- another
#: bind address, no reverse proxy, no subnet allowed without a sign-in, a
#: sign-in asked of loopback, and names the Gateways and the Service are not --
#: beside settings the component does not own.
KEPT = """\
[Application]
FileLogger\\Enabled=true
FileLogger\\Path=/var/lib/qBittorrent/qBittorrent/data/logs

[AutoRun]
program=/mnt/nas/Downloads/move.sh -N \\"%N\\" -F \\"%F\\"

[BitTorrent]
Session\\DefaultSavePath=/mnt/nas
Session\\Port=53363

[LegalNotice]
Accepted=true

[Meta]
MigrationVersion=8

[Preferences]
Connection\\UPnP=true
WebUI\\Address=192.0.2.7
WebUI\\AuthSubnetWhitelistEnabled=false
WebUI\\HostHeaderValidation=true
WebUI\\LocalHostAuth=true
WebUI\\Port=9876
WebUI\\ReverseProxySupportEnabled=false
WebUI\\ServerDomains=elsewhere.example
WebUI\\TrustedReverseProxiesList=
WebUI\\UseUPnP=true
"""

#: The bound on a case that runs the server: the pulls, the merge and the
#: server's start, which take seconds. A stop-loss, which nothing asserts on.
SERVER_CASE_TIMEOUT = 2 * PULL_TIMEOUT + 6 * COMMAND_TIMEOUT + 60
#: The same, with seedwatch's pull and its first pass beside it.
WATCH_CASE_TIMEOUT = 3 * PULL_TIMEOUT + 8 * COMMAND_TIMEOUT + 60


async def _declared(exposure: Exposure = SERVER, watch: Exposure = WATCH) -> tuple[Recorder, Qbittorrent]:
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        component = Qbittorrent(
            'qbittorrent',
            exposure=exposure,
            seedwatch_exposure=watch,
            policy=POLICY,
            image=IMAGE,
            seedwatch_image=WATCH_IMAGE,
            writer_key=WRITER_KEY,
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        )
    return monitor, component


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='declared_pair')
async def declared_pair_fixture() -> tuple[Recorder, Qbittorrent]:
    """The server and seedwatch, as the `apps` program declares them, and the component."""
    return await _declared()


@pytest.fixture(scope='module')
def declared(declared_pair: tuple[Recorder, Qbittorrent]) -> Recorder:
    return declared_pair[0]


def in_namespace(monitor: Recorder, typ: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == qbittorrent_app.NAME]


def named(monitor: Recorder, typ: str, name: str) -> dict[str, Any]:
    (found,) = [it for it in in_namespace(monitor, typ) if it['metadata'].get('name') == name]
    return found


def pod_of(monitor: Recorder, app: str) -> dict[str, Any]:
    return named(monitor, DEPLOYMENT, app)['spec']['template']


def container(monitor: Recorder, app: str) -> dict[str, Any]:
    (found,) = pod_of(monitor, app)['spec']['containers']
    return found


def init(monitor: Recorder) -> dict[str, Any]:
    (found,) = pod_of(monitor, qbittorrent_app.NAME)['spec']['initContainers']
    return found


def environment(monitor: Recorder, app: str) -> dict[str, str]:
    return {it['name']: it['value'] for it in container(monitor, app).get('env', []) if 'value' in it}


def option(monitor: Recorder, name: str) -> str:
    """The value of the server's command-line option `--<name>=`."""
    (found,) = [
        it.split('=', 1)[1] for it in container(monitor, qbittorrent_app.NAME)['command'] if it.startswith(f'--{name}=')
    ]
    return found


def volume_claims(pod: dict[str, Any]) -> dict[str, str]:
    """The claim each of the pod's volumes names, by volume."""
    return {
        it['name']: it['persistentVolumeClaim']['claimName']
        for it in pod['spec']['volumes']
        if 'persistentVolumeClaim' in it
    }


def mount_of(monitor: Recorder, app: str, claim_name: str) -> dict[str, Any]:
    """The app's container's one mount of the volume over `claim_name`."""
    by_volume = volume_claims(pod_of(monitor, app))
    (mount,) = [it for it in container(monitor, app)['volumeMounts'] if by_volume.get(it['name']) == claim_name]
    return mount


def nas_volume_of(monitor: Recorder, app: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The app's one NAS volume, and its container's mount of it."""
    claims = set(volume_claims(pod_of(monitor, app)).values())
    (volume,) = [it.inputs for it in monitor.of_type(VOLUME) if it.inputs['spec']['claimRef']['name'] in claims]
    return volume, mount_of(monitor, app, volume['spec']['claimRef']['name'])


def source_of(monitor: Recorder, app: str) -> dict[str, Any]:
    """The restic source of the claim the app's pod mounts."""
    claims = set(volume_claims(pod_of(monitor, app)).values())
    (found,) = [it for it in in_namespace(monitor, SOURCE) if it['spec']['sourcePVC'] in claims]
    return found


def config_map_files(monitor: Recorder, file: str) -> dict[str, str]:
    """The files of the namespace's one ConfigMap that holds `file`, by name."""
    (found,) = [it['data'] for it in in_namespace(monitor, CONFIG_MAP) if file in it['data']]
    return found


def owned(monitor: Recorder) -> tuple[OwnedSetting, ...]:
    """The owned settings the init container reads, as the component rendered them."""
    return settings.read_owned(config_map_files(monitor, 'owned.json')['owned.json'])


def parsed(configuration: str) -> dict[str, dict[str, str]]:
    """Each section's settings, by INI key, as the server reads the file it wrote."""
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in configuration.splitlines():
        if line.startswith('['):
            current = sections.setdefault(line.strip('[]'), {})
        elif '=' in line:
            key, _, value = line.partition('=')
            current[key] = value
    return sections


def at(configuration: dict[str, dict[str, str]], key: str) -> str | None:
    """The value a setting of the server's name, `Section/Key`, holds in the parsed file."""
    section, _, rest = key.partition('/')
    return configuration.get(section, {}).get(rest.replace('/', '\\'))


def interface_route(monitor: Recorder, gateway: str, host: str) -> dict[str, Any]:
    (found,) = [
        it
        for it in in_namespace(monitor, HTTP_ROUTE)
        if [ref['name'] for ref in it['spec']['parentRefs']] == [gateway]
        and it['spec']['hostnames'] == [f'{host}.{conventions.ZONE_PRIMARY}']
    ]
    return found


# -- the pods ------------------------------------------------------------------


@pytest.mark.parametrize('app', [qbittorrent_app.NAME, seedwatch.NAME])
def test_each_pod_runs_as_a_numeric_user_that_is_not_root_and_escalates_nothing(declared: Recorder, app: str) -> None:
    """`restricted`, which the namespace is held to, refuses a pod that runs as root or as a name it cannot check."""
    (namespace,) = [it.inputs for it in declared.of_type(NAMESPACE)]
    spec = pod_of(declared, app)['spec']
    context = spec['securityContext']

    assert namespace['metadata']['labels'][POD_SECURITY_ENFORCE_LABEL] == PodSecurity.RESTRICTED
    assert context['runAsNonRoot'] is True
    # The mock hands numbers back as floats, so the user is held to a whole one.
    assert context['runAsUser'] == int(context['runAsUser']) > 0
    assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
    for each in [*spec.get('initContainers', []), *spec['containers']]:
        assert each['securityContext']['allowPrivilegeEscalation'] is False
        assert each['securityContext']['capabilities'] == {'drop': ['ALL']}
        assert each['securityContext']['readOnlyRootFilesystem'] is True


def test_each_container_runs_the_pin_it_is_handed(declared: Recorder) -> None:
    assert init(declared)['image'] == container(declared, qbittorrent_app.NAME)['image'] == str(IMAGE)
    assert container(declared, seedwatch.NAME)['image'] == str(WATCH_IMAGE)


@pytest.mark.parametrize('app', [qbittorrent_app.NAME, seedwatch.NAME])
def test_each_probe_asks_a_port_the_container_listens_on(declared: Recorder, app: str) -> None:
    serving = container(declared, app)
    ports = {it['name'] for it in serving['ports']}

    for probe in ('startupProbe', 'readinessProbe', 'livenessProbe'):
        if probe in serving:
            assert serving[probe]['httpGet']['port'] in ports, probe


@pytest.mark.parametrize('app', [qbittorrent_app.NAME, seedwatch.NAME])
def test_neither_never_runs_beside_itself(declared: Recorder, app: str) -> None:
    """The profile is one process's, and two reconcilers would reap the same torrents."""
    assert named(declared, DEPLOYMENT, app)['spec']['strategy']['type'] == 'Recreate'


def test_the_server_is_held_at_home_by_its_bulk_egress(declared_pair: tuple[Recorder, Qbittorrent]) -> None:
    """Seeding leaves through the uplink it runs behind, so the declaration holds the server at home with its peer port."""
    monitor, component = declared_pair
    site = component.constraints.site

    assert site is not None
    assert any(qbittorrent_app.BULK_EGRESS.reason in it for it in site.declarations)
    for app in (qbittorrent_app.NAME, seedwatch.NAME):
        affinity = pod_of(monitor, app)['spec']['affinity']['nodeAffinity']
        assert affinity['requiredDuringSchedulingIgnoredDuringExecution'] == HOME_NODE_SELECTOR_JSON, app


# -- the exposures -------------------------------------------------------------


def test_the_interface_is_gated_from_outside_and_open_at_home(declared: Recorder) -> None:
    """Each Gateway the row sets forwards the name to the server's Service, the portal's gate on `internet`'s alone."""
    service = named(declared, SERVICE, qbittorrent_app.NAME)
    (port,) = service['spec']['ports']
    ports = {it['name']: it['containerPort'] for it in container(declared, qbittorrent_app.NAME)['ports']}
    labels = pod_of(declared, qbittorrent_app.NAME)['metadata']['labels']

    for gateway, gated in ((conventions.GATEWAY_INTERNET, True), (conventions.GATEWAY_LAN, False)):
        (rule,) = interface_route(declared, gateway, 'qb-ui')['spec']['rules']
        assert rule['backendRefs'] == [{'name': service['metadata']['name'], 'port': port['port']}]
        assert any(it['type'] == 'ExternalAuth' for it in rule.get('filters', [])) is gated, gateway
    assert service['spec']['selector'].items() <= labels.items()
    assert ports[port['targetPort']] == port['port']


def test_the_server_takes_peers_on_the_exposures_port(declared: Recorder) -> None:
    """The peer port's Service forwards to the server's pods, and the server listens there on both transports."""
    (peer,) = [it for it in in_namespace(declared, SERVICE) if it['metadata']['name'].startswith(PEER.name)]
    labels = pod_of(declared, qbittorrent_app.NAME)['metadata']['labels']
    listened = {
        (it['containerPort'], it['protocol'])
        for it in container(declared, qbittorrent_app.NAME)['ports']
        if it['containerPort'] != int(option(declared, 'webui-port'))
    }

    assert int(option(declared, 'torrenting-port')) == PEER.port
    assert peer['spec']['selector'].items() <= labels.items()
    assert listened == {(PEER.port, 'TCP'), (PEER.port, 'UDP')}


def test_seedwatchs_interface_is_reached_at_home_ungated(declared: Recorder) -> None:
    service = named(declared, SERVICE, seedwatch.NAME)
    (port,) = service['spec']['ports']
    (rule,) = interface_route(declared, conventions.GATEWAY_LAN, 'qb-watch')['spec']['rules']
    labels = pod_of(declared, seedwatch.NAME)['metadata']['labels']

    assert rule['backendRefs'] == [{'name': service['metadata']['name'], 'port': port['port']}]
    assert [it for it in rule.get('filters', []) if it['type'] == 'ExternalAuth'] == []
    assert service['spec']['selector'].items() <= labels.items()
    assert int(environment(declared, seedwatch.NAME)['PORT']) == port['port']


# -- seedwatch's way to the server ---------------------------------------------


def test_seedwatch_calls_the_servers_service_on_the_port_the_server_answers_at(declared: Recorder) -> None:
    """The server refuses a request whose `Host` names a port other than its own, so the Service forwards on it unchanged."""
    url = urlsplit(environment(declared, seedwatch.NAME)['QB_URL'])
    service = named(declared, SERVICE, qbittorrent_app.NAME)
    (port,) = service['spec']['ports']

    assert url.hostname == service['metadata']['name']
    assert url.port == port['port'] == int(option(declared, 'webui-port'))


def test_the_server_answers_at_seedwatchs_name_and_at_each_of_the_routes(declared: Recorder) -> None:
    """The names the server answers at are each that a request carries: the route's, and the one seedwatch dials."""
    (domains,) = [it.value for it in owned(declared) if it.key == 'WebUI/ServerDomains']
    answered = domains.strip('"').split(';')

    assert urlsplit(environment(declared, seedwatch.NAME)['QB_URL']).hostname in answered
    assert f'qb-ui.{conventions.ZONE_PRIMARY}' in answered


def test_seedwatch_stats_the_seeding_tree_at_the_path_the_server_reports_it_under(declared: Recorder) -> None:
    """seedwatch stats each file at the server's path for it, so its mount of the tree is where the server's export puts it."""
    server_volume, server_mount = nas_volume_of(declared, qbittorrent_app.NAME)
    watch_volume, watch_mount = nas_volume_of(declared, seedwatch.NAME)
    under = PurePosixPath(watch_volume['spec']['nfs']['path']).relative_to(server_volume['spec']['nfs']['path'])
    policy = tomllib.loads(config_map_files(declared, 'config.toml')['config.toml'])

    assert watch_mount['mountPath'] == str(PurePosixPath(server_mount['mountPath']) / under)
    assert policy['scan']['downloads'] == watch_mount['mountPath']
    assert watch_mount['readOnly'] is True
    assert watch_volume['spec']['nfs']['readOnly'] is True


def test_seedwatch_reads_its_policy_where_the_environment_says(declared: Recorder) -> None:
    mounts = {it['mountPath']: it for it in container(declared, seedwatch.NAME)['volumeMounts']}

    assert mounts[environment(declared, seedwatch.NAME)['SEEDWATCH_CONFIG']]['readOnly'] is True


# -- the claims ------------------------------------------------------------------


def test_the_namespace_is_one_whose_writer_key_is_sealed() -> None:
    assert BackupFamily.VOLSYNC in BACKED_NAMESPACES[qbittorrent_app.NAME]


def test_the_profile_is_the_backed_up_claim_the_server_runs_on(declared: Recorder) -> None:
    """The server keeps its profile where its command line says, and a mover reads it as the user that wrote it."""
    pod = pod_of(declared, qbittorrent_app.NAME)
    source = source_of(declared, qbittorrent_app.NAME)
    mount = mount_of(declared, qbittorrent_app.NAME, source['spec']['sourcePVC'])

    assert mount['mountPath'] == option(declared, 'profile')
    assert init(declared)['command'][2].startswith(f'{mount["mountPath"]}/')
    assert source['spec']['restic']['moverSecurityContext']['runAsUser'] == pod['spec']['securityContext']['runAsUser']


def test_seedwatchs_logs_are_the_backed_up_claim_at_its_data_directory(declared: Recorder) -> None:
    pod = pod_of(declared, seedwatch.NAME)
    source = source_of(declared, seedwatch.NAME)
    mount = mount_of(declared, seedwatch.NAME, source['spec']['sourcePVC'])

    assert mount['mountPath'] == environment(declared, seedwatch.NAME)['DATA_DIR']
    assert source['spec']['restic']['moverSecurityContext']['runAsUser'] == pod['spec']['securityContext']['runAsUser']


def test_the_server_mounts_the_whole_export_writable_at_the_path_the_host_saw_it_at(declared: Recorder) -> None:
    """The profile names every payload by the host's path, and a hard link into `Media` takes one mount of the export."""
    volume, mount = nas_volume_of(declared, qbittorrent_app.NAME)

    assert volume['spec']['nfs']['path'] == conventions.NAS_EXPORT
    assert mount['mountPath'] == volume['spec']['nfs']['path']
    assert volume['spec']['nfs']['readOnly'] is False
    assert not mount.get('readOnly')


# -- the policy file -------------------------------------------------------------


def test_the_policy_file_reads_back_as_the_policy() -> None:
    """Each setting seedwatch reads from the file is the policy's, the tree the server's."""
    read = tomllib.loads(policy_file(POLICY, downloads='/srv/tree'))

    assert read['scan'] == {
        'downloads': '/srv/tree',
        'managed_categories': list(POLICY.managed_categories),
        'grace_hours': POLICY.grace_hours,
        'interval_minutes': POLICY.interval_minutes,
    }
    assert read['reap'] == {
        'dry_run': POLICY.dry_run,
        'default_ratio': POLICY.default_ratio,
        'trackers': {
            host: {'ratio': it.ratio} | ({} if it.or_seed_days is None else {'or_seed_days': it.or_seed_days})
            for host, it in POLICY.trackers.items()
        },
    }


def test_a_policy_with_a_number_the_file_cannot_hold_is_refused() -> None:
    with pytest.raises(ValueError, match='finite'):
        _ = Policy(managed_categories=(), grace_hours=float('inf'), interval_minutes=60, dry_run=True, default_ratio=4)


# -- the merge, in-process ------------------------------------------------------


@pytest.fixture(scope='module', name='kept_merged')
def kept_merged_fixture(declared: Recorder) -> dict[str, dict[str, str]]:
    return parsed(merge(KEPT, owned(declared)))


def test_the_merge_sets_each_owned_setting_where_the_pinned_server_reads_it(
    declared: Recorder, kept_merged: dict[str, dict[str, str]]
) -> None:
    for setting in owned(declared):
        assert at(kept_merged, setting.key) == setting.value, setting.key


def test_the_merge_removes_each_key_the_servers_migration_would_move_over_an_owned_one(
    declared: Recorder, kept_merged: dict[str, dict[str, str]]
) -> None:
    """The server moves an older release's key over the newer one when it starts, so one left behind would undo the merge."""
    kept = parsed(KEPT)
    replaced = [key for setting in owned(declared) for key in setting.replaces]

    assert [key for key in replaced if at(kept, key) is not None] != []
    assert [key for key in replaced if at(kept_merged, key) is not None] == []


def test_the_merge_keeps_every_other_line_where_the_profile_had_it(declared: Recorder) -> None:
    """A setting the component does not own, the command run on completion among them, stays as the file holds it, in its order."""
    settings_owned = owned(declared)
    touched = {key for it in settings_owned for key in (it.key, *it.replaces)}

    def untouched(text: str) -> list[str]:
        section = ''
        lines: list[str] = []
        for line in text.splitlines():
            if line.startswith('['):
                section = line.strip('[]')
            elif '=' in line and f'{section}/{line.partition("=")[0].replace(chr(92), "/")}' not in touched:
                lines.append(f'{section}: {line}')
        return lines

    assert untouched(merge(KEPT, settings_owned)) == untouched(KEPT)
    assert 'AutoRun: program=/mnt/nas/Downloads/move.sh -N \\"%N\\" -F \\"%F\\"' in untouched(KEPT)


def test_a_server_with_no_configuration_takes_the_owned_settings_as_its_initial_state(declared: Recorder) -> None:
    result = parsed(merge(None, owned(declared)))

    assert {(it.key, at(result, it.key)) for it in owned(declared)} == {(it.key, it.value) for it in owned(declared)}
    assert sum(len(it) for it in result.values()) == len(owned(declared))


def test_merging_twice_changes_nothing_the_first_merge_wrote(declared: Recorder) -> None:
    once = merge(KEPT, owned(declared))

    assert merge(once, owned(declared)) == once


@pytest.mark.parametrize(
    'configuration',
    [
        pytest.param('', id='empty'),
        pytest.param(KEPT[: KEPT.index('WebUI\\Port=') + len('WebUI\\Po')], id='cut-within-a-line'),
        pytest.param(KEPT.replace('[Meta]\n', '[Meta]\nnot a setting\n'), id='a-line-of-no-shape'),
    ],
)
def test_the_merge_refuses_a_configuration_not_in_the_shape_the_server_writes(
    declared: Recorder, configuration: str
) -> None:
    """The server would read such a file as fewer settings than it kept, and a merge over it would write that back whole."""
    with pytest.raises(Refused):
        _ = merge(configuration, owned(declared))


# -- the server, as the pod runs it ------------------------------------------------


def lay_out(files: Mapping[str, str]) -> tuple[list[str], str]:
    """The `podman run` options handing a container `files`, by path, and the shell lines that write each at its path."""
    options = [f'--env={FILE_VARIABLE}{n}={contents}' for n, contents in enumerate(files.values())]
    lines = [
        f'mkdir -p {shlex.quote(str(PurePosixPath(path).parent))} && printf %s "${FILE_VARIABLE}{n}" >{shlex.quote(path)}'
        for n, path in enumerate(files)
    ]
    return options, '\n'.join(lines)


def isolated(name: str, user: int, mounted: Sequence[str], *, network: str) -> list[str]:
    """The options of a `podman run` as a pod runs a container: no capability, a read-only root, a scratch filesystem at each of `mounted`."""
    return [
        '--detach',
        '--rm',
        f'--timeout={CONTAINER_TIMEOUT}',
        f'--label={LABEL}',
        process_sessions.LOG_TO_FILE,
        f'--name={name}',
        f'--network={network}',
        '--read-only',
        # The kubelet adds no scratch filesystem to a read-only root but
        # `/dev/shm`, and no entry to the image's password file.
        '--read-only-tmpfs=false',
        '--passwd=false',
        '--mount=type=tmpfs,destination=/dev/shm,tmpfs-mode=1777',
        '--cap-drop=all',
        '--security-opt=no-new-privileges',
        f'--user={user}:{user}',
        *(f'--mount=type=tmpfs,destination={path},tmpfs-mode=1777' for path in mounted),
    ]


def start_prober(prober: str, addresses: Sequence[str], *, network: str = 'none') -> None:
    """The container the callers ask from, which gives the network it is in each of `addresses`: its own, or `network`."""
    _ = process_sessions.run(
        [
            'podman', 'run', '--detach', '--rm', f'--timeout={CONTAINER_TIMEOUT}', f'--label={LABEL}',
            process_sessions.LOG_TO_FILE, f'--name={prober}', f'--network={network}', '--user=0:0', '--cap-drop=all',
            '--cap-add=NET_ADMIN', '--entrypoint=sleep', pulled(pinned(PROBER)), str(CONTAINER_TIMEOUT),
        ],
        timeout=COMMAND_TIMEOUT,
        check=True,
    )  # fmt: skip
    for address in addresses:
        _ = process_sessions.run(
            ['podman', 'exec', prober, 'ip', 'address', 'add', f'{address}/32', 'dev', 'lo'],
            timeout=COMMAND_TIMEOUT,
            check=True,
        )


def start_server(monitor: Recorder, name: str, network: str) -> None:
    """The server's pod, the init container's merge over `KEPT` then the server, in the network of the container `network`."""
    reference = pulled(pinned('qbittorrent'))
    pod = pod_of(monitor, qbittorrent_app.NAME)
    merging = init(monitor)
    serving = container(monitor, qbittorrent_app.NAME)
    (rendered,) = [it['mountPath'] for it in merging['volumeMounts'] if it['name'] == 'rendered']
    files = {f'{rendered}/{file}': contents for file, contents in config_map_files(monitor, 'owned.json').items()} | {
        merging['command'][2]: KEPT
    }
    options, layout = lay_out(files)
    mounted = {rendered, *(it['mountPath'] for it in serving['volumeMounts'])}
    _ = process_sessions.run(
        [
            'podman',
            'run',
            *isolated(
                name, int(pod['spec']['securityContext']['runAsUser']), sorted(mounted), network=f'container:{network}'
            ),
            *options,
            '--entrypoint=/bin/sh',
            reference,
            '-c',
            f'set -e\n{layout}\n{shlex.join(merging["command"])}\nexec {shlex.join(serving["command"])}',
        ],
        timeout=COMMAND_TIMEOUT,
        check=True,
    )


def asked(prober: str, url: str, source: str, headers: Mapping[str, str]) -> str:
    """The status `url` answers the prober's curl from `source` with `headers`, once the server answers at all."""
    sent = ' '.join(f'-H {shlex.quote(f"{key}: {value}")}' for key, value in headers.items())
    return (
        process_sessions.run(
            [
                'podman',
                'exec',
                prober,
                'sh',
                '-c',
                f'until code=$(curl -s -o /dev/null -w "%{{http_code}}" --interface {source} {sent} {shlex.quote(url)}) '
                f'&& [ "$code" != 000 ]; do sleep 0.2; done; echo "$code"',
            ],
            timeout=COMMAND_TIMEOUT + 30,
            check=True,
        )
        .stdout.decode()
        .strip()
    )


@pytest.mark.timeout(SERVER_CASE_TIMEOUT)
def test_the_server_answers_seedwatch_without_a_sign_in_and_asks_one_of_a_forwarded_client(declared: Recorder) -> None:
    """Run as the pod runs it after the merge over an older release's profile, the server tells seedwatch from a client.

    Each caller asks from an address of its own in the network the server's
    container shares. seedwatch calls from an address in the pod ranges with no
    forwarded address, at the `Host` its URL names, and is answered. Envoy
    connects from the pod ranges too and appends the client to
    `X-Forwarded-For`, behind a pod-range address the client wrote there
    itself; the server judges the request by the client, a home address, and
    asks it to sign in. The kubelet probes from the address `cilium_host`
    holds, and the interface answers its probe. `KEPT` disables all of it
    under the keys an older release used, which the server moves to the
    current ones as it starts, so a merge that left them would leave seedwatch
    refused; it also names another port, a bind address the pod does not have,
    and names the requests do not carry, so a server that read it unmerged
    would answer none.
    """
    require_podman(WHY)
    probe = container(declared, qbittorrent_app.NAME)['readinessProbe']['httpGet']
    port = int(option(declared, 'webui-port'))
    pod_address, cilium_host, envoy, watcher = (str(conventions.POD_CIDR_V4[n]) for n in (10, 1, 20, 30))
    forged = str(conventions.POD_CIDR_V4[99])
    seedwatch_host = urlsplit(environment(declared, seedwatch.NAME)['QB_URL']).netloc
    version = f'http://{pod_address}:{port}/api/v2/app/version'
    run = uuid.uuid4().hex[:12]
    name, prober = f'kluster-test-qbittorrent-{run}', f'kluster-test-qbittorrent-prober-{run}'

    with process_sessions.removing(name, prober, timeout=COMMAND_TIMEOUT):
        start_prober(prober, [pod_address, cilium_host, envoy, watcher])
        start_server(declared, name, prober)
        answers = {
            'kubelet': asked(prober, f'http://{pod_address}:{port}{probe["path"]}', cilium_host, {}),
            'seedwatch': asked(prober, version, watcher, {'Host': seedwatch_host}),
            'forwarded': asked(
                prober,
                version,
                envoy,
                {'Host': f'qb-ui.{conventions.ZONE_PRIMARY}', 'X-Forwarded-For': f'{forged}, 192.168.1.50'},
            ),
        }

    assert answers == {'kubelet': '200', 'seedwatch': '200', 'forwarded': '403'}


@pytest.mark.timeout(WATCH_CASE_TIMEOUT)
def test_seedwatch_run_as_its_pod_runs_it_reconciles_against_the_server(declared: Recorder) -> None:
    """seedwatch, run as its pod runs it beside the server, reads its policy and finishes a pass through the server's API.

    It reaches the server at the name its URL gives, which its container
    resolves to the server's address, with no credential: a pass that
    finishes is one whose every call the server answered. Its report carries
    the policy's `dry_run`, so it read the rendered file. Its container holds
    the network the others join, the one podman lets a host entry be added to,
    and it starts once the server answers, since a pass that fails waits out
    the policy's interval before the next.
    """
    require_podman(WHY)
    watch_reference = pulled(pinned('seedwatch'))
    pod = pod_of(declared, seedwatch.NAME)
    serving = container(declared, seedwatch.NAME)
    env = environment(declared, seedwatch.NAME)
    cmd = json.loads(
        process_sessions.run(
            ['podman', 'image', 'inspect', '--format', '{{json .Config.Cmd}}', watch_reference],
            timeout=COMMAND_TIMEOUT,
            check=True,
        ).stdout
    )
    config = env['SEEDWATCH_CONFIG']
    options, layout = lay_out({config: config_map_files(declared, 'config.toml')['config.toml']})
    mounted = {
        str(PurePosixPath(config).parent),
        *(it['mountPath'] for it in serving['volumeMounts'] if it['mountPath'] != config),
    }
    pod_address = str(conventions.POD_CIDR_V4[10])
    answers = (
        'import time, urllib.request\n'
        'while True:\n'
        '    try:\n'
        f'        urllib.request.urlopen({env["QB_URL"]!r}, timeout=1)\n'
        '        break\n'
        '    except OSError:\n'
        '        time.sleep(0.2)'
    )
    run = uuid.uuid4().hex[:12]
    server, watch, prober = (f'kluster-test-qbittorrent-{part}-{run}' for part in ('server', 'seedwatch', 'prober'))

    with process_sessions.removing(server, prober, watch, timeout=COMMAND_TIMEOUT):
        _ = process_sessions.run(
            [
                'podman',
                'run',
                *isolated(watch, int(pod['spec']['securityContext']['runAsUser']), sorted(mounted), network='none'),
                # The server's Service, by name, at the server's address.
                f'--add-host={urlsplit(env["QB_URL"]).hostname}:{pod_address}',
                *options,
                *(f'--env={key}={value}' for key, value in env.items()),
                '--entrypoint=/bin/sh',
                watch_reference,
                '-c',
                f'set -e\n{layout}\npython -c {shlex.quote(answers)}\nexec {shlex.join(cmd)}',
            ],
            timeout=COMMAND_TIMEOUT,
            check=True,
        )
        start_prober(prober, [pod_address], network=f'container:{watch}')
        start_server(declared, server, watch)
        report = f'http://127.0.0.1:{env["PORT"]}{serving["readinessProbe"]["httpGet"]["path"]}'
        answered = process_sessions.run(
            [
                'podman',
                'exec',
                prober,
                'sh',
                '-c',
                f'until body=$(curl -sf {report}) && echo "$body" | grep -q \'"report":{{\\|"error":{{\'; '
                f'do sleep 0.5; done; echo "$body"',
            ],
            timeout=COMMAND_TIMEOUT + 60,
            check=True,
        )
        shown = process_sessions.run(['podman', 'logs', watch], timeout=COMMAND_TIMEOUT)
        logs = (shown.stdout + shown.stderr).decode()

    body = json.loads(answered.stdout)
    assert body['error'] is None, logs
    assert body['report'] is not None
    assert body['dry_run'] is POLICY.dry_run


# -- refusals --------------------------------------------------------------------


def _refused(exposure: Exposure = SERVER, watch: Exposure = WATCH) -> None:
    _ = Qbittorrent(
        'qbittorrent',
        exposure=exposure,
        seedwatch_exposure=watch,
        policy=POLICY,
        image=IMAGE,
        seedwatch_image=WATCH_IMAGE,
        writer_key=WRITER_KEY,
        fronts=FRONTS,
        zone_ids=ZONE_IDS,
    )


@pytest.mark.parametrize(
    'internet',
    [pytest.param(PROXIED, id='proxied'), pytest.param(Direct('a test of the refusal'), id='direct')],
)
def test_an_ungated_internet_side_on_either_interface_is_refused(internet: Proxied | Direct) -> None:
    """The server can run a program and seedwatch deletes torrents, so from outside each is behind the gate or not reached."""
    with pytest.raises(ValueError, match='with no gate'):
        _refused(exposure=Exposure(routes=(Route('qb-ui', internet=internet, lan=REWRITE),), ports=(PEER,)))
    with pytest.raises(ValueError, match='with no gate'):
        _refused(watch=Exposure(routes=(Route('qb-watch', internet=internet, lan=REWRITE),)))


@pytest.mark.parametrize(
    ('peer', 'refusal'),
    [
        pytest.param(Port('qb-peer', 41234, Transports.TCP, internet=SiteForward()), 'over TCP and over UDP', id='tcp'),
        pytest.param(
            Port('qb-peer', 41234, Transports.TCP_AND_UDP, internet=Balancer()), "site's forward", id='balancer'
        ),
        pytest.param(
            Port('qb-peer', 41234, Transports.TCP_AND_UDP, lan=Vip('qb-peer')), "site's forward", id='home-alone'
        ),
    ],
)
def test_a_peer_port_other_than_both_transports_through_the_sites_forward_is_refused(peer: Port, refusal: str) -> None:
    with pytest.raises(ValueError, match=refusal):
        _refused(exposure=Exposure(routes=SERVER.routes, ports=(peer,)))


def test_an_exposure_other_than_one_route_and_one_peer_port_is_refused() -> None:
    with pytest.raises(ValueError, match='one name'):
        _refused(exposure=Exposure(routes=(*SERVER.routes, Route('qb-other', lan=REWRITE)), ports=(PEER,)))
    with pytest.raises(ValueError, match='one raw port'):
        _refused(exposure=Exposure(routes=SERVER.routes))
    with pytest.raises(ValueError, match='one route row alone'):
        _refused(watch=Exposure(routes=WATCH.routes, ports=(Port('qb-w', 1, Transports.TCP, lan=Vip('qb-w')),)))
