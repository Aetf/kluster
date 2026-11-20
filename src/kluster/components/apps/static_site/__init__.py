"""Static sites: a stock static server answering several names, its content from git or from the NAS (rfc-008 §4.5).

One `StaticSite` is one application: its namespace, a Deployment of nginx
(`nginx-unprivileged`, pinned as `versions:image-static-site`), its Service,
and what its exposure derives. Each name its route rows serve is a server of
the one nginx, reading its files from a directory under the content: the
one the call names for it (`directories`), or the content's root. So one
instance serves several sites, and a name no row serves is answered 404.

**Where the content comes from is the instance's source**, which decides its
placement:

-   **A git source** (`GitSource`) is a built branch, which git-sync pulls
    into an empty directory the server reads, as the git-sync sidecar
    (`versions:image-static-site-git-sync`). The same sync runs once as an
    init container first, so a pod serves nothing before the branch is
    there. Afterwards the sidecar retries a failed fetch for as long as it
    fails, since a sidecar that exits makes the pod unready, and the content
    already synced is still the branch's last state. The instance holds no
    state, so it runs `GIT_REPLICAS` pods, spread across the cloud nodes, one
    per node: a node's drain or loss leaves the site served.
-   **A NAS source** (`NasSource`) is a directory under the host's export,
    mounted read-only through `nas_claim`, so the files are edited on the NAS
    share and the cluster deploys nothing for it. The export is reachable from
    the worker alone, so the instance runs one pod there.

**An apex serves documents of its own** (`documents`): JSON a browser reads
across origins. They are not part of the content: the program hands them in,
and they reach the server as a ConfigMap, served as `application/json` at their paths on the apex's names
alone, which carry no extension a type is read from. The CORS header they need
is added by a route of their own at the apex's listeners, matching those
paths exactly (`documents_route`), so no other path of the site carries it.

The server runs as the image's numeric user, on a read-only root, with an
empty directory for its temporary files. It speaks HTTP to the Gateways alone,
on `PORT`, and answers the kubelet's probe at `HEALTH` under a name no site
uses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.apps import Backend, Exposed, Fronts, expose
from kluster.components.apps.storage import NasClaim, nas_claim
from kluster.components.cilium.gateways import apex_listener
from kluster.conventions.routes import Apex, Exposure, Held, Passthrough, PortRedirect, Route
from kluster.lib import templates
from kluster.lib.k8s import (
    CLOUD_POD_AFFINITY,
    WORKER_POD_AFFINITY,
    ApplicationNamespace,
    application_namespace,
    identity_stripped,
)
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'CONTENT',
    'CORS_HEADER',
    'DOCUMENTS',
    'GIT_REPLICAS',
    'HEALTH',
    'PORT',
    'SERVER_USER',
    'SYNC_USER',
    'GitSource',
    'NasSource',
    'StaticSite',
    'documents_route',
    'served_names',
    'server_configuration',
)

#: The port the server listens on, the image's own, which its Service forwards
#: on unchanged. Above 1024, so its user binds it with no capability.
PORT = 8080

#: The path the kubelet's readiness probe asks for, answered under a name no
#: site serves.
HEALTH = '/healthz'

#: The user and group the server runs as: the image's own, `nginx`, which its
#: image config names by number.
SERVER_USER = 101

#: The user and group git-sync runs as: the image's own, which its image config
#: names by number. The server reads what it writes as any other user, since
#: git checks files out readable by all.
SYNC_USER = 65533

#: Where the server reads the content from: the empty directory git-sync
#: writes, mounted at the same path in both, or the NAS claim.
CONTENT = '/srv/content'

#: Where the server reads the apex's documents from, each at its own path.
DOCUMENTS = '/srv/documents'

#: The pods of an instance with a git source: more than one, each on its own
#: cloud node.
GIT_REPLICAS = 2

#: git-sync's root: a directory it makes itself under `CONTENT`. git refuses a
#: repository in a directory another user owns, which the empty directory is,
#: the kubelet's.
_SYNC_ROOT = f'{CONTENT}/git'

#: The link git-sync keeps under its root, which it swaps to each new checkout
#: in one rename, so the server never reads a half-written tree.
_LINK = 'site'

#: How often git-sync asks the remote for the branch's head: a push is live
#: within this.
_SYNC_PERIOD = '60s'

#: How long git-sync keeps a checkout once the link has moved off it, so a
#: request that resolved the link just before the move finds its file.
_STALE_CHECKOUT = '1m'

#: Where git-sync's home is, which it writes its git configuration to: the
#: image's `HOME`, an empty directory on a read-only root.
_SYNC_HOME = '/tmp'

#: The file the server's configuration lands under, which the image's
#: `nginx.conf` includes from `conf.d`.
_SERVER_CONF = 'default.conf'
_SERVER_CONF_DIR = '/etc/nginx/conf.d'

#: Where the server writes its pid and temporary files: the image's
#: `nginx.conf` names `/tmp` for them.
_SERVER_TMP = '/tmp'

#: The header a browser reads a document across origins by, and its value: any
#: origin may read it.
CORS_HEADER = 'Access-Control-Allow-Origin'
_CORS_ANY = '*'


@dataclass(frozen=True)
class GitSource:
    """A built branch of a public repository, which the cluster pulls with no credential of its own."""

    repository: str
    """The URL git-sync clones."""
    branch: str
    """The branch whose head is the content."""
    image: ImagePin
    """git-sync's pin, `versions:image-static-site-git-sync`."""


