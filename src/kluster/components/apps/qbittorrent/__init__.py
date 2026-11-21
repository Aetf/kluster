"""qBittorrent, with seedwatch beside it, at the home site (rfc-008 §5.7).

One server, in a namespace of its own, reached two ways: its interface by name
through the Gateways, on `internet` behind the portal's gate and on `lan`, and
its peer port raw, through the site's forward and pinhole to the worker's own
address. Both are rows of the exposure the component receives, and `expose`
declares them. seedwatch, which reaps what is done seeding through the server's
API, runs in the same namespace (`seedwatch`), from its own exposure.

**It runs at the home site**, held there by what it declares: its bulk egress,
since every path out of a cloud node is metered, and its peer port, which the
site's forward and pinhole land on the worker's own address
(`kluster.components.apps.declared_constraints`); and the payloads on the NAS,
which the home site alone reaches (`NasClaim.constraints`). Its rollout is
`Recreate`: the profile is one process's, which locks it.

**The interface is gated from outside, since the server can run a program.**
Its "run external program" setting, which hardlinks a finished download into
the library, turns the interface into a shell for whoever reaches it, so a row
whose `internet` side asks the portal for no gate is refused
(cluster-infra.md §2).

**What it keeps, and where** (rfc-008 §4.2):

-   its profile, the settings, the resume data of every torrent and the logs,
    under `PROFILE`, a claim backed up under `STANDARD`, which the land copies
    from the host's profile through the NAS;
-   the payloads, on the whole export, mounted at `conventions.NAS_EXPORT`:
    the profile names every path absolutely, as the host saw the export, and
    a finished download is hardlinked into `Media`, which takes one mount of
    the export rather than a `Downloads` mount and a `Media` mount, across
    whose boundary a hard link fails.

The container runs the server's binary under the image's `tini`, which reaps
the programs the server runs on completion, with the profile, the interface's
port and the peer port on its command line, which outrank the profile's.

**The component owns the settings the pod's wiring decides, and the profile
keeps every other.** Before the server starts, an init container merges the
owned settings into the profile's configuration (`settings`): each is set where
the pinned server reads it, and each key an older release kept it under, which
the server's migration would move over it, is removed. A configuration the
merge cannot read in the shape the server writes is refused, and the pod does
not start. What it owns, and why:

-   the interface bound on every address of the pod, plain HTTP, since the
    Gateway terminates TLS and the probes speak HTTP;
-   the names the interface answers at: each of the route's names, and the
    server's Service by name, which seedwatch dials. The server refuses a
    request whose `Host` names neither, and one whose port is not its own,
    so the Service forwards on the server's port unchanged;
-   **seedwatch answered without a sign-in, and a forwarded request judged by
    its client.** The server skips its sign-in for a client in the cluster's
    pod ranges (`conventions.POD_CIDR_V4`, `POD_CIDR_V6`), which seedwatch
    calls from, and trusts the same ranges as its reverse proxies: the
    Gateways' Envoy connects from them and appends its peer to
    `X-Forwarded-For`, and the server judges such a request by the nearest
    address in that header no trusted proxy vouches for, which is the
    client's, so a request through the Gateways is asked to sign in as on the
    host, whatever addresses its client wrote into the header ahead of
    Envoy's. The namespace's default-deny admits nothing else to the pod but
    its own pods and the Gateways;
-   port forwarding off, for the peer port and for the interface: the site's
    forward and pinhole are `physical`'s, and nothing in a pod reaches the
    gateway's UPnP.

The image is `qbittorrentofficial/qbittorrent-nox`, pinned as
`versions:image-qbittorrent`, and the init container runs the same image,
whose interpreter the server carries for its search plugins. Its image config
names no user, so the pod states one, as the namespace's `restricted` level
requires: `USER`, the host service's, which owns every file of the profile.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.apps import Backend, BulkEgress, Exposed, Fronts, declared_constraints, expose
from kluster.components.apps.qbittorrent import seedwatch as seedwatch_app
from kluster.components.apps.qbittorrent.seedwatch import Policy, Seedwatch
from kluster.components.apps.qbittorrent.settings import OwnedSetting
from kluster.components.apps.storage import Claim, NasClaim, claim, nas_claim
from kluster.conventions.backup import STANDARD
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import Exposure, Held, Port, Proxied, Route, SiteForward
from kluster.lib import templates
from kluster.lib.k8s import ApplicationNamespace, application_namespace
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'BULK_EGRESS',
    'CONFIGURATION',
    'EXPORT',
    'HTTP_PORT',
    'LABELS',
    'NAME',
    'PROFILE',
    'USER',
    'Qbittorrent',
    'owned_settings',
)

#: The application's name: its namespace, its Deployment and its interface's
#: Service, which seedwatch calls the API at. The namespace is a backed one of
#: `conventions.backup`, whose writer key is sealed under this name.
NAME = 'qbittorrent'

#: The labels its pods carry, which its interface's Service and its peer port's
#: Service select.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The user and group the server and its init container run as: the host
#: service's, which owns every file of the profile the land copies, and which
#: the profile claim's movers run as.
USER = 1000

#: The port the interface serves HTTP on, the server's default, which the
#: command line states and the Service forwards on unchanged.
HTTP_PORT = 8080

#: The server's profile directory, where its claim is mounted: the path the
#: host service ran with, which the profile's own logging path names.
PROFILE = '/var/lib/qBittorrent'

#: The server's configuration, which the init container merges the owned
#: settings into.
CONFIGURATION = f'{PROFILE}/qBittorrent/config/qBittorrent.conf'

#: Where the export is mounted, whole: the path the host saw it at, which every
#: path in the profile names.
EXPORT = conventions.NAS_EXPORT

#: What the server sends out, which holds it at home.
BULK_EGRESS = BulkEgress('seeding, the upload of every torrent it holds to the swarm')

#: Where the merge and the owned settings are mounted, read by the init
#: container alone.
_RENDERED = '/rendered'
_MERGE = 'settings.py'
_OWNED = 'owned.json'

#: The claim of the profile, in GiB: about 175 MB, the resume data growing with
#: each torrent added.
_PROFILE_SIZE = 2

#: What the init container asks for and is held to: one interpreter reading a
#: file of a few dozen lines.
_INIT_RESOURCES = k8s.core.v1.ResourceRequirementsArgs(
    requests={'cpu': '10m', 'memory': '32Mi'},
    limits={'cpu': '200m', 'memory': '64Mi'},
)

#: The prefix an older release kept the interface's settings under, which the
#: pinned server's migration moves to `WebUI/` (`upgrade.cpp`,
#: `migrateWebUISettings`, at the pinned release).
_LEGACY_WEBUI = 'Preferences/'


def _interface(key: str, value: str) -> OwnedSetting:
    """An owned setting of the interface's, which an older release kept under `Preferences/`."""
    return OwnedSetting(key=key, value=value, replaces=(f'{_LEGACY_WEBUI}{key}',))


