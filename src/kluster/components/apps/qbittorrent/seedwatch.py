"""seedwatch: qBittorrent's seed lifecycle reconciler, beside the server it reconciles (rfc-008 §5.7).

seedwatch reads the torrents and their tags through qBittorrent's API, stats
the files of each where the server keeps them, tags what is ready to reap, and
asks the server to delete it once its grace has passed: every deletion is the
server's, and seedwatch itself writes nothing but its two logs. It serves its
report and its adjudication interface over HTTP, reached by its route row
alone, which the Gateway terminates TLS for.

**It runs in qBittorrent's namespace and calls the server's Service by name.**
The server answers it without a sign-in because it calls from an address in the
cluster's pod ranges with no `X-Forwarded-For` (`kluster.components.apps.qbittorrent`
says how the server tells it from a request a Gateway forwards), so seedwatch
needs no credential. The namespace's default-deny admits its pods to the
server's.

**Nothing guards its interface but the network**: the interface tags and
deletes torrents and asks for no sign-in, so a row whose `internet` side asks
the portal for no gate is refused, as on the host, where the gateway's proxy
served it to the home alone.

**What it keeps, and where** (rfc-008 §4.2):

-   its audit history and its pass history, two JSON-lines files it appends to
    in its data directory, a claim backed up under `STANDARD`;
-   the seeding tree, `DOWNLOADS` on the export, read-only, mounted at the path
    the server keeps it at, because seedwatch stats each file at the path the
    server reports it under.

**Its policy is the operator's and its wiring is the component's.** The
policy, which categories it manages, how long it waits, whether it acts and at
what ratio a torrent is done, is a `Policy` the program hands it; the server's
address, the seeding tree's path, the port and the data directory are what the
pod's wiring decides, so the component states them. The address, the port and
the data directory are environment variables, which outrank the policy file,
and the tree's path is in the rendered file.

The image is built in `docker/` from a pinned commit of its source and pinned as
`versions:image-seedwatch`. Its image config names a numeric user, which the pod
states too, so the claim's movers run as the user that writes the data.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field

import pulumi
import pulumi_kubernetes as k8s

from kluster.components.apps import Backend, Exposed, Fronts, declared_constraints, expose
from kluster.components.apps.storage import Claim, NasClaim, claim, nas_claim
from kluster.conventions.backup import STANDARD
from kluster.conventions.routes import Exposure, Proxied, Route
from kluster.lib.k8s import ApplicationNamespace
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'CONFIG_FILE',
    'DATA_DIR',
    'DOWNLOADS',
    'LABELS',
    'NAME',
    'PORT',
    'USER',
    'Policy',
    'Seedwatch',
    'Tracker',
    'policy_file',
    'route_of',
)

#: The application's name: its Deployment, its Service and its claim's base name,
#: in qBittorrent's namespace.
NAME = 'seedwatch'

#: The labels its pods carry and its Service selects.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The port it serves its interface on, which its environment states and its
#: Service forwards to.
PORT = 8490

#: The user and group it runs as: the image's, which owns nothing in the image,
#: so its data claim and the claim's movers are this user's.
USER = 65534

#: Its data directory, where its claim is mounted: the two logs.
DATA_DIR = '/data'

#: Where it reads its policy, the image's default, which the rendered policy is
#: mounted at.
CONFIG_FILE = '/config/config.toml'

#: The path under the export that holds the seeding tree.
DOWNLOADS = 'Downloads'

#: The claim of its data directory, in GiB: two append-only logs of a few
#: megabytes.
_DATA_SIZE = 1

#: What answers once the web server listens: the report, which is in memory.
_REPORT = '/api/report'


@dataclass(frozen=True, kw_only=True)
class Tracker:
    """When a torrent of one tracker is done seeding: at `ratio`, or after `or_seed_days` of seeding where it is set."""

    ratio: float
    or_seed_days: float | None = None


@dataclass(frozen=True, kw_only=True)
class Policy:
    """seedwatch's policy, as the operator sets it.

    Each field is the setting of seedwatch's policy file of that name:
    `managed_categories`, `grace_hours` and `interval_minutes` under `scan`,
    `dry_run`, `default_ratio` and `trackers` under `reap`, each tracker keyed
    by the host name suffix it matches.
    """

    managed_categories: tuple[str, ...]
    grace_hours: float
    interval_minutes: float
    dry_run: bool
    default_ratio: float
    trackers: Mapping[str, Tracker] = field(default_factory=dict[str, Tracker])

    def __post_init__(self) -> None:
        numbers = [self.grace_hours, self.interval_minutes, self.default_ratio]
        for tracker in self.trackers.values():
            numbers += [tracker.ratio] + ([] if tracker.or_seed_days is None else [tracker.or_seed_days])
        if not all(math.isfinite(it) for it in numbers):
            raise ValueError("seedwatch's policy is finite numbers, which its policy file can write")


def _string(value: str) -> str:
    """`value` as a TOML basic string: JSON's escapes are TOML's."""
    return json.dumps(value, ensure_ascii=False)


def policy_file(policy: Policy, *, downloads: str) -> str:
    """seedwatch's policy file: `policy`, and the seeding tree at `downloads`, the path the server keeps it at."""
    lines = [
        '[scan]',
        f'downloads = {_string(downloads)}',
        f'managed_categories = [{", ".join(_string(it) for it in policy.managed_categories)}]',
        f'grace_hours = {float(policy.grace_hours)!r}',
        f'interval_minutes = {float(policy.interval_minutes)!r}',
        '',
        '[reap]',
        f'dry_run = {"true" if policy.dry_run else "false"}',
        f'default_ratio = {float(policy.default_ratio)!r}',
    ]
    for host, tracker in policy.trackers.items():
        lines += ['', f'[reap.trackers.{_string(host)}]', f'ratio = {float(tracker.ratio)!r}']
        if tracker.or_seed_days is not None:
            lines.append(f'or_seed_days = {float(tracker.or_seed_days)!r}')
    return '\n'.join(lines) + '\n'