@dataclass(frozen=True)
class NasSource:
    """A directory under the host's export, which the cluster reads and never writes."""

    path: str
    """The path under the export, relative (`nas_claim`)."""


type Source = GitSource | NasSource


def served_names(exposure: Exposure) -> tuple[str, ...]:
    """Every name the exposure's route rows serve, held or not, once each: each apex zone, then each row's host in each of its zones."""
    names: list[str] = []
    for held in exposure.routes:
        row = held.row if isinstance(held, Held) else held
        match row:
            case Apex():
                names += row.zones
            case Route():
                names += [f'{row.host}.{zone}' for zone in row.zones]
            case Passthrough() | PortRedirect():
                pass
    return tuple(dict.fromkeys(names))


def _apex(exposure: Exposure) -> Apex | None:
    apexes = [row for row in exposure.routes if isinstance(row, Apex)]
    return apexes[0] if apexes else None


def _refuse_exposure(name: str, exposure: Exposure) -> None:
    """Refuse an exposure a static site is not reached through: a raw port, a device, or TLS the site would hold."""
    if exposure.ports or exposure.device is not None:
        raise ValueError(f'{name}: a static site is reached by name through a Gateway, never at a raw port or a device')
    if any(isinstance(held.row if isinstance(held, Held) else held, Passthrough) for held in exposure.routes):
        raise ValueError(f'{name}: a static site holds no certificate, so it takes no passthrough row')


def _refuse_path(name: str, path: str, *, what: str, under: str, absolute: bool) -> None:
    parts = [part for part in path.split('/') if part]
    if path.startswith('/') != absolute or '..' in parts or '.' in parts:
        place = 'an absolute path' if absolute else f'a path relative to {under}'
        raise ValueError(f'{name}: {what} {path!r} is not {place} that stays under {under}')


def _refuse_directories(name: str, exposure: Exposure, directories: Mapping[str, str]) -> None:
    served = served_names(exposure)
    if unserved := sorted(set(directories) - set(served)):
        raise ValueError(f'{name}: no route row serves {", ".join(unserved)}, which a directory is named for')
    for directory in directories.values():
        _refuse_path(name, directory, what='the directory', under='the content', absolute=False)


def _refuse_documents(name: str, exposure: Exposure, documents: Mapping[str, str]) -> None:
    if documents and _apex(exposure) is None:
        raise ValueError(f'{name}: documents are served at an apex, and the exposure holds no apex row')
    for path in documents:
        _refuse_path(name, path, what='the document', under='the root', absolute=True)
        if path.endswith('/'):
            raise ValueError(f'{name}: the document {path!r} names a directory, not a file')


@dataclass(frozen=True)
class _Server:
    """One server of the configuration: the names it answers, where it reads them from, and the documents it serves."""

    names: tuple[str, ...]
    root: str
    documents: tuple[str, ...]


@dataclass(frozen=True)
class _Configuration:
    """What `static_site.conf.j2` reads."""

    port: int
    health: str
    documents_root: str
    servers: tuple[_Server, ...]


def _content_root(source: Source) -> str:
    """The directory the content's root is at, in the server's filesystem."""
    return f'{_SYNC_ROOT}/{_LINK}' if isinstance(source, GitSource) else CONTENT