def _quoted(value: str) -> str:
    """`value` as the server's settings store writes a string holding a `;`, which it would otherwise read as a separator."""
    return f'"{value}"'


def owned_settings(route: Route) -> tuple[OwnedSetting, ...]:
    """The settings the component owns, each as the pinned server writes it, with the keys it replaces."""
    ranges = (str(conventions.POD_CIDR_V4), str(conventions.POD_CIDR_V6))
    names = (*(f'{route.host}.{zone}' for zone in route.zones), NAME)
    return (
        _interface('WebUI/Address', '*'),
        _interface('WebUI/HTTPS/Enabled', 'false'),
        _interface('WebUI/ServerDomains', _quoted(';'.join(names))),
        _interface('WebUI/AuthSubnetWhitelistEnabled', 'true'),
        # A list, which the settings store writes comma-separated.
        _interface('WebUI/AuthSubnetWhitelist', ', '.join(ranges)),
        _interface('WebUI/ReverseProxySupportEnabled', 'true'),
        _interface('WebUI/TrustedReverseProxiesList', _quoted(';'.join(ranges))),
        _interface('WebUI/UseUPnP', 'false'),
        # The key the release before the settings' renaming kept it under
        # (`upgrade.cpp`, `handleSettingKeys`).
        OwnedSetting(key='Network/PortForwardingEnabled', value='false', replaces=('Preferences/Connection/UPnP',)),
    )


