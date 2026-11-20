"""The pull of OCI's block-volume series: which volumes it reads, the ConfigMap carrying it, and its hourly CronJob.

**Pulled, because one fact lives only there.** A volume held to its
performance level shows on its node as latency; only OCI's throttled-I/O
series says the volume refused the I/O. OCI publishes the block and boot
volumes' series under `conventions.providers.OCI_BLOCKSTORE_METRICS` with no
agent on the instance, so a job in the cluster reads them under a key whose
one statement reads that namespace of `physical`'s compartment
(`conventions.sealed.OCI_METRICS`, credentials.md §3) and writes them to
vmagent, which writes them to both stores; the history keeps them
(`stores.HISTORY_CLASS`).

**The program is `oci_pull`**, rendered into a ConfigMap (`PULL_CONFIGMAP`)
with the run's settings beside it, and run by the `oci-sdk` image, which
carries the interpreter and the SDK and no code of this repository
(framework/ci.md §4). The settings name every target volume by its OCID, its
node and its name (`targets`); the SDK's key opens into a Secret the pod
mounts, so no rendered object and no process argument carries it.

**The targets are the census's, looked up in `physical`'s map.** Every cloud
node's boot volume and every node volume under the node it is attached to is
looked up in the `volume_ids` output by its node and its name. A declared
volume the map lacks is refused by name: a targeted apply of `physical` that
leaves a volume uncreated writes no OCID for it, and a pull built from the map
alone would then have no target for that volume and say nothing. A volume the
map holds and the census does not declare is not pulled.

**It is a row of the scheduled-job family**
(`conventions.schedule.OCI_METRICS_PULL`), which gives the CronJob its name,
its period and the deadline a run is cut off at. A run that fails, a mapped volume OCI returned nothing of among them,
leaves the CronJob's last success where it was: the family's rules report the
failed run within its deadline, and the pull as stale once its last success is
older than the hourly period allows (`rules`). A run reads up to
`oci_pull.BACKFILL` back, so a gap shorter than that, a rotated key the cluster
does not hold yet among its causes, is filled by the first run that succeeds
after it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.monitoring import memory
from kluster.conventions.cloud import NodeVolumeEntry
from kluster.conventions.providers import OCI_BLOCKSTORE_METRICS
from kluster.conventions.schedule import OCI_METRICS_PULL
from kluster.lib import templates
from kluster.lib.k8s import Constraints
from kluster.lib.versions import ImagePin

__all__ = (
    'CREDENTIALS_DIR',
    'PROGRAM',
    'PROGRAM_DIR',
    'PROGRAM_FILE',
    'PULL_CONFIGMAP',
    'PULL_USER',
    'SETTINGS_FILE',
    'Target',
    'VolumeUnpublished',
    'cronjob_spec',
    'settings',
    'targets',
)

#: The minute of each hour it runs at: apart from the stores' backups.
_PULL_MINUTE = 30

#: The ConfigMap carrying the program and its settings, where the pod mounts
#: it, and the two files in it.
PULL_CONFIGMAP = 'oci-metrics-pull'
PROGRAM_DIR = '/opt/kluster/oci-pull'
PROGRAM_FILE = 'oci_pull.py'
SETTINGS_FILE = 'settings.json'

#: Where the pod mounts the Secret the sealed key opens into, a file per key.
CREDENTIALS_DIR = '/etc/kluster/oci-metrics'

#: The program, as the ConfigMap carries it.
PROGRAM = templates.load('kluster.components.monitoring', PROGRAM_FILE)

#: The user and group the pull runs as: `nobody`, which owns nothing in the
#: image. The key's files are readable by the group alone.
PULL_USER = 65534
_KEY_MODE = 0o440
_PROGRAM_MODE = 0o444


@dataclass(frozen=True)
class Target:
    """A volume the census declares, by the node it belongs to and its name there."""

    node: str
    volume: str


class VolumeUnpublished(LookupError):
    """A volume the census declares, which `physical`'s `volume_ids` output holds no OCID for."""

    def __init__(self, target: Target) -> None:
        super().__init__(
            f'the {target.volume!r} volume of {target.node} is declared, and the {conventions.PHYSICAL_OUTPUTS.volume_ids!r} '
            f'output of `physical` holds no OCID for it, so its series would be pulled by nothing: apply `physical`, '
            'whose apply publishes it, and then this stack'
        )
        self.target: Target = target