def server_configuration(
    exposure: Exposure, source: Source, *, directories: Mapping[str, str], documents: Mapping[str, str]
) -> str:
    """nginx's server configuration: one server per directory, and the apex's names apart where they serve documents."""
    apex = _apex(exposure)
    at_apex = set(apex.zones) if apex is not None and documents else set[str]()
    grouped: dict[tuple[str, bool], list[str]] = {}
    for served in served_names(exposure):
        grouped.setdefault((directories.get(served, ''), served in at_apex), []).append(served)
    content = _content_root(source)
    return templates.render(
        'kluster.components.apps.static_site',
        'templates/static_site.conf.j2',
        _Configuration(
            port=PORT,
            health=HEALTH,
            documents_root=DOCUMENTS,
            servers=tuple(
                _Server(
                    names=tuple(names),
                    root='/'.join(part for part in (content, directory) if part),
                    documents=tuple(documents) if serves_documents else (),
                )
                for (directory, serves_documents), names in grouped.items()
            ),
        ),
    )


def _document_key(path: str) -> str:
    """The ConfigMap key a document is held under: its path, which a key cannot hold a slash of."""
    return path.strip('/').replace('/', '_')


def documents_route(
    resource_name: str,
    apex: Apex,
    paths: tuple[str, ...],
    *,
    namespace: pulumi.Input[str],
    backend: Backend,
    opts: pulumi.ResourceOptions | None = None,
) -> crds.gateway.v1.HTTPRoute:
    """The route of an apex's documents: at `internet-gw`'s apex listeners, matching their paths exactly, adding the CORS header.

    It sits beside the apex's own route (`kluster.components.apps.expose`),
    which matches every path: an exact match outranks a prefix match whichever
    route holds it, so the documents' requests take this route and every
    other request the apex's, which adds no header.
    """
    return crds.gateway.v1.HTTPRoute(
        f'{resource_name}-documents',
        metadata=k8s.meta.v1.ObjectMetaArgs(name='apex-documents', namespace=namespace),
        spec=crds.gateway.v1.HTTPRouteSpecArgs(
            parent_refs=[
                crds.gateway.v1.HTTPRouteSpecParentRefsArgs(
                    name=conventions.GATEWAY_INTERNET,
                    namespace=conventions.GATEWAY_NAMESPACE,
                    section_name=apex_listener(zone),
                )
                for zone in apex.zones
            ],
            hostnames=list(apex.zones),
            rules=[
                crds.gateway.v1.HTTPRouteSpecRulesArgs(
                    matches=[
                        crds.gateway.v1.HTTPRouteSpecRulesMatchesArgs(
                            path=crds.gateway.v1.HTTPRouteSpecRulesMatchesPathArgs(type='Exact', value=path)
                        )
                        for path in paths
                    ],
                    filters=[
                        identity_stripped(),
                        crds.gateway.v1.HTTPRouteSpecRulesFiltersArgs(
                            type='ResponseHeaderModifier',
                            response_header_modifier=crds.gateway.v1.HTTPRouteSpecRulesFiltersResponseHeaderModifierArgs(
                                add=[
                                    crds.gateway.v1.HTTPRouteSpecRulesFiltersResponseHeaderModifierAddArgs(
                                        name=CORS_HEADER, value=_CORS_ANY
                                    )
                                ]
                            ),
                        ),
                    ],
                    backend_refs=[
                        crds.gateway.v1.HTTPRouteSpecRulesBackendRefsArgs(name=backend.service, port=backend.port)
                    ],
                )
            ],
        ),
        opts=opts,
    )


#: The node label the git source's pods are spread over, one per node.
_NODE_TOPOLOGY = 'kubernetes.io/hostname'


def _container_security(user: int) -> k8s.core.v1.SecurityContextArgs:
    return k8s.core.v1.SecurityContextArgs(
        run_as_user=user,
        run_as_group=user,
        allow_privilege_escalation=False,
        read_only_root_filesystem=True,
        capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
    )


def _sync_args(source: GitSource) -> list[str]:
    """git-sync's flags: the branch, under `CONTENT` behind its link, retried for as long as the remote fails."""
    return [
        f'--repo={source.repository}',
        f'--ref={source.branch}',
        f'--root={_SYNC_ROOT}',
        f'--link={_LINK}',
        f'--period={_SYNC_PERIOD}',
        f'--stale-worktree-timeout={_STALE_CHECKOUT}',
        '--depth=1',
        '--max-failures=-1',
    ]