def _route(exposure: Exposure) -> Route:
    """The exposure's one route row, held or not, refused where its `internet` side would publish the interface ungated."""
    if len(exposure.routes) != 1:
        raise ValueError(f'qbittorrent is served at one name, and its exposure holds {len(exposure.routes)} route rows')
    (held,) = exposure.routes
    row = held.row if isinstance(held, Held) else held
    if not isinstance(row, Route):
        raise ValueError('qbittorrent is served at a route row, which the Gateways terminate TLS for')
    if row.internet is not None and not (isinstance(row.internet, Proxied) and row.internet.gate is not None):
        raise ValueError(
            f'the route {row.host!r} reaches qbittorrent from outside with no gate, and its interface can run a program'
        )
    return row


def _peer(exposure: Exposure) -> Port:
    """The exposure's one raw port, the peer port, refused unless it is on both transports through the site's forward."""
    if len(exposure.ports) != 1:
        raise ValueError(f'qbittorrent takes peers on one raw port, and its exposure holds {len(exposure.ports)}')
    (row,) = exposure.ports
    if row.transports is not Transports.TCP_AND_UDP:
        raise ValueError(
            f'the port {row.name!r} is on {row.transports.protocols}, and a peer connects over TCP and over UDP'
        )
    if not isinstance(row.internet, SiteForward):
        raise ValueError(
            f"the port {row.name!r} is not through the site's forward: inbound peer traffic lands where the "
            "server's outbound traffic leaves, the worker's own address"
        )
    return row