def targets(
    volume_ids: Mapping[str, Mapping[str, str]],
    *,
    nodes: Sequence[str],
    boot_volume: str,
    node_volumes: Mapping[str, NodeVolumeEntry],
) -> dict[str, Target]:
    """Each volume the census declares, by its OCID in `volume_ids`: every node's boot volume, each node volume on its node.

    A declared volume the map lacks is refused by name (`VolumeUnpublished`);
    an entry of the map the census does not declare is left out.
    """
    declared = [
        *(Target(node, boot_volume) for node in nodes),
        *(Target(entry.attached_node, name) for name, entry in node_volumes.items()),
    ]
    found: dict[str, Target] = {}
    for target in declared:
        ocid = volume_ids.get(target.node, {}).get(target.volume)
        if ocid is None:
            raise VolumeUnpublished(target)
        found[ocid] = target
    return found


def settings(found: Mapping[str, Target], *, compartment: str, history: str, importer: str) -> str:
    """The run's settings file, as `oci_pull.Job.read` reads it."""
    return json.dumps(
        {
            'namespace': OCI_BLOCKSTORE_METRICS,
            'compartment': compartment,
            'tenancy': conventions.OCI_TENANCY.tenancy_ocid,
            'region': conventions.OCI_TENANCY.region,
            'targets': {ocid: {'node': it.node, 'volume': it.volume} for ocid, it in sorted(found.items())},
            'history': history,
            'importer': importer,
        },
        indent=2,
        sort_keys=True,
    )


def cronjob_spec(*, image: ImagePin, constraints: Constraints) -> k8s.batch.v1.CronJobSpecArgs:
    """The pull every hour, its row's period, at its minute, one run at a time, cut off at its row's deadline.

    A failed run is left to the next hour's, which reads from the newest
    sample the stores hold and so fills what the failed one missed. The pod
    runs as `PULL_USER` on a read-only root, with no capability, and reads the
    program, its settings and the key from what it mounts. It is placed under
    `constraints`.
    """
    return k8s.batch.v1.CronJobSpecArgs(
        schedule=f'{_PULL_MINUTE} * * * *',
        concurrency_policy='Forbid',
        job_template=k8s.batch.v1.JobTemplateSpecArgs(
            spec=k8s.batch.v1.JobSpecArgs(
                backoff_limit=0,
                active_deadline_seconds=int(OCI_METRICS_PULL.deadline.total_seconds()),
                template=k8s.core.v1.PodTemplateSpecArgs(spec=_pod(image, constraints)),
            ),
        ),
    )


def _pod(image: ImagePin, constraints: Constraints) -> k8s.core.v1.PodSpecArgs:
    resources = memory.OCI_PULL.rendered()
    return k8s.core.v1.PodSpecArgs(
        restart_policy='Never',
        **constraints.pod_fields(),
        security_context=k8s.core.v1.PodSecurityContextArgs(
            run_as_non_root=True,
            run_as_user=PULL_USER,
            run_as_group=PULL_USER,
            fs_group=PULL_USER,
            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
        ),
        containers=[
            k8s.core.v1.ContainerArgs(
                name='pull',
                image=str(image),
                # Isolated: the interpreter reads no environment variable and
                # no user site, and writes no bytecode to the read-only root.
                command=[
                    'python',
                    '-I',
                    '-B',
                    f'{PROGRAM_DIR}/{PROGRAM_FILE}',
                    f'{PROGRAM_DIR}/{SETTINGS_FILE}',
                    CREDENTIALS_DIR,
                ],
                resources=k8s.core.v1.ResourceRequirementsArgs(
                    requests=resources['requests'], limits=resources.get('limits')
                ),
                security_context=k8s.core.v1.SecurityContextArgs(
                    allow_privilege_escalation=False,
                    read_only_root_filesystem=True,
                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                ),
                volume_mounts=[
                    k8s.core.v1.VolumeMountArgs(name='program', mount_path=PROGRAM_DIR, read_only=True),
                    k8s.core.v1.VolumeMountArgs(name='key', mount_path=CREDENTIALS_DIR, read_only=True),
                ],
            )
        ],
        volumes=[
            k8s.core.v1.VolumeArgs(
                name='program',
                config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=PULL_CONFIGMAP, default_mode=_PROGRAM_MODE),
            ),
            k8s.core.v1.VolumeArgs(
                name='key',
                secret=k8s.core.v1.SecretVolumeSourceArgs(
                    secret_name=conventions.sealed.OCI_METRICS.name, default_mode=_KEY_MODE
                ),
            ),
        ],
    )
