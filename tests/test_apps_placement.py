"""Every application component, and the platform's stores, held to the relations of their node constraints at once.

One run declares every application component `kluster.components.apps`
holds, each with rows, pins and ciphertexts of the test's own, and beside it
the `k8s-base` program as its stack file holds it, for the monitoring stores'
values. Every pod the run declares is read the way the scheduler reads it,
against the nodes as they are labelled (`node_selection`): each workload's pod
template, each CronJob's, each database instance and the restore drill's
recovered one, and each restic mover. So a component that forgets what its
declaration derives, or what its claim records for its pod, fails here
whichever component it is:

-   a pod of a component that declares it keeps working while the uplink is
    down admits no cloud node, and a declaring client of the portal requires
    the portal to declare too (cluster/architecture.md §1.1);
-   a pod that mounts a NAS claim admits exactly the nodes its volume does;
-   a restic mover admits exactly the nodes its workload does, the GPU its
    workload requests included, and the mover of a workload that requests
    the GPU admits only nodes that offer it;
-   a pod that mounts a `local-path` claim writes in the blockio class
    `conventions.ETCD_DISK_WRITER`, and a mover in none;
-   a pod a volume or a device keeps to one node runs at the held priority;
-   a pod that serves a client of the portal reaches the issuer's name at the
    issuer's addresses.

Literals written here are the upstream projects' contracts: the key the GPU's
device plugin offers, Kubernetes' and CloudNativePG's field names, VolSync's.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import image_busybox
import pytest
import pytest_asyncio
from component_census import components, stated
from k8s_base_installation import Run, applied
from mock_monitor import Recorder, declaring, run_with
from node_selection import CLOUD, NODES, schedulable, schedulable_among, selected

from kluster import conventions
from kluster.components.apps import UPLINK_DOWN, Fronts
from kluster.components.apps import authelia as authelia_app
from kluster.components.apps import emailproxy as emailproxy_app
from kluster.components.apps import exim as exim_app
from kluster.components.apps import immich as immich_app
from kluster.components.apps import matrix as matrix_app
from kluster.components.apps import splitpro as splitpro_app
from kluster.components.apps.authelia import Authelia
from kluster.components.apps.echo import Echo
from kluster.components.apps.emailproxy import EmailProxy
from kluster.components.apps.exim import Exim
from kluster.components.apps.golinks import Golinks
from kluster.components.apps.immich import Immich
from kluster.components.apps.jellyfin import Jellyfin
from kluster.components.apps.matrix import Matrix
from kluster.components.apps.shoko import Shoko
from kluster.components.apps.splitpro import SplitPro
from kluster.components.apps.spoolman import Spoolman
from kluster.components.apps.static_site import GitSource, NasSource, StaticSite
from kluster.components.apps.stdiscosrv import Stdiscosrv
from kluster.components.apps.syncthing import Syncthing
from kluster.components.apps.syncthing.webdav import WebDav
from kluster.components.cilium import InternetPoolMembers
from kluster.components.gpu import GPU_NODE, GPU_RESOURCE
from kluster.components.monitoring.operator_objects import type_token
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import (
    PROXIED,
    REWRITE,
    Apex,
    Balancer,
    Direct,
    Exposure,
    Gate,
    Heard,
    Held,
    Host,
    Port,
    PortRedirect,
    Proxied,
    Route,
    Vip,
)
from kluster.conventions.sso import Client
from kluster.lib.k8s import Constraints
from kluster.lib.versions import ImagePin

DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
CRONJOB = 'kubernetes:batch/v1:CronJob'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
VOLUME = 'kubernetes:core/v1:PersistentVolume'
CLUSTER = 'crds:postgresql.cnpg.io/v1:Cluster'
SERVICE = 'kubernetes:core/v1:Service'
HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'
VM_SINGLE = type_token('VMSingle')

#: The type tokens of the application components, which `kluster.components.apps` defines.
APPS = 'kluster:apps:'

#: The monitoring component's chart, whose values hold the recent store.
MONITORING_CHART = 'monitoring'

#: The key of the restore drill's ConfigMap that holds its recovered cluster.
DRILL_MANIFEST = 'cluster.json'

#: Each of the cluster's nodes twice, by its name and whether it offers the
#: GPU: without the label the GPU plugin serves its nodes by
#: (`kluster.components.gpu.GPU_NODE`) and with it. Which node carries the GPU
#: is a fact of the hardware, so a pod's constraints are read on both, beside
#: every node they otherwise admit.
WITH_AND_WITHOUT_GPU: dict[tuple[str, bool], dict[str, str]] = {
    (node, gpu): {**labels, **({GPU_NODE.key: GPU_NODE.value} if gpu else {})}
    for node, labels in NODES.items()
    for gpu in (False, True)
}

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
ZONE_IDS = {zone: f'{zone}-id' for zone in (conventions.ZONE_PRIMARY, 'blog.example', 'doors.example')}


def pin(name: str) -> ImagePin:
    """A pin of the test's own: a reference that is no published image."""
    return ImagePin(f'registry.example.invalid/{name}', 'v9.9.9', f'sha256:{"a" * 64}')