class Qbittorrent(Component, pulumi_type='kluster:apps:Qbittorrent'):
    """The torrent server and seedwatch: their namespace, their claims, the server's settings, their Deployments, and their exposures.

    `exposure` is the server's row of the census, its interface's route and its
    peer port, and `seedwatch_exposure` seedwatch's, one route; `policy` is
    seedwatch's, as the operator sets it. `image` and `seedwatch_image` are the
    pins they run, `writer_key` the namespace's sealed writer key as the
    program read it, which both claims' repositories take, and `fronts` and
    `zone_ids` what the program read for `expose`. The providers arrive in
    `opts`: the Kubernetes provider for the cluster's objects and the
    Cloudflare one for its records.
    """

    namespace: ApplicationNamespace
    profile: Claim
    export: NasClaim
    settings: k8s.core.v1.ConfigMap
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    exposed: Exposed
    seedwatch: Seedwatch

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        seedwatch_exposure: Exposure,
        policy: Policy,
        image: ImagePin,
        seedwatch_image: ImagePin,
        writer_key: Mapping[str, pulumi.Input[str]],
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # Refused before anything is registered.
        route = _route(exposure)
        peer = _peer(exposure)
        _ = seedwatch_app.route_of(seedwatch_exposure)
        owned = json.dumps([asdict(it) for it in owned_settings(route)], indent=2) + '\n'
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAME, opts=self.child_opts())
        self.constraints = declared_constraints(exposure, self.namespace, bulk_egress=BULK_EGRESS)
        # Read off the namespace, so each object in it waits for it; the
        # claims take the plain name, and wait for the namespace through opts.
        namespace = self.namespace.namespace.metadata.name
        in_namespace = self.child_opts(depends_on=[self.namespace.namespace])

        self.export = nas_claim(f'{name}-export', 'export', '', namespace=NAME, read_only=False, opts=in_namespace)
        self.profile = claim(
            f'{name}-profile',
            'profile',
            _PROFILE_SIZE,
            STANDARD,
            namespace=NAME,
            uid=USER,
            writer_key=writer_key,
            constraints=self.constraints & self.export.constraints,
            opts=in_namespace,
        )
        # Autonamed, so a change to either file is a new ConfigMap, and the
        # Deployment that names it rolls.
        self.settings = k8s.core.v1.ConfigMap(
            f'{name}-settings',
            metadata=k8s.meta.v1.ObjectMetaArgs(namespace=namespace),
            data={_OWNED: owned, _MERGE: templates.load(__name__, _MERGE)},
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
                        labels=dict(LABELS), annotations=dict(self.profile.pod_annotations)
                    ),
                    spec=self._pod(image=image, peer=peer),
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
        self.seedwatch = Seedwatch(
            f'{name}-{seedwatch_app.NAME}',
            namespace=self.namespace,
            namespace_name=NAME,
            server=f'http://{NAME}:{HTTP_PORT}',
            server_tree=EXPORT,
            exposure=seedwatch_exposure,
            policy=policy,
            image=seedwatch_image,
            writer_key=writer_key,
            fronts=fronts,
            zone_ids=zone_ids,
            opts=self.child_opts(),
        )

        self.register_outputs({})

    def _pod(self, *, image: ImagePin, peer: Port) -> k8s.core.v1.PodSpecArgs:
        profile = k8s.core.v1.VolumeMountArgs(name='profile', mount_path=PROFILE)
        interface = k8s.core.v1.HTTPGetActionArgs(path='/', port='http')
        container_security = k8s.core.v1.SecurityContextArgs(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
        )
        return k8s.core.v1.PodSpecArgs(
            # It speaks to its peers, its trackers and its clients, and never
            # to Kubernetes.
            automount_service_account_token=False,
            # It finds no Service by the variables Kubernetes would hand it.
            enable_service_links=False,
            **self.profile.constraints.pod_fields(),
            security_context=k8s.core.v1.PodSecurityContextArgs(
                run_as_non_root=True,
                run_as_user=USER,
                run_as_group=USER,
                seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
            ),
            init_containers=[
                k8s.core.v1.ContainerArgs(
                    name='settings',
                    image=str(image),
                    command=['python3', f'{_RENDERED}/{_MERGE}', CONFIGURATION, f'{_RENDERED}/{_OWNED}'],
                    resources=_INIT_RESOURCES,
                    security_context=container_security,
                    volume_mounts=[
                        profile,
                        k8s.core.v1.VolumeMountArgs(name='rendered', mount_path=_RENDERED, read_only=True),
                    ],
                ),
            ],
            containers=[
                k8s.core.v1.ContainerArgs(
                    name=NAME,
                    image=str(image),
                    command=[
                        '/sbin/tini',
                        '-g',
                        '--',
                        'qbittorrent-nox',
                        f'--profile={PROFILE}',
                        # The resume data names each torrent's files relative
                        # to the profile, as the host service wrote it.
                        '--relative-fastresume',
                        '--confirm-legal-notice',
                        f'--webui-port={HTTP_PORT}',
                        f'--torrenting-port={peer.port}',
                    ],
                    ports=[
                        k8s.core.v1.ContainerPortArgs(name='http', container_port=HTTP_PORT, protocol='TCP'),
                        *(
                            k8s.core.v1.ContainerPortArgs(
                                name=f'peer-{protocol}', container_port=peer.port, protocol=protocol.upper()
                            )
                            for protocol in peer.transports.protocols
                        ),
                    ],
                    # A start that loads the resume data of every torrent runs
                    # for a while before the interface answers.
                    startup_probe=k8s.core.v1.ProbeArgs(http_get=interface, period_seconds=10, failure_threshold=30),
                    readiness_probe=k8s.core.v1.ProbeArgs(http_get=interface, period_seconds=10, timeout_seconds=5),
                    liveness_probe=k8s.core.v1.ProbeArgs(http_get=interface, period_seconds=30, timeout_seconds=10),
                    # The memory is the host process's running size, with room
                    # for the disk cache the limit bounds.
                    resources=k8s.core.v1.ResourceRequirementsArgs(
                        requests={'cpu': '100m', 'memory': '192Mi'},
                        limits={'cpu': '2', 'memory': '1Gi'},
                    ),
                    security_context=container_security,
                    volume_mounts=[
                        profile,
                        k8s.core.v1.VolumeMountArgs(name='export', mount_path=EXPORT),
                    ],
                )
            ],
            volumes=[
                k8s.core.v1.VolumeArgs(
                    name='profile',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.profile.claim.metadata.name
                    ),
                ),
                k8s.core.v1.VolumeArgs(
                    name='export',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.export.claim.metadata.name
                    ),
                ),
                k8s.core.v1.VolumeArgs(
                    name='rendered',
                    config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=self.settings.metadata.name),
                ),
            ],
        )
