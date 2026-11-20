"""syncthing on the NAS, and the WebDAV server beside it (rfc-008 §5.3).

One syncthing instance, the NAS's, with its folders on the host's export and
its device identity in its configuration. It runs on the worker, the one node
that reaches the export (`nas_claim`), which its pod's affinity states and its
claim's movers follow (`claim`), and is reached two ways: its interface
by name, through the Gateways, and its peer port raw, at the balancer for a
roaming device and at the LAN VIP for one at home. Both are rows of the
exposure the component receives, and `expose` declares them.

**The configuration is the instance's, and the component owns the parts it
renders.** The instance's `config.xml` lives in its claim beside its key, its
certificate and its index, which is where its device identity is, so the
identity moves when the claim's contents do. Before the instance starts, an
init container merges the configuration the component renders
(`templates/config.xml.j2`) into it (`shell/config-merge.sh`): each top-level
part the rendered file holds replaces the instance's whole, and the devices
and folders, which it does not hold, stay as the instance keeps them, which is
where the interface edits them. A device on `retired_devices` leaves the
device list and every folder's list on each start. A device the instance marks
as an introducer adds a retired device back on its next connection if it
still lists it, and the entry then stays until the pod next starts. An
instance with no configuration yet takes the rendered one as its initial
state. A configuration file that holds no device is refused, and the pod does
not start: every configuration syncthing writes holds the instance's own, and
yq reads a file cut short or emptied as nothing, which a merge would replace
with no device and no folder.

**The interface's API key goes with the replaced part.** The instance's
`<gui>` is the rendered one, which names no key, and syncthing writes none into
a configuration it loads without one: the REST API then answers the
interface's own session, which a browser behind the gate holds, and the
unauthenticated health check, and refuses every other caller. Nothing in the
installation calls the REST API with a key.

**What the rendered options say, and from where.** syncthing listens for peers
on the peer row's port, on TCP where the row carries TCP and on QUIC where it
carries UDP, and adds an address of the same transports at each name the row is
dialed at, in the primary zone. Such an address never binds, which syncthing
logs each minute, and it is announced all the same: the discovery server sees
no client address it could announce for the instance, so this is how a device
with `dynamic` learns the name (rfc-008 §4.1). The instance announces to the
discovery server at its raw port, by the name it is dialed at
([Aetf/kluster-ops#723](https://github.com/Aetf/kluster-ops/issues/723)).
Local discovery is off, since a pod's broadcast reaches no device's segment.
The default folder path is the export's mount, so a folder accepted in the
interface lands on the NAS.

**The interface is served plain to the Gateway**, which terminates TLS for the
name and holds the gate. The container's `--gui-address` states the bind and
its scheme; it outranks both the image's environment, which sets an address of
its own, and the configuration's `<gui>`, which the rendered file states for an
instance started without the flag. Nothing else reaches the pod's interface port, since
the namespace's default-deny admits what the Gateways forward and the
namespace's own pods. A peer is admitted on the peer port alone, by the allow
`expose` declares for the raw row. The application base denies the pod private
space outside the cluster, so a device at home reaches the instance and the
instance does not reach the device's home address, and one connection either
way is all a pair of devices needs.

**A pod writes to the NAS as the export's anonymous user** (`nas_claim`), so a
path the instance syncs is that user's. The instance and its init container run
as `USER` below, which owns what they write in the claim and which the claim's
movers run as.

The images are `syncthing/syncthing`, pinned as `versions:image-syncthing`,
and `mikefarah/yq`, pinned as `versions:image-syncthing-config`, which runs the
merge. Neither image config names a numeric user, so the pod states one, as the
namespace's `restricted` level requires. syncthing's entrypoint drops to its
own user only when it starts as root, and run as any other user it runs the
binary as that user.

The WebDAV server is `kluster.components.apps.syncthing.webdav`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pulumi
import pulumi_kubernetes as k8s

from kluster.components.apps import Backend, Exposed, Fronts, dialed_names, expose
from kluster.components.apps.storage import Claim, NasClaim, claim, nas_claim
from kluster.conventions.backup import STANDARD
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import Exposure, Port
from kluster.lib import templates
from kluster.lib.k8s import WORKER_POD_AFFINITY, ApplicationNamespace, application_namespace
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'CONFIG_FILE',
    'FOLDER_ROOT',
    'GUI_PORT',
    'HOME',
    'LABELS',
    'NAME',
    'RENDERED_MOUNT',
    'SYNC',
    'USER',
    'Syncthing',
)

#: The application's name: its namespace, its Deployment and its interface's
#: Service. The namespace is a backed one of `conventions.backup`, whose writer
#: key is sealed under this name.
NAME = 'syncthing'

#: The labels its pods carry, which its interface's Service and its peer port's
#: Services select.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The port the interface listens on, which the container's `--gui-address`
#: binds.
GUI_PORT = 8384

#: The user and group the instance and its merge run as: the image's `PUID`
#: and `PGID`, which own every file the instance wrote in its home, and the
#: user the yq image creates.
USER = 1000

#: The instance's home: its configuration, key, certificate and index, which is
#: where the claim is mounted.
HOME = '/var/syncthing'

#: The path under the export that holds every folder the instance syncs, the
#: WebDAV server's among them.
SYNC = 'Sync'

#: Where the export's `SYNC` is mounted, which is the root every folder's path is
#: under. A folder's path is held in the instance's configuration, so this is
#: where the instance's folders already are, and moving it moves none of them.
FOLDER_ROOT = '/hostsync'

#: Where the rendered configuration and the merge script are mounted, read by
#: the merge alone.
RENDERED_MOUNT = '/rendered'

#: The instance's configuration file, which the merge writes and syncthing reads.
CONFIG_FILE = f'{HOME}/config.xml'

#: The claim of the instance's home, in GiB: its index runs to about 145 MB.
_STATE_SIZE = 1

#: The relays syncthing's own pool serves, which a device that reaches the
#: instance no other way connects through.
_RELAYS = 'dynamic+https://relays.syncthing.net/endpoint'

#: A device ID as syncthing writes it in its configuration: fifty-six base32
#: characters, the hash's fifty-two with a check character after each
#: thirteen, in eight dash-separated groups of seven.
_DEVICE_ID = re.compile(r'[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}')

#: The alphabet of a device ID, in the order its check character counts by.
_BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'

#: How many of the hash's characters each check character follows.
_CHECKED = 13

#: The files the merge reads, beside this module.
_TEMPLATE = 'templates/config.xml.j2'
_MERGE = 'config-merge.sh'
_MERGE_SOURCE = f'shell/{_MERGE}'


@dataclass(frozen=True)
class _ConfigParams:
    """What the rendered configuration is rendered with."""

    gui_port: int
    listen_addresses: tuple[str, ...]
    announce_server: str
    folder_root: str


def _schemes(transports: Transports) -> tuple[str, ...]:
    """syncthing's listener schemes for a row's transports: TCP's own, and QUIC over UDP."""
    return tuple(scheme for scheme, protocol in (('tcp', 'tcp'), ('quic', 'udp')) if protocol in transports.protocols)


