"""Jellyfin, the media server, at the home site (rfc-008 §5.6).

One server, in a namespace of its own, reached two ways: its interface by
name through the Gateways, on `internet` behind the proxy and on `media` for a
television on either home VLAN, and its discovery on the raw port a client
broadcasts to, heard on the home segments by a relay on the host that sends
each broadcast to the media VIP. Both are rows of the exposure the component
receives, and `expose` declares them. It signs a browser in through the portal
with the client its census row is (`conventions.sso`), whose redirect
addresses are under the route's name, so a row and a client that name two
hosts are refused. The server finishes a sign-in itself, calling the portal's
issuer by its public name from its single sign-on plugin, whose settings are
in its data directory, so its pod carries the issuer's alias
(`kluster.components.apps.issuer_aliases`).

**It runs at the home site**: the library and the server's previews are on the
NAS, which the home site alone reaches (`NasClaim.constraints`). The GPU it
transcodes on keeps it to the node offering it
(`kluster.components.apps.GPU_HOLD`). Its data claim is handed both, so its
mover binds it on a home node that offers the GPU. Its rollout is
`Recreate`: the server's database is SQLite, one process's, and the GPU is
one device, which a second pod would wait for.

**What it keeps, and where** (rfc-008 §4.2):

-   its database, settings, plugins and logs, in its data directory, a claim
    backed up under `STANDARD`;
-   its metadata and its scrub previews, which run to gigabytes and are
    derived again from the library and the metadata providers, under
    `NAS_DERIVED` on the NAS, each mounted at the path the server keeps it
    at under its data directory: the metadata at the default the server
    takes while its settings name no other, the previews at the one it
    always takes;
-   the library, `LIBRARY` on the export, read-only;
-   its cache, transcodes among them, in an `emptyDir`, since a restart
    loses nothing it needs. Its bound is a share of the worker's disk
    (`conventions.homelab.HOMELAB_DISK_GIB`), so a transcode that outgrows
    it evicts the pod rather than fill the disk.

The data, cache and configuration directories are stated in the container's
environment, which outranks the image's, so the mounts and the server agree
by construction.

**Init containers prepare the claim before the server starts.** One creates
`DATA_PATH` as the pod's user. A mount inside the claim, the previews', needs
it as its parent, and the runtime creates a missing parent as root, where the
server, which writes its marker there before it opens its database, could not
write.

**The network configuration is the server's, and the component owns the
settings the pod's wiring decides.** The server's `network.xml` is in its
claim beside its database and comes over with it, holding what the
dashboard's networking page set there. Before the server starts, an init
container merges `templates/network.xml.j2`, rendered, into it
(`shell/network-merge.sh`): each element the rendered file holds replaces the
server's of that name, and every other element stays as the file holds it,
one the server no longer reads among them. So a setting the component does
not own lasts, and one it owns returns to the rendered value on the next
start. A server with no network configuration yet takes the rendered one as
its initial state. A file the merge cannot read in the shape the server writes is
refused, and the pod does not start: an emptied file, or one cut short, holds
less than the server kept, and a merge over it would write that back whole.
The merge is shell and awk, the tools the server's image carries, which has
no Python.

The component owns a setting when the pod's own wiring decides its value --
the Service's port, the probes, the Gateways in front of it, the exposure's
rows -- so a value carried over from the host the file came from would break
the pod. Every other setting, which clients the server serves and which
address it publishes, is the dashboard's. What it owns today, and why:

-   no base path, since the route, the probes and the single sign-on client's
    redirect addresses are at the root of the name;
-   plain HTTP on `HTTP_PORT`, which the Service forwards to, and no HTTPS:
    the Gateway terminates TLS for the name. With HTTPS off the server
    serves no certificate and redirects nothing to HTTPS, which it does only
    while it serves HTTPS, so the certificate's path and password, and the
    setting that asks for the redirect, stay as the file holds them;
-   every IPv4 address of the pod bound, with no bind address named and IPv4
    on: the Service and the probes reach the pod on IPv4, and a bind address
    the file names is one of the host the file came from, which the pod does
    not have;
-   the cluster's pod ranges as the known proxies. The Gateways' Envoy
    reaches the pod from an address in them, and the server reads a
    request's client from `X-Forwarded-For` only when the connection comes
    from a known proxy. Without them every client is the Gateway's address,
    which is private, and a private address is on the server's local
    network unless the file's local subnets leave it out, so a client from
    the internet could pass as a home one. The IPv6 range takes effect only
    where the server's IPv6 is turned on;
-   discovery answered and remote access allowed, which the exposure's
    discovery row and its internet side need;
-   no remote address filter, so the server answers every client outside its
    local network, which the internet side needs. Behind the Gateway the
    server judges a request by the address nearest the end of
    `X-Forwarded-For` that is no known proxy: Cloudflare's edge's, for a
    request through the proxied internet route, or a client's. On the path
    the file was written for, every client reached the server behind an
    address of the old cluster's own, which is what the filter it holds
    names, so a filter carried over would refuse every client from the
    internet. Whether the filter is an allowlist or a blocklist stays as the
    file holds it, since the server reads that only beside a filter.

**Discovery replies with the one name.** `JELLYFIN_PublishedServerUrl` is the
route's name as an `https` URL, which the server hands a client in reply to
its broadcast whatever address the broadcast came from, so a client
dials the name on 443 and reaches the server through the Gateway on its
segment. The server prefers it to any address its network configuration
publishes by subnet, so those stay as the file holds them. The server listens
for discovery on `DISCOVERY_PORT` on every IPv4 address of the pod; the port
is fixed in the server's code, so an exposure whose discovery row states
another is refused.

**The server transcodes on the A380**, requesting one
`kluster.components.gpu.GPU_RESOURCE`, which the device plugin offers from
the GPU bound to the worker. The render node reaches the container with the
ownership and mode it has on the node, where udev gives every render node
mode 0666 (systemd's `50-udev-default.rules`), so the pod's user opens it.

The image is `jellyfin/jellyfin`, pinned as `versions:image-jellyfin`, and
the init containers run the same image. Its image config names no user, so
the pod states one, as the namespace's `restricted` level requires.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.apps import GPU_HOLD, Backend, Exposed, Fronts, declared_constraints, expose, issuer_aliases
from kluster.components.apps.storage import Claim, NasClaim, backed, claim, nas_claim
from kluster.components.gpu import GPU_RESOURCE
from kluster.conventions.backup import STANDARD
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import Exposure, Held, Port, Route
from kluster.conventions.sso import Client, Issuer
from kluster.lib import templates
from kluster.lib.k8s import ApplicationNamespace, application_namespace
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'CACHE_DIR',
    'CONFIG_DIR',
    'DATA_DIR',
    'DATA_PATH',
    'DISCOVERY_PORT',
    'HTTP_PORT',
    'LABELS',
    'LIBRARY',
    'MEDIA',
    'METADATA',
    'NAME',
    'NAS_DERIVED',
    'NETWORK_CONFIG',
    'RENDERED_MOUNT',
    'TRICKPLAY',
    'USER',
    'Jellyfin',
)

#: The application's name: its namespace, its Deployment and its interface's
#: Service. The namespace is a backed one of `conventions.backup`, whose writer
#: key is sealed under this name.
NAME = 'jellyfin'

#: The labels its pods carry, which its interface's Service and its discovery
#: port's Services select.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The user and group the server and its init containers run as, which own
#: everything the server writes in its claim and which the claim's movers run
#: as. The image names none.
USER = 1000

#: The port the server serves HTTP on, which the rendered network
#: configuration states and the Service forwards to. It is the server's
#: default (`NetworkConfiguration.DefaultHttpPort`), which leaving the HTTPS
#: port to the file relies on: where the two collide the server resets both
#: to their defaults (`ApplicationHost.Init`), which lands back on this one.
HTTP_PORT = 8096

#: The port the server listens for discovery on, fixed in its code
#: (`AutoDiscoveryHost.PortNumber`).
DISCOVERY_PORT = 7359

#: The server's data directory, where its claim is mounted: its database,
#: plugins, logs and, under `CONFIG_DIR`, its settings.
DATA_DIR = '/config'

#: The directory the server reads its settings from, `network.xml` among them.
CONFIG_DIR = f'{DATA_DIR}/config'

#: The server's network configuration, which an init container merges the
#: rendered one into.
NETWORK_CONFIG = f'{CONFIG_DIR}/network.xml'

#: The server's cache directory, transcodes among them, an `emptyDir`.
CACHE_DIR = '/cache'

#: Where the server keeps its metadata while its settings name no other path:
#: `metadata` under its data directory (`ServerApplicationPaths`).
METADATA = f'{DATA_DIR}/metadata'

#: The directory the server keeps its database in, `data` under its data
#: directory (`BaseApplicationPaths.DataPath`), which it writes its marker into
#: before it opens anything.
DATA_PATH = f'{DATA_DIR}/data'

#: Where the server keeps its scrub previews, which it calls trickplay images:
#: `trickplay` under `DATA_PATH` (`BaseApplicationPaths.TrickplayPath`).
TRICKPLAY = f'{DATA_PATH}/trickplay'

#: The path under the export that holds the library, where its files already
#: are: moving it moves the library on the host.
LIBRARY = 'Media'

#: Where the library is mounted: the path the server's database records each
#: item at, so the library is found where it was.
MEDIA = '/media'

#: The path under the export that holds what the server derives from the
#: library and the metadata providers, its metadata and its scrub previews,
#: each in a directory of its own.
NAS_DERIVED = 'Jellyfin'

#: Where the rendered network configuration and the merge are mounted, read
#: by the init container alone.
RENDERED_MOUNT = '/rendered'

#: The claim of the server's data directory, in GiB: its database, settings,
#: plugins and logs, about half a GiB with its metadata and previews on the
#: NAS, and the backups the server writes there itself.
_DATA_SIZE = 4

#: The bound on the cache, in GiB: a third of the worker's disk, which leaves
#: the rest to the images, the claims and the node itself. A playback holds the
#: segments of its transcode until it stops unless the server's encoding
#: settings delete them as it goes, so the bound holds one full-length title
#: transcoded at a television's bitrate with room beside it.
_CACHE_GIB = conventions.HOMELAB_DISK_GIB // 3

#: What each init container asks for and is held to: one short `install`, or
#: one `awk` over a file of a few dozen lines.
_INIT_RESOURCES = k8s.core.v1.ResourceRequirementsArgs(
    requests={'cpu': '10m', 'memory': '16Mi'},
    limits={'cpu': '100m', 'memory': '64Mi'},
)

#: Where custom fonts for subtitles are read from, which the image's fontconfig
#: includes, and the directory of the claim they are kept in.
_FONTS = '/usr/local/share/fonts/custom'
_FONTS_DIR = 'fonts'

#: The rendered network configuration's template and the merge, beside this
#: module, and their keys in the ConfigMap.
_TEMPLATE = 'templates/network.xml.j2'
_RENDERED = 'network.xml'
_MERGE = 'network-merge.sh'
_MERGE_SOURCE = f'shell/{_MERGE}'


@dataclass(frozen=True)
class _NetworkParams:
    """What the network configuration is rendered with."""

    http_port: int
    known_proxies: tuple[str, ...]


def _route(exposure: Exposure) -> Route:
    """The exposure's one route row, held or not, served in one zone."""
    if len(exposure.routes) != 1:
        raise ValueError(f'jellyfin is served at one name, and its exposure holds {len(exposure.routes)} route rows')
    (held,) = exposure.routes
    row = held.row if isinstance(held, Held) else held
    if not isinstance(row, Route):
        raise ValueError('jellyfin is served at a route row, which the Gateways terminate TLS for')
    if len(row.zones) != 1:
        raise ValueError(
            f'the route {row.host!r} is published in {len(row.zones)} zones, and jellyfin publishes one address'
        )
    return row