def sealed(keys: tuple[str, ...]) -> dict[str, str]:
    """Ciphertext of each key, which names the key."""
    return {key: f'sealed-{key}' for key in keys}


def writer_key(namespace: str) -> dict[str, str]:
    """A namespace's sealed writer key, its ciphertext naming the namespace."""
    return {'AWS_ACCESS_KEY_ID': f'{namespace}-id', 'AWS_SECRET_ACCESS_KEY': f'{namespace}-secret'}


def client(name: str) -> Client:
    """The census's client of that name, which the program hands its application."""
    (found,) = [it for it in conventions.sso.CLIENTS if it.name == name]
    return found


#: The portal's row as its cut leaves it, on `internet` and at home, and the
#: issuer the program derives from it for each client.
PORTAL = Exposure(routes=(Route('auth', internet=PROXIED, lan=REWRITE),))
ISSUER = conventions.sso.issuer(PORTAL)


@dataclass(frozen=True)
class Declared:
    """One component the run declared: what its declarations derive, its namespaces, and its client of the portal."""

    constraints: Constraints
    namespaces: frozenset[str]
    client: Client | None = None


def _components() -> dict[str, Declared]:
    """Every application component, each declared with what the test hands it."""
    found: dict[str, Declared] = {}

    def keep(name: str, component: Any, *namespaces: str, sso: Client | None = None) -> None:
        found[name] = Declared(cast('Constraints', component.constraints), frozenset(namespaces), sso)

    keep(
        'authelia',
        Authelia(
            'authelia',
            exposure=PORTAL,
            image=pin('authelia'),
            redis_image=pin('redis'),
            canary_image=pin('curl'),
            target_image=pin('nginx'),
            secrets=sealed(authelia_app.SECRETS.keys),
            config_secrets=sealed(authelia_app.CONFIG_SECRETS.keys),
            storage_key=sealed(conventions.sealed.AUTHELIA_STORAGE_KEY.keys),
            writer_key=writer_key(authelia_app.NAMESPACE),
            gated=('some-application',),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        authelia_app.NAMESPACE,
        authelia_app.CANARY_NAMESPACE,
    )
    keep(
        'echo',
        Echo(
            'echo',
            exposure=Exposure(routes=(Route('ec-echo', internet=Direct('the client address')),)),
            image=pin('whoami'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        'echo',
    )
    keep(
        'emailproxy',
        EmailProxy(
            'emailproxy',
            exposure=Exposure(ports=(Port('ep-mail', 1994, Transports.TCP, lan=Vip('ep-mail')),)),
            image=pin('emailproxy'),
            config=sealed(emailproxy_app.PORTED.keys),
            writer_key=writer_key(emailproxy_app.NAMESPACE),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        emailproxy_app.NAMESPACE,
    )
    keep(
        'exim',
        Exim('exim', image=pin('exim'), smtp=sealed(exim_app.SMTP.keys), dkim={'tls.key': 'sealed-dkim-key'}),
        exim_app.NAMESPACE,
    )
    keep(
        'golinks',
        Golinks(
            'golinks',
            exposure=Exposure(routes=(Route('gl-links', lan=REWRITE),)),
            image=pin('golinks'),
            writer_key=writer_key('golinks'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        'golinks',
    )
    photos = client('immich')
    keep(
        'immich',
        Immich(
            'immich',
            exposure=Exposure(routes=(Held(Route(photos.host, internet=Direct('large uploads'), lan=REWRITE)),)),
            client=photos,
            issuer=ISSUER,
            server_image=pin('server'),
            ml_image=pin('ml'),
            redis_image=pin('redis'),
            exporter_image=pin('exporter'),
            database_image=pin('vchord'),
            drill_image=image_busybox.pinned('database-restore-drill'),
            redis=sealed(immich_app.REDIS.keys),
            config=sealed(immich_app.CONFIG.keys),
            recovery=sealed(immich_app.RECOVERY.keys),
            writer_key=writer_key(immich_app.NAMESPACE),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        immich_app.NAMESPACE,
        sso=photos,
    )
    tube = client('jellyfin')
    keep(
        'jellyfin',
        Jellyfin(
            'jellyfin',
            exposure=Exposure(
                routes=(Held(Route(tube.host, internet=PROXIED, media=REWRITE)),),
                ports=(Port('jf-find', 7359, Transports.UDP, media=Heard()),),
            ),
            client=tube,
            issuer=ISSUER,
            image=pin('jellyfin'),
            writer_key=writer_key('jellyfin'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        'jellyfin',
        sso=tube,
    )
    homeserver = client('matrix')
    keep(
        'matrix',
        Matrix(
            'matrix',
            exposure=Exposure(routes=(Held(Route(homeserver.host, internet=PROXIED)),)),
            client=homeserver,
            issuer=ISSUER,
            image=pin('continuwuity'),
            oidc=sealed(matrix_app.OIDC.keys),
            writer_key=writer_key(matrix_app.NAMESPACE),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        matrix_app.NAMESPACE,
        sso=homeserver,
    )
    keep(
        'shoko',
        Shoko(
            'shoko',
            exposure=Exposure(routes=(Route('sk-library', lan=REWRITE),)),
            image=pin('shoko'),
            writer_key=writer_key('shoko'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        'shoko',
    )
    split = client('splitpro')
    keep(
        'splitpro',
        SplitPro(
            'splitpro',
            exposure=Exposure(routes=(Held(Route(split.host, internet=PROXIED)),)),
            client=split,
            issuer=ISSUER,
            image=pin('splitpro'),
            database_image=pin('pgcron'),
            drill_image=image_busybox.pinned('database-restore-drill'),
            auth=sealed(splitpro_app.AUTH.keys),
            recovery_key=sealed(splitpro_app.RECOVERY_KEY.keys),
            database_writer_key=writer_key('barman'),
            receipts_writer_key=writer_key('volsync'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        splitpro_app.NAMESPACE,
        sso=split,
    )
    keep(
        'spoolman',
        Spoolman(
            'spoolman',
            exposure=Exposure(routes=(Route('sp-spools', internet=Proxied(Gate.SESSION), media=REWRITE),)),
            image=pin('spoolman'),
            writer_key=writer_key('spoolman'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        'spoolman',
    )
    keep(
        'blog',
        StaticSite(
            'blog',
            exposure=Exposure(
                routes=(Apex(('blog.example',)), Route('www', internet=PROXIED, zones=('blog.example',)))
            ),
            source=GitSource('file:///tmp/upstream', 'built', pin('git-sync')),
            image=pin('static'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
            directories={},
            documents={'/.well-known/example/server': json.dumps({'server': 'home.blog.example:443'})},
        ),
        'blog',
    )
    keep(
        'doors',
        StaticSite(
            'doors',
            exposure=Exposure(routes=(Apex(('doors.example',)),)),
            source=NasSource('Web/doors'),
            image=pin('static'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
            directories={},
            documents={},
        ),
        'doors',
    )
    find = Port('st-find', 8444, Transports.TCP, internet=Balancer((Host('st-find'),)), lan=Vip('st-home'))
    keep(
        'stdiscosrv',
        Stdiscosrv(
            'stdiscosrv',
            exposure=Exposure(
                routes=(PortRedirect('st-home', find.port, internet=Direct('a raw port'), lan=REWRITE),),
                ports=(find,),
            ),
            image=pin('discosrv'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        'stdiscosrv',
    )
    keep(
        'syncthing',
        Syncthing(
            'syncthing',
            exposure=Exposure(
                routes=(Route('st-ui', internet=Proxied(Gate.SESSION)),),
                ports=(Port('st-peer', 22001, Transports.TCP, internet=Balancer((Host('st-dial'),))),),
            ),
            discovery=Port('st-disco', 8443, Transports.TCP, internet=Balancer((Host('st-disco'),))),
            retired_devices=(),
            image=pin('syncthing'),
            config_image=image_busybox.pinned('syncthing-config'),
            writer_key=writer_key('syncthing'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        'syncthing',
    )
    keep(
        'webdav',
        WebDav(
            'webdav',
            exposure=Exposure(routes=(Route('st-files', internet=Proxied(Gate.BASIC)),)),
            image=pin('dufs'),
            writer_key=writer_key('webdav'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        'webdav',
    )
    return found


@dataclass(frozen=True)
class Program:
    """What the run declared: the components, and the recorder of their resources."""

    components: Mapping[str, Declared]
    monitor: Recorder


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def program() -> Program:
    """Every application component, declared in one run."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        components = _components()
    return Program(components, monitor)


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The `k8s-base` program as its stack file holds it, for the monitoring stores."""
    return await applied()


@dataclass(frozen=True)
class Pod:
    """One pod the run declared, as the scheduler and containerd read it."""

    name: str
    namespace: str
    spec: Mapping[str, Any]
    annotations: Mapping[str, str]
    claims: tuple[str, ...]
    """The names of the claims it mounts; an instance's own, on `local-path`, as `<name>-instance`."""
    kind: Literal['pod', 'instance', 'recovered'] = 'pod'
    """A workload's or a CronJob's pod, a database's instance, or a restore drill's recovered instance."""
    labels: Mapping[str, str] = field(default_factory=dict[str, str])
    """A workload's or a CronJob's pod's labels, which a Service selects it by."""


def _template_pod(name: str, namespace: str, template: Mapping[str, Any]) -> Pod:
    spec = cast('Mapping[str, Any]', template['spec'])
    volumes = cast('list[dict[str, Any]]', spec.get('volumes', []))
    metadata = cast('Mapping[str, Any]', template.get('metadata', {}))
    return Pod(
        name,
        namespace,
        spec,
        cast('Mapping[str, str]', metadata.get('annotations') or {}),
        tuple(it['persistentVolumeClaim']['claimName'] for it in volumes if 'persistentVolumeClaim' in it),
        labels=cast('Mapping[str, str]', metadata.get('labels') or {}),
    )


def _instance_pod(name: str, namespace: str, spec: Mapping[str, Any], kind: Literal['instance', 'recovered']) -> Pod:
    """What CloudNativePG puts on an instance's pod from its cluster's spec, its claim among it."""
    affinity = cast('Mapping[str, Any]', spec.get('affinity', {}))
    return Pod(
        name,
        namespace,
        {
            **({'affinity': {'nodeAffinity': affinity['nodeAffinity']}} if 'nodeAffinity' in affinity else {}),
            **({'tolerations': affinity['tolerations']} if 'tolerations' in affinity else {}),
            **({'priorityClassName': spec['priorityClassName']} if 'priorityClassName' in spec else {}),
        },
        cast('Mapping[str, str]', spec['inheritedMetadata'].get('annotations') or {}),
        (f'{name}-instance',) if spec['storage']['storageClass'] == conventions.SC_LOCAL_PATH else (),
        kind,
    )


def pods(monitor: Recorder) -> Iterator[Pod]:
    """Every pod the run declares: each workload's and CronJob's template, each instance, each drill's recovered instance."""
    for it in monitor.of_type(DEPLOYMENT):
        metadata = it.inputs['metadata']
        yield _template_pod(metadata['name'], metadata['namespace'], it.inputs['spec']['template'])
    for it in monitor.of_type(CRONJOB):
        metadata = it.inputs['metadata']
        template = it.inputs['spec']['jobTemplate']['spec']['template']
        yield _template_pod(metadata['name'], metadata['namespace'], template)
    for it in monitor.of_type(CLUSTER):
        metadata = it.inputs['metadata']
        yield _instance_pod(metadata['name'], metadata['namespace'], it.inputs['spec'], 'instance')
    for it in monitor.of_type(CONFIG_MAP):
        data = cast('Mapping[str, str]', it.inputs.get('data') or {})
        if DRILL_MANIFEST in data:
            manifest = cast('dict[str, Any]', json.loads(data[DRILL_MANIFEST]))
            yield _instance_pod(
                manifest['metadata']['name'], manifest['metadata']['namespace'], manifest['spec'], 'recovered'
            )


def local_path_claims(monitor: Recorder) -> set[tuple[str, str]]:
    """Every claim on `local-path` the run declares, by its namespace and name."""
    return {
        (it.inputs['metadata']['namespace'], it.inputs['metadata']['name'])
        for it in monitor.of_type('kubernetes:core/v1:PersistentVolumeClaim')
        if it.inputs['spec'].get('storageClassName') == conventions.SC_LOCAL_PATH
    }


def nas_volumes(monitor: Recorder) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Every NAS claim's volume, by its claim's namespace and name."""
    return {
        (it.inputs['spec']['claimRef']['namespace'], it.inputs['spec']['claimRef']['name']): it.inputs['spec']
        for it in monitor.of_type(VOLUME)
    }


def node_volume_claims(monitor: Recorder) -> set[tuple[str, str]]:
    """Every claim on a node volume's class the run declares, by its namespace and name."""
    return {
        (it.inputs['metadata']['namespace'], it.inputs['metadata']['name'])
        for it in monitor.of_type('kubernetes:core/v1:PersistentVolumeClaim')
        if it.inputs['spec'].get('storageClassName') not in (conventions.SC_LOCAL_PATH, conventions.SC_NAS)
    }


def backed_claims(monitor: Recorder) -> set[tuple[str, str]]:
    """Every claim a restic source backs, by its namespace and name: a claim its mover may bind first."""
    return {
        (it.inputs['metadata']['namespace'], it.inputs['spec']['sourcePVC'])
        for it in monitor.of_type(SOURCE)
        if 'restic' in it.inputs['spec']
    }


def kept_by_a_volume_or_a_device(pod: Pod, monitor: Recorder) -> bool:
    """Whether a claim bound on a node keeps the pod there, or a device a node offers; a drill's fresh claim keeps nothing."""
    on_a_node = local_path_claims(monitor) | node_volume_claims(monitor)
    if pod.kind == 'recovered':
        return False
    return (
        pod.kind == 'instance' or any((pod.namespace, it) in on_a_node for it in pod.claims) or requests_a_device(pod)
    )


def requests_a_device(pod: Pod) -> bool:
    """Whether a container of the pod requests the GPU, which the nodes the GPU plugin serves offer."""
    containers = cast('list[dict[str, Any]]', pod.spec.get('containers', []))
    return any(GPU_RESOURCE in container.get('resources', {}).get('requests', {}) for container in containers)


def admitting(spec: Mapping[str, Any], *, gpu: bool) -> set[tuple[str, bool]]:
    """The nodes of `WITH_AND_WITHOUT_GPU` a pod of `spec` may run on: its node constraints, and the GPU where it requests it.

    The scheduler reads a request for the GPU off the pod and places it only
    on a node whose device plugin offers it. The plugin serves the nodes its
    chart labels, and a labeled node offers `GPU_RESOURCE` while its GPU is
    bound to `i915`, which is the one driver the worker's image carries, so
    the label is read as the offer.
    """
    return {node for node in schedulable_among(spec, WITH_AND_WITHOUT_GPU) if node[1] or not gpu}


def restic_sources(monitor: Recorder) -> Iterator[tuple[Mapping[str, Any], Pod]]:
    """Every restic source the run declares, beside the workload's pod that mounts its claim."""
    by_claim = {(pod.namespace, it): pod for pod in pods(monitor) for it in pod.claims}
    for it in monitor.of_type(SOURCE):
        if 'restic' in it.inputs['spec']:
            yield it.inputs, by_claim[it.inputs['metadata']['namespace'], it.inputs['spec']['sourcePVC']]


def test_the_run_declares_every_kind_of_pod_these_relations_read(program: Program) -> None:
    """The relations below are vacuous over a kind of pod the run does not reach: each kind is here at least once."""
    every = list(pods(program.monitor))
    claims = local_path_claims(program.monitor)
    volumes = nas_volumes(program.monitor)

    assert any(pod.kind == 'instance' for pod in every)
    assert any(pod.kind == 'recovered' for pod in every)
    assert any(any((pod.namespace, it) in claims for it in pod.claims) for pod in every)
    assert any(any((pod.namespace, it) in volumes for it in pod.claims) for pod in every)
    assert any(requests_a_device(pod) for pod in every)


def test_every_pod_of_a_component_that_keeps_working_while_the_uplink_is_down_admits_no_cloud_node(
    program: Program,
) -> None:
    """With the uplink down the worker reaches no cloud node, so the declaration holds every pod at home."""
    declaring_ = {
        name: it
        for name, it in program.components.items()
        if it.constraints.site is not None and UPLINK_DOWN in it.constraints.site.declarations
    }
    assert declaring_

    for name, component in declaring_.items():
        held = [pod for pod in pods(program.monitor) if pod.namespace in component.namespaces]
        assert held, name
        for pod in held:
            assert schedulable(pod.spec) <= set(NODES) - CLOUD, (name, pod.name)
            assert {it['key'] for it in pod.spec.get('tolerations', [])} >= {'node.kubernetes.io/unreachable'}


def test_a_declaring_client_of_the_portal_requires_the_portal_to_declare(program: Program) -> None:
    """A client signs in through the portal while it answers, so the portal keeps working too.

    It holds nothing on `main`, and cannot fail there: no client of the portal
    declares the requirement, so the set it reads is empty, which is the
    state the rulings leave. It holds the first client that declares.
    """

    def declares(component: Declared) -> bool:
        site = component.constraints.site
        return site is not None and UPLINK_DOWN in site.declarations

    clients = [it for it in program.components.values() if it.client is not None and declares(it)]
    if clients:
        assert declares(program.components['authelia'])


def test_a_pod_that_mounts_a_nas_claim_admits_exactly_the_nodes_its_volume_does(program: Program) -> None:
    volumes = nas_volumes(program.monitor)
    mounting = [pod for pod in pods(program.monitor) if any((pod.namespace, it) in volumes for it in pod.claims)]

    assert mounting
    for pod in mounting:
        for claim in pod.claims:
            if (pod.namespace, claim) in volumes:
                (term,) = volumes[pod.namespace, claim]['nodeAffinity']['required']['nodeSelectorTerms']
                assert schedulable(pod.spec) == selected(term, NODES), (pod.namespace, pod.name)


def test_every_nas_claim_is_mounted_by_a_pod_of_its_namespace(program: Program) -> None:
    """A NAS claim's name carries a digest of what its volume and it cannot change, so a pod mounts it by the name it was given.

    A pod that named it any other way, by its base name say, would mount a
    claim that is not there, and the claim would serve nothing.
    """
    mounted = {(pod.namespace, it) for pod in pods(program.monitor) for it in pod.claims}
    claims = set(nas_volumes(program.monitor))

    assert claims
    assert claims <= mounted, sorted(claims - mounted)


def test_a_restic_mover_admits_exactly_the_nodes_its_workload_does(program: Program) -> None:
    """A claim binds where the first of its mover and its workload is placed, so the two admit one set of nodes.

    The mover requests no GPU, so the nodes its workload admits are read with
    the GPU the workload requests, on nodes with and without it.
    """
    sources = list(restic_sources(program.monitor))

    assert sources
    for source, workload in sources:
        mover = {'affinity': source['spec']['restic'].get('moverAffinity')}
        assert admitting(mover, gpu=False) == admitting(workload.spec, gpu=requests_a_device(workload)), source[
            'metadata'
        ]


def test_the_mover_of_a_claim_whose_workload_requests_the_gpu_admits_only_nodes_that_offer_it(
    program: Program,
) -> None:
    """A mover placed first binds the claim where it lands, and the workload, which needs the claim and the GPU, runs only where both are.

    Read on every node twice, with the GPU and without it: the mover admits
    some node, and each node it admits carries the label of a node that
    offers the GPU.
    """
    beside_the_gpu = [
        (source, workload) for source, workload in restic_sources(program.monitor) if requests_a_device(workload)
    ]

    assert beside_the_gpu
    for source, _ in beside_the_gpu:
        mover = {'affinity': source['spec']['restic'].get('moverAffinity')}
        admitted = admitting(mover, gpu=False)
        assert admitted, source['metadata']
        assert all(gpu for _, gpu in admitted), (source['metadata'], sorted(admitted))


def test_a_pod_that_mounts_a_local_path_claim_writes_in_the_class_and_its_mover_in_none(program: Program) -> None:
    """Wherever the scheduler places it, the class caps what it writes to the disk etcd shares on a control plane."""
    claims = local_path_claims(program.monitor)
    writing = [
        pod
        for pod in pods(program.monitor)
        if any((pod.namespace, it) in claims for it in pod.claims) or f'{pod.name}-instance' in pod.claims
    ]

    assert writing
    for pod in writing:
        assert pod.annotations.get(conventions.BLOCKIO_POD_ANNOTATION) == conventions.ETCD_DISK_WRITER.name, (
            pod.namespace,
            pod.name,
        )
    for source in program.monitor.of_type(SOURCE):
        assert conventions.ETCD_DISK_WRITER.name not in json.dumps(source.inputs['spec']), source.name


def test_a_pod_kept_to_one_node_by_a_volume_or_a_device_runs_at_the_held_priority(program: Program) -> None:
    """Free pods that reach its node first would leave it pending, since it can run nowhere else."""
    kept = [pod for pod in pods(program.monitor) if kept_by_a_volume_or_a_device(pod, program.monitor)]

    assert kept
    for pod in kept:
        assert pod.spec.get('priorityClassName') == conventions.HELD_PRIORITY_CLASS, (pod.namespace, pod.name)


def test_a_restore_drills_recovered_instance_is_kept_nowhere(program: Program) -> None:
    """Its claim is made fresh each run, so it binds wherever the instance lands; held, a drill would preempt a serving pod."""
    for name, component in program.components.items():
        for pod in pods(program.monitor):
            if pod.kind == 'recovered' and pod.namespace in component.namespaces:
                assert pod.spec == component.constraints.pod_fields_json(), (name, pod.name)


def test_no_pod_mounts_a_backed_claim_beside_a_claim_another_pod_could_bind_it_away_from(program: Program) -> None:
    """A restic mover binds its claim apart from the pod's other claims, so the pod could find them on two nodes.

    Read from the render, however a component composed its constraints: no
    pod mounts two backed claims, or a backed claim beside a node volume's.
    """
    backed = backed_claims(program.monitor)
    on_a_node = node_volume_claims(program.monitor)

    assert backed
    for pod in pods(program.monitor):
        mounted = {(pod.namespace, it) for it in pod.claims}
        assert len(mounted & backed) <= 1, (pod.namespace, pod.name)
        assert not (mounted & backed and mounted & on_a_node), (pod.namespace, pod.name)


def serving(monitor: Recorder, client: Client) -> list[Pod]:
    """Every pod the run declares that serves `client`'s name: the pods a Service selects that a route on the name forwards to."""
    name = f'{client.host}.{client.zone}'
    backends = {
        (it.inputs['metadata']['namespace'], ref['name'])
        for it in monitor.of_type(HTTP_ROUTE)
        if name in it.inputs['spec'].get('hostnames', [])
        for rule in it.inputs['spec']['rules']
        for ref in rule.get('backendRefs', [])
    }
    selectors = [
        (it.inputs['metadata']['namespace'], cast('Mapping[str, str]', it.inputs['spec']['selector']))
        for it in monitor.of_type(SERVICE)
        if (it.inputs['metadata']['namespace'], it.inputs['metadata']['name']) in backends
    ]
    return [
        pod
        for pod in pods(monitor)
        if any(
            pod.namespace == namespace and selector.items() <= pod.labels.items() for namespace, selector in selectors
        )
    ]


def test_every_pod_that_serves_a_client_of_the_portal_reaches_the_issuers_name_at_the_issuers_addresses(
    program: Program,
) -> None:
    """The server a sign-in returns to calls the issuer by its public name, which no resolver a pod asks answers inside the cluster.

    Read over every client of the census whose name a route of the run
    serves, so a component that serves a client and forgets the alias fails
    here whichever component it is.
    """
    served = {client.name: found for client in conventions.sso.CLIENTS if (found := serving(program.monitor, client))}
    aliased = {(str(address), (ISSUER.host,)) for address in ISSUER.addresses}

    assert ISSUER.addresses
    assert {it.client.name for it in program.components.values() if it.client is not None} <= served.keys()
    for name, found in served.items():
        for pod in found:
            aliases = {(it['ip'], tuple(it['hostnames'])) for it in pod.spec.get('hostAliases', [])}
            assert aliases == aliased, (name, pod.namespace, pod.name)


def test_the_run_declares_every_application_component_the_installation_defines(program: Program) -> None:
    """A component the run leaves out renders nowhere here, and every relation above would pass without it."""
    defined = {token for cls in components() if (token := stated(cls)) is not None and token.startswith(APPS)}
    declared = {request.type for request in program.monitor.requested if request.type.startswith(APPS)}

    assert defined - declared == set()


@pytest.mark.parametrize('store', ['recent', 'history'])
def test_each_monitoring_store_writes_in_the_class_and_runs_at_the_held_priority(applied: Run, store: str) -> None:
    """Its `local-path` claim keeps it to its node, wherever the scheduler first placed it."""
    spec = _store(applied, store)

    assert spec['podMetadata']['annotations'][conventions.BLOCKIO_POD_ANNOTATION] == conventions.ETCD_DISK_WRITER.name
    assert spec['priorityClassName'] == conventions.HELD_PRIORITY_CLASS
    assert 'affinity' not in spec


def _store(run: Run, store: str) -> dict[str, Any]:
    """A store's spec: the chart's vmsingle, the recent store, or the history store the component declares."""
    if store == 'recent':
        return run.values(MONITORING_CHART)['vmsingle']['spec']
    (history,) = [it.inputs for it in run.monitor.of_type(VM_SINGLE)]
    return history['spec']