def _listen_addresses(peer: Port) -> tuple[str, ...]:
    """Every address the instance listens at and announces: the peer port's own, the relays, and each name it is dialed at."""
    schemes = _schemes(peer.transports)
    names = dialed_names(peer)
    return (
        *(f'{scheme}://:{peer.port}' for scheme in schemes),
        _RELAYS,
        *(f'{scheme}://{name}:{peer.port}' for name in names for scheme in schemes),
    )


def _announce_server(discovery: Port) -> str:
    """The discovery server's address: HTTPS at the first name its raw port is dialed at, on that port.

    The server's certificate is for every name `dialed_names` gives, so the one
    announced to is among them.
    """
    names = dialed_names(discovery)
    if not names:
        raise ValueError(
            f'the discovery port {discovery.name!r} names no host a device dials it at, and an instance announces to one'
        )
    return f'https://{names[0]}:{discovery.port}'


def _peer(exposure: Exposure) -> Port:
    if len(exposure.ports) != 1:
        raise ValueError(
            f'syncthing listens for peers on one port, and its exposure holds {len(exposure.ports)} raw ports'
        )
    return exposure.ports[0]


def _check_character(chunk: str) -> str:
    """The check character syncthing appends to `chunk`: Luhn's mod 32 over the base32 alphabet (`lib/protocol/luhn.go`)."""
    total = 0
    for position, character in enumerate(chunk):
        # The factor alternates 1, 2, 1, … from the chunk's first character.
        addend = (1 + position % 2) * _BASE32.index(character)
        total += addend // len(_BASE32) + addend % len(_BASE32)
    return _BASE32[-total % len(_BASE32)]