def _discovery(exposure: Exposure) -> Port:
    """The exposure's one raw port, the discovery the server listens for, refused unless it is on the server's fixed port."""
    if len(exposure.ports) != 1:
        raise ValueError(
            f'jellyfin listens for discovery on one raw port, and its exposure holds {len(exposure.ports)}'
        )
    (row,) = exposure.ports
    if row.port != DISCOVERY_PORT or row.transports is not Transports.UDP:
        raise ValueError(
            f'the port {row.name!r} is {row.port} on {row.transports.protocols}, and jellyfin listens for discovery '
            f'on {DISCOVERY_PORT} on UDP alone'
        )
    return row


def _refuse_client(row: Route, client: Client) -> None:
    """Refuse a client whose redirect addresses are under another name than the route's, which the portal would send a browser back to."""
    (zone,) = row.zones
    if (client.host, client.zone) != (row.host, zone):
        raise ValueError(
            f'the client {client.name!r} redirects to {client.host}.{client.zone}, and jellyfin is served at '
            f'{row.host}.{zone}'
        )


def _published_url(row: Route) -> str:
    """The server's address as a client is told it: the route's name, over HTTPS."""
    (zone,) = row.zones
    return f'https://{row.host}.{zone}'


class Jellyfin(Component, pulumi_type='kluster:apps:Jellyfin'):
    """The media server: its namespace, its claims, its network configuration, its Deployment, and its exposure.

    `exposure` is its row of the census, its interface's route and its
    discovery port; `client` its row of the single sign-on census, and
    `issuer` the portal's issuer, whose name its pod reaches at the issuer's
    addresses. `image` is the server's pin, `writer_key` the namespace's
    sealed writer key as the program read it, and `fronts` and `zone_ids`
    what the program read for `expose`. The providers arrive in `opts`: the
    Kubernetes provider for the cluster's objects and the Cloudflare one for
    its records.
    """

    namespace: ApplicationNamespace
    data: Claim
    derived: NasClaim
    media: NasClaim
    network: k8s.core.v1.ConfigMap
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        client: Client,
        issuer: Issuer,
        image: ImagePin,
        writer_key: Mapping[str, pulumi.Input[str]],
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # Refused before anything is registered.
        route = _route(exposure)
        _refuse_client(route, client)
        discovery = _discovery(exposure)
        rendered = templates.render(
            __name__,
            _TEMPLATE,
            _NetworkParams(
                http_port=HTTP_PORT,
                known_proxies=(str(conventions.POD_CIDR_V4), str(conventions.POD_CIDR_V6)),
            ),
        )
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAME, opts=self.child_opts())
        self.constraints = declared_constraints(exposure, self.namespace)
        # Read off the namespace, so each object in it waits for it; the
        # claims take the plain name, and wait for the namespace through opts.
        namespace = self.namespace.namespace.metadata.name
        in_namespace = self.child_opts(depends_on=[self.namespace.namespace])

        self.derived = nas_claim(
            f'{name}-derived', 'derived', NAS_DERIVED, namespace=NAME, read_only=False, opts=in_namespace
        )
        self.media = nas_claim(f'{name}-media', 'media', LIBRARY, namespace=NAME, read_only=True, opts=in_namespace)
        self.data = claim(
            f'{name}-data',
            'data',
            _DATA_SIZE,
            backed(STANDARD, writer_key),
            namespace=NAME,
            uid=USER,
            constraints=self.constraints & self.derived.constraints & self.media.constraints & GPU_HOLD,
            opts=in_namespace,
        )
        # Autonamed, so a change to either file is a new ConfigMap, and the
        # Deployment that names it rolls.
        self.network = k8s.core.v1.ConfigMap(
            f'{name}-network',
            metadata=k8s.meta.v1.ObjectMetaArgs(namespace=namespace),
            data={_RENDERED: rendered, _MERGE: templates.load(__name__, _MERGE_SOURCE)},
            opts=self.child_opts(),
        )

        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels=dict(LABELS), annotations=dict(self.data.pod_annotations)
                    ),
                    spec=self._pod(
                        image=image, published_url=_published_url(route), discovery=discovery, issuer=issuer
                    ),
                ),
            ),
            opts=self.child_opts(),
        )
        self.service = k8s.core.v1.Service(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector=dict(LABELS),
                ports=[k8s.core.v1.ServicePortArgs(name='http', port=HTTP_PORT, target_port='http', protocol='TCP')],
            ),
            opts=self.child_opts(),
        )
        self.exposed = expose(
            name,
            exposure,
            namespace=namespace,
            backend=Backend(service=self.service.metadata.name, port=HTTP_PORT),
            selector=LABELS,
            fronts=fronts,
            zone_ids=zone_ids,
            opts=self.child_opts(),
        )

        self.register_outputs({})

    def _pod(self, *, image: ImagePin, published_url: str, discovery: Port, issuer: Issuer) -> k8s.core.v1.PodSpecArgs:
        data = k8s.core.v1.VolumeMountArgs(name='data', mount_path=DATA_DIR)
        health = k8s.core.v1.HTTPGetActionArgs(path='/health', port='http')
        return k8s.core.v1.PodSpecArgs(
            # It speaks to its clients, the portal and metadata providers, and
            # never to Kubernetes.
            automount_service_account_token=False,
            # The Service of the server's own name would hand the pod
            # `JELLYFIN_*` variables, the prefix the server reads its
            # configuration from.
            enable_service_links=False,
            **self.data.constraints.pod_fields(),
            host_aliases=issuer_aliases(issuer),
            security_context=k8s.core.v1.PodSecurityContextArgs(
                run_as_non_root=True,
                run_as_user=USER,
                run_as_group=USER,
                seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
            ),
            init_containers=[
                # The runtime would create the parent of a mount inside the
                # claim as root, and the server writes its marker there.
                k8s.core.v1.ContainerArgs(
                    name='directories',
                    image=str(image),
                    command=['install', '-d', '-m', '0750', DATA_PATH],
                    resources=_INIT_RESOURCES,
                    security_context=_container_security(),
                    volume_mounts=[data],
                ),
                k8s.core.v1.ContainerArgs(
                    name='network',
                    image=str(image),
                    command=['/bin/sh', f'{RENDERED_MOUNT}/{_MERGE}', NETWORK_CONFIG, f'{RENDERED_MOUNT}/{_RENDERED}'],
                    resources=_INIT_RESOURCES,
                    security_context=_container_security(),
                    volume_mounts=[
                        data,
                        k8s.core.v1.VolumeMountArgs(name='rendered', mount_path=RENDERED_MOUNT, read_only=True),
                    ],
                ),
            ],
            containers=[
                k8s.core.v1.ContainerArgs(
                    name=NAME,
                    image=str(image),
                    env=[
                        k8s.core.v1.EnvVarArgs(name='JELLYFIN_DATA_DIR', value=DATA_DIR),
                        k8s.core.v1.EnvVarArgs(name='JELLYFIN_CONFIG_DIR', value=CONFIG_DIR),
                        k8s.core.v1.EnvVarArgs(name='JELLYFIN_CACHE_DIR', value=CACHE_DIR),
                        k8s.core.v1.EnvVarArgs(name='JELLYFIN_PublishedServerUrl', value=published_url),
                    ],
                    ports=[
                        k8s.core.v1.ContainerPortArgs(name='http', container_port=HTTP_PORT, protocol='TCP'),
                        k8s.core.v1.ContainerPortArgs(name='discovery', container_port=discovery.port, protocol='UDP'),
                    ],
                    # A start that migrates the database after an upgrade runs
                    # for minutes before the server answers.
                    startup_probe=k8s.core.v1.ProbeArgs(http_get=health, period_seconds=10, failure_threshold=60),
                    readiness_probe=k8s.core.v1.ProbeArgs(http_get=health, period_seconds=10, timeout_seconds=5),
                    liveness_probe=k8s.core.v1.ProbeArgs(http_get=health, period_seconds=30, timeout_seconds=10),
                    resources=k8s.core.v1.ResourceRequirementsArgs(
                        requests={'cpu': '200m', 'memory': '512Mi', GPU_RESOURCE: '1'},
                        limits={'cpu': '4', 'memory': '4Gi', GPU_RESOURCE: '1'},
                    ),
                    security_context=_container_security(),
                    volume_mounts=[
                        data,
                        k8s.core.v1.VolumeMountArgs(
                            name='data', mount_path=_FONTS, sub_path=_FONTS_DIR, read_only=True
                        ),
                        k8s.core.v1.VolumeMountArgs(name='derived', mount_path=METADATA, sub_path='metadata'),
                        k8s.core.v1.VolumeMountArgs(name='derived', mount_path=TRICKPLAY, sub_path='trickplay'),
                        k8s.core.v1.VolumeMountArgs(name='media', mount_path=MEDIA, read_only=True),
                        k8s.core.v1.VolumeMountArgs(name='cache', mount_path=CACHE_DIR),
                    ],
                )
            ],
            volumes=[
                k8s.core.v1.VolumeArgs(
                    name='data',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.data.claim.metadata.name
                    ),
                ),
                k8s.core.v1.VolumeArgs(
                    name='derived',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.derived.claim.metadata.name
                    ),
                ),
                k8s.core.v1.VolumeArgs(
                    name='media',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.media.claim.metadata.name, read_only=True
                    ),
                ),
                k8s.core.v1.VolumeArgs(
                    name='cache', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs(size_limit=f'{_CACHE_GIB}Gi')
                ),
                k8s.core.v1.VolumeArgs(
                    name='rendered',
                    config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=self.network.metadata.name),
                ),
            ],
        )


def _container_security() -> k8s.core.v1.SecurityContextArgs:
    return k8s.core.v1.SecurityContextArgs(
        allow_privilege_escalation=False,
        capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
    )