def route_of(exposure: Exposure) -> Route:
    """The exposure's one row, a route, refused where its `internet` side would publish the interface ungated."""
    if exposure.ports or len(exposure.routes) != 1:
        raise ValueError(
            f'seedwatch is reached by one route row alone, and its exposure holds {len(exposure.routes)} route rows '
            f'and {len(exposure.ports)} raw ports'
        )
    (row,) = exposure.routes
    if not isinstance(row, Route):
        raise ValueError(f'seedwatch is reached by a route row, and its exposure holds a {type(row).__name__}')
    if row.internet is not None and not (isinstance(row.internet, Proxied) and row.internet.gate is not None):
        raise ValueError(
            f'the route {row.host!r} reaches seedwatch from outside with no gate, and its interface deletes torrents '
            'with no sign-in of its own'
        )
    return row


class Seedwatch(Component, pulumi_type='kluster:apps:Seedwatch'):
    """seedwatch's claims, its policy, its Deployment and Service, and what its exposure derives, in qBittorrent's namespace.

    `namespace` is the namespace qBittorrent's component declared, and
    `namespace_name` its name as declared; `server` the
    URL of the server's API, its Service by name; `server_tree` where the server
    keeps the export, so that the seeding tree is mounted at the path the server
    reports its files under. `exposure` is its row of the census, one route;
    `policy` the operator's; `image` its pin; `writer_key` the namespace's
    sealed writer key as the program read it; and `fronts` and `zone_ids` what
    the program read for `expose`.
    """

    data: Claim
    downloads: NasClaim
    policy: k8s.core.v1.ConfigMap
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        namespace: ApplicationNamespace,
        namespace_name: str,
        server: str,
        server_tree: str,
        exposure: Exposure,
        policy: Policy,
        image: ImagePin,
        writer_key: Mapping[str, pulumi.Input[str]],
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # Refused before anything is registered.
        _ = route_of(exposure)
        self.tree = f'{server_tree}/{DOWNLOADS}'
        rendered = policy_file(policy, downloads=self.tree)
        super().__init__(name, opts=opts)

        self.constraints = declared_constraints(exposure, namespace)
        # Read off the namespace, so each object in it waits for it; the claims
        # take the plain name, and wait for it through opts.
        in_namespace_name = namespace.namespace.metadata.name
        in_namespace = self.child_opts(depends_on=[namespace.namespace])

        self.downloads = nas_claim(
            f'{name}-downloads',
            f'{NAME}-downloads',
            DOWNLOADS,
            namespace=namespace_name,
            read_only=True,
            opts=in_namespace,
        )
        self.data = claim(
            f'{name}-data',
            f'{NAME}-data',
            _DATA_SIZE,
            STANDARD,
            namespace=namespace_name,
            uid=USER,
            writer_key=writer_key,
            constraints=self.constraints & self.downloads.constraints,
            opts=in_namespace,
        )
        # Autonamed, so a change to the policy is a new ConfigMap, and the
        # Deployment that names it rolls.
        self.policy = k8s.core.v1.ConfigMap(
            f'{name}-policy',
            metadata=k8s.meta.v1.ObjectMetaArgs(namespace=in_namespace_name),
            data={'config.toml': rendered},
            opts=self.child_opts(),
        )
        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=in_namespace_name),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                # Two reconcilers would tag and reap the same torrents, and
                # append to the same logs.
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels=dict(LABELS), annotations=dict(self.data.pod_annotations)
                    ),
                    spec=self._pod(image, server),
                ),
            ),
            opts=self.child_opts(),
        )
        self.service = k8s.core.v1.Service(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=in_namespace_name),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector=dict(LABELS),
                ports=[k8s.core.v1.ServicePortArgs(name='http', port=PORT, target_port='http', protocol='TCP')],
            ),
            opts=self.child_opts(),
        )
        self.exposed = expose(
            name,
            exposure,
            namespace=in_namespace_name,
            backend=Backend(service=self.service.metadata.name, port=PORT),
            selector=None,
            fronts=fronts,
            zone_ids=zone_ids,
            opts=self.child_opts(),
        )

        self.register_outputs({})

    def _pod(self, image: ImagePin, server: str) -> k8s.core.v1.PodSpecArgs:
        report = k8s.core.v1.HTTPGetActionArgs(path=_REPORT, port='http')
        return k8s.core.v1.PodSpecArgs(
            # It speaks to the server's API alone, and never to Kubernetes.
            automount_service_account_token=False,
            enable_service_links=False,
            **self.data.constraints.pod_fields(),
            security_context=k8s.core.v1.PodSecurityContextArgs(
                run_as_non_root=True,
                run_as_user=USER,
                run_as_group=USER,
                seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
            ),
            containers=[
                k8s.core.v1.ContainerArgs(
                    name=NAME,
                    image=str(image),
                    env=[
                        k8s.core.v1.EnvVarArgs(name='QB_URL', value=server),
                        k8s.core.v1.EnvVarArgs(name='PORT', value=str(PORT)),
                        k8s.core.v1.EnvVarArgs(name='DATA_DIR', value=DATA_DIR),
                        k8s.core.v1.EnvVarArgs(name='SEEDWATCH_CONFIG', value=CONFIG_FILE),
                    ],
                    ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=PORT, protocol='TCP')],
                    readiness_probe=k8s.core.v1.ProbeArgs(http_get=report, period_seconds=10),
                    liveness_probe=k8s.core.v1.ProbeArgs(http_get=report, period_seconds=60, timeout_seconds=10),
                    # The memory is the host process's running size with room
                    # for a pass over every torrent's file list.
                    resources=k8s.core.v1.ResourceRequirementsArgs(
                        requests={'cpu': '10m', 'memory': '64Mi'},
                        limits={'cpu': '500m', 'memory': '256Mi'},
                    ),
                    security_context=k8s.core.v1.SecurityContextArgs(
                        allow_privilege_escalation=False,
                        read_only_root_filesystem=True,
                        capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                    ),
                    volume_mounts=[
                        k8s.core.v1.VolumeMountArgs(name='data', mount_path=DATA_DIR),
                        k8s.core.v1.VolumeMountArgs(
                            name='policy', mount_path=CONFIG_FILE, sub_path='config.toml', read_only=True
                        ),
                        k8s.core.v1.VolumeMountArgs(name='downloads', mount_path=self.tree, read_only=True),
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
                    name='policy', config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=self.policy.metadata.name)
                ),
                k8s.core.v1.VolumeArgs(
                    name='downloads',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.downloads.claim.metadata.name, read_only=True
                    ),
                ),
            ],
        )