def _is_device_id(device: str) -> bool:
    """Whether `device` is a device ID as syncthing writes one, its check characters included."""
    if _DEVICE_ID.fullmatch(device) is None:
        return False
    characters = device.replace('-', '')
    step = _CHECKED + 1
    return all(
        _check_character(characters[start : start + _CHECKED]) == characters[start + _CHECKED]
        for start in range(0, len(characters), step)
    )


def _retired(devices: Sequence[str]) -> tuple[str, ...]:
    """The device IDs the instance drops, each refused unless it is one, so a mistyped ID is not a device kept."""
    for device in devices:
        if not _is_device_id(device):
            raise ValueError(
                f'{device!r} is not a device ID as syncthing writes one: eight dash-separated groups, '
                'with its check characters'
            )
    return tuple(devices)


class Syncthing(Component, pulumi_type='kluster:apps:Syncthing'):
    """The NAS's syncthing instance: its namespace, its claims, its configuration, its Deployment, and its exposure.

    `exposure` is its row of the census, its interface's route and its one
    peer port; `discovery` is the discovery server's raw port, which the
    instance announces to; `retired_devices` the device IDs the instance
    drops. `image` and `config_image` are the pins of syncthing and of the
    merge, `writer_key` the namespace's sealed writer key as the program read
    it, and `fronts` and `zone_ids` what the program read for `expose`. The
    providers arrive in `opts`: the Kubernetes provider for the cluster's
    objects and the Cloudflare one for its records.
    """

    namespace: ApplicationNamespace
    state: Claim
    sync: NasClaim
    config: k8s.core.v1.ConfigMap
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        discovery: Port,
        retired_devices: Sequence[str],
        image: ImagePin,
        config_image: ImagePin,
        writer_key: Mapping[str, pulumi.Input[str]],
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # Refused before anything is registered.
        peer = _peer(exposure)
        retired = _retired(retired_devices)
        rendered = templates.render(
            __name__,
            _TEMPLATE,
            _ConfigParams(
                gui_port=GUI_PORT,
                listen_addresses=_listen_addresses(peer),
                announce_server=_announce_server(discovery),
                folder_root=FOLDER_ROOT,
            ),
        )
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAME, opts=self.child_opts())
        # Read off the namespace, so each object in it waits for it; the
        # claims take the plain name, and wait for the namespace through opts.
        namespace = self.namespace.namespace.metadata.name
        in_namespace = self.child_opts(depends_on=[self.namespace.namespace])

        self.state = claim(
            f'{name}-state',
            'state',
            _STATE_SIZE,
            STANDARD,
            namespace=NAME,
            uid=USER,
            writer_key=writer_key,
            placement=WORKER_POD_AFFINITY,
            opts=in_namespace,
        )
        self.sync = nas_claim(f'{name}-sync', 'sync', SYNC, namespace=NAME, read_only=False, opts=in_namespace)
        # Autonamed, so a change to either file is a new ConfigMap, and the
        # Deployment that names it rolls.
        self.config = k8s.core.v1.ConfigMap(
            f'{name}-config',
            metadata=k8s.meta.v1.ObjectMetaArgs(namespace=namespace),
            data={'config.xml': rendered, _MERGE: templates.load(__name__, _MERGE_SOURCE)},
            opts=self.child_opts(),
        )

        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                # The index is one process's: a second pod beside the first
                # would find its database locked.
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(LABELS)),
                    spec=self._pod(peer, retired, image=image, config_image=config_image),
                ),
            ),
            opts=self.child_opts(),
        )
        self.service = k8s.core.v1.Service(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector=dict(LABELS),
                ports=[k8s.core.v1.ServicePortArgs(name='gui', port=GUI_PORT, target_port='gui', protocol='TCP')],
            ),
            opts=self.child_opts(),
        )
        self.exposed = expose(
            name,
            exposure,
            namespace=namespace,
            backend=Backend(service=self.service.metadata.name, port=GUI_PORT),
            selector=LABELS,
            fronts=fronts,
            zone_ids=zone_ids,
            opts=self.child_opts(),
        )

        self.register_outputs({})

    def _pod(
        self, peer: Port, retired: tuple[str, ...], *, image: ImagePin, config_image: ImagePin
    ) -> k8s.core.v1.PodSpecArgs:
        home = k8s.core.v1.VolumeMountArgs(name='state', mount_path=HOME)
        health = k8s.core.v1.HTTPGetActionArgs(path='/rest/noauth/health', port='gui')
        return k8s.core.v1.PodSpecArgs(
            # It speaks to its peers and its interface's clients alone, and
            # never to Kubernetes.
            automount_service_account_token=False,
            affinity=self.state.placement,
            security_context=k8s.core.v1.PodSecurityContextArgs(
                run_as_non_root=True,
                run_as_user=USER,
                run_as_group=USER,
                seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
            ),
            init_containers=[
                k8s.core.v1.ContainerArgs(
                    name='config',
                    image=str(config_image),
                    command=['/bin/sh', f'{RENDERED_MOUNT}/{_MERGE}', CONFIG_FILE, f'{RENDERED_MOUNT}/config.xml'],
                    env=[k8s.core.v1.EnvVarArgs(name='RETIRED_DEVICES', value=' '.join(retired))],
                    resources=k8s.core.v1.ResourceRequirementsArgs(
                        requests={'cpu': '10m', 'memory': '16Mi'},
                        limits={'cpu': '100m', 'memory': '64Mi'},
                    ),
                    security_context=_container_security(),
                    volume_mounts=[
                        home,
                        k8s.core.v1.VolumeMountArgs(name='rendered', mount_path=RENDERED_MOUNT, read_only=True),
                    ],
                )
            ],
            containers=[
                k8s.core.v1.ContainerArgs(
                    name=NAME,
                    image=str(image),
                    args=[
                        '--no-browser',
                        '--no-restart',
                        '--no-upgrade',
                        '--log-level=INFO',
                        f'--home={HOME}',
                        f'--gui-address=http://:{GUI_PORT}',
                    ],
                    ports=[
                        k8s.core.v1.ContainerPortArgs(name='gui', container_port=GUI_PORT, protocol='TCP'),
                        *(
                            k8s.core.v1.ContainerPortArgs(
                                name=f'peer-{protocol}', container_port=peer.port, protocol=protocol.upper()
                            )
                            for protocol in peer.transports.protocols
                        ),
                    ],
                    readiness_probe=k8s.core.v1.ProbeArgs(http_get=health, period_seconds=10),
                    # The interface answers once the index is open, which takes
                    # minutes on a large one.
                    liveness_probe=k8s.core.v1.ProbeArgs(
                        http_get=health, initial_delay_seconds=300, period_seconds=60, timeout_seconds=10
                    ),
                    resources=k8s.core.v1.ResourceRequirementsArgs(
                        requests={'cpu': '10m', 'memory': '96Mi'},
                        limits={'cpu': '300m', 'memory': '256Mi'},
                    ),
                    security_context=_container_security(),
                    volume_mounts=[home, k8s.core.v1.VolumeMountArgs(name='sync', mount_path=FOLDER_ROOT)],
                )
            ],
            volumes=[
                k8s.core.v1.VolumeArgs(
                    name='state',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.state.claim.metadata.name
                    ),
                ),
                k8s.core.v1.VolumeArgs(
                    name='sync',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.sync.claim.metadata.name
                    ),
                ),
                k8s.core.v1.VolumeArgs(
                    name='rendered',
                    config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=self.config.metadata.name),
                ),
            ],
        )


def _container_security() -> k8s.core.v1.SecurityContextArgs:
    return k8s.core.v1.SecurityContextArgs(
        allow_privilege_escalation=False,
        capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
    )