def _sync(source: GitSource, *, name: str, once: bool) -> k8s.core.v1.ContainerArgs:
    """git-sync, writing the content's directory: once, before the server starts, or as the sidecar."""
    return k8s.core.v1.ContainerArgs(
        name=name,
        image=str(source.image),
        args=[*_sync_args(source), *(['--one-time'] if once else [])],
        resources=k8s.core.v1.ResourceRequirementsArgs(
            requests={'cpu': '5m', 'memory': '32Mi'},
            limits={'cpu': '50m', 'memory': '128Mi'},
        ),
        security_context=_container_security(SYNC_USER),
        volume_mounts=[
            k8s.core.v1.VolumeMountArgs(name='content', mount_path=CONTENT),
            k8s.core.v1.VolumeMountArgs(name='sync-home', mount_path=_SYNC_HOME),
        ],
    )


def _pod(
    name: str,
    source: Source,
    *,
    image: ImagePin,
    content: k8s.core.v1.VolumeArgs,
    documents: Mapping[str, str],
) -> k8s.core.v1.PodSpecArgs:
    """The pod: the server reading `content`, and for a git source the sync before it and beside it."""
    probe = k8s.core.v1.HTTPGetActionArgs(path=HEALTH, port='http')
    mounts = [
        k8s.core.v1.VolumeMountArgs(name='config', mount_path=_SERVER_CONF_DIR, read_only=True),
        k8s.core.v1.VolumeMountArgs(name='tmp', mount_path=_SERVER_TMP),
        k8s.core.v1.VolumeMountArgs(name='content', mount_path=CONTENT, read_only=True),
    ]
    volumes = [
        k8s.core.v1.VolumeArgs(name='config', config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=name)),
        k8s.core.v1.VolumeArgs(name='tmp', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs()),
        content,
    ]
    if documents:
        mounts.append(k8s.core.v1.VolumeMountArgs(name='documents', mount_path=DOCUMENTS, read_only=True))
        volumes.append(
            k8s.core.v1.VolumeArgs(
                name='documents',
                config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(
                    name=f'{name}-documents',
                    items=[
                        k8s.core.v1.KeyToPathArgs(key=_document_key(path), path=path.lstrip('/')) for path in documents
                    ],
                ),
            )
        )
    server = k8s.core.v1.ContainerArgs(
        name='server',
        image=str(image),
        ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=PORT, protocol='TCP')],
        readiness_probe=k8s.core.v1.ProbeArgs(http_get=probe, period_seconds=10),
        resources=k8s.core.v1.ResourceRequirementsArgs(
            # The legacy server's working set, serving these sites and more,
            # stays near 10 MiB, and its CPU at a few millicores (nodes.md §1.1).
            requests={'cpu': '5m', 'memory': '16Mi'},
            limits={'cpu': '50m', 'memory': '64Mi'},
        ),
        security_context=_container_security(SERVER_USER),
        volume_mounts=mounts,
    )
    match source:
        case GitSource():
            volumes.append(k8s.core.v1.VolumeArgs(name='sync-home', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs()))
            return k8s.core.v1.PodSpecArgs(
                automount_service_account_token=False,
                enable_service_links=False,
                affinity=CLOUD_POD_AFFINITY,
                topology_spread_constraints=[
                    k8s.core.v1.TopologySpreadConstraintArgs(
                        max_skew=1,
                        topology_key=_NODE_TOPOLOGY,
                        when_unsatisfiable='DoNotSchedule',
                        label_selector=k8s.meta.v1.LabelSelectorArgs(match_labels=_labels(name)),
                        # Spread within one rollout's pods, so the old ones
                        # leaving does not skew where the new ones go.
                        match_label_keys=['pod-template-hash'],
                    )
                ],
                security_context=_POD_SECURITY,
                init_containers=[_sync(source, name='first-sync', once=True)],
                containers=[server, _sync(source, name='sync', once=False)],
                volumes=volumes,
            )
        case NasSource():
            return k8s.core.v1.PodSpecArgs(
                automount_service_account_token=False,
                enable_service_links=False,
                affinity=WORKER_POD_AFFINITY,
                security_context=_POD_SECURITY,
                containers=[server],
                volumes=volumes,
            )


#: What every pod of a static site is held to; each container names its user.
_POD_SECURITY = k8s.core.v1.PodSecurityContextArgs(
    run_as_non_root=True,
    seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
)


def _labels(name: str) -> dict[str, str]:
    return {'app.kubernetes.io/name': name}


class StaticSite(Component, pulumi_type='kluster:apps:StaticSite'):
    """A static site's namespace, its configuration and content, its Deployment and Service, and what its exposure derives.

    `name` is the application's, and its namespace's, Deployment's and
    Service's. `exposure` is its row of the route census, route rows alone;
    `source` is where the content comes from, and `image` the server's pin.
    `directories` names, for a name the exposure serves, the directory under
    the content it reads from, the content's root where it names none.
    `documents` are the apex's own, by path, each a JSON body. Both are
    required: a site whose names all read the content's root, or whose apex
    serves no document, is handed an empty mapping. `fronts` and
    `zone_ids` are what the program read for `expose`. The providers arrive in
    `opts`: the Kubernetes provider for the cluster's objects and the
    Cloudflare one for its records.
    """

    namespace: ApplicationNamespace
    config: k8s.core.v1.ConfigMap
    documents: k8s.core.v1.ConfigMap | None
    nas: NasClaim | None
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    exposed: Exposed
    documents_route: crds.gateway.v1.HTTPRoute | None

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        source: Source,
        image: ImagePin,
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        directories: Mapping[str, str],
        documents: Mapping[str, str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # Refused before anything is registered.
        _refuse_exposure(name, exposure)
        _refuse_directories(name, exposure, directories)
        _refuse_documents(name, exposure, documents)
        if isinstance(source, NasSource):
            # `nas_claim` refuses it too, once the namespace has registered.
            _refuse_path(name, source.path, what='the NAS path', under='the export', absolute=False)
        configuration = server_configuration(exposure, source, directories=directories, documents=documents)
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=name, opts=self.child_opts())
        # Read off the namespace, so each object in it waits for it.
        namespace = self.namespace.namespace.metadata.name

        self.config = k8s.core.v1.ConfigMap(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=name, namespace=namespace),
            data={_SERVER_CONF: configuration},
            opts=self.child_opts(),
        )
        self.documents = (
            k8s.core.v1.ConfigMap(
                f'{name}-documents',
                metadata=k8s.meta.v1.ObjectMetaArgs(name=f'{name}-documents', namespace=namespace),
                data={_document_key(path): body for path, body in documents.items()},
                opts=self.child_opts(),
            )
            if documents
            else None
        )
        self.nas = (
            nas_claim(
                f'{name}-content',
                'content',
                source.path,
                namespace=name,
                read_only=True,
                opts=self.child_opts(depends_on=[self.namespace.namespace]),
            )
            if isinstance(source, NasSource)
            else None
        )
        content = (
            k8s.core.v1.VolumeArgs(
                name='content',
                persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                    claim_name=self.nas.claim.metadata.name, read_only=True
                ),
            )
            if self.nas is not None
            else k8s.core.v1.VolumeArgs(name='content', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs())
        )
        labels = _labels(name)
        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=name,
                namespace=namespace,
                # Reloader rolls the pods when the configuration changes, which
                # the server reads only at its start.
                annotations={'reloader.stakater.com/auto': 'true'},
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=GIT_REPLICAS if isinstance(source, GitSource) else 1,
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=labels),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=labels),
                    spec=_pod(name, source, image=image, content=content, documents=documents),
                ),
            ),
            opts=self.child_opts(),
        )
        self.service = k8s.core.v1.Service(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=name, namespace=namespace),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector=labels,
                ports=[k8s.core.v1.ServicePortArgs(name='http', port=PORT, target_port='http', protocol='TCP')],
            ),
            opts=self.child_opts(),
        )
        backend = Backend(service=self.service.metadata.name, port=PORT)
        self.exposed = expose(
            name,
            exposure,
            namespace=namespace,
            backend=backend,
            selector=None,
            fronts=fronts,
            zone_ids=zone_ids,
            opts=self.child_opts(),
        )
        apex = _apex(exposure)
        self.documents_route = (
            documents_route(name, apex, tuple(documents), namespace=namespace, backend=backend, opts=self.child_opts())
            if apex is not None and documents
            else None
        )

        self.register_outputs({})
