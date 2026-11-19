"""The state table's daily upkeep: the census, the prune of the expired rows and the ceiling, as a CronJob on the control planes.

**What a run does** is what `state-backend census --prune-expired --yes
--ceiling` does on a workstation, through the same function
(`kluster.lib.state_backend.table.run`): the census of the state table, the
delete of the rows its retention rule expires, and the ceiling over the table
as the delete left it, which names every value larger than the row the
pooler's cap was derived for (physical/state-backend.md §2). The status is
non-zero when the delete passed over a row it listed or a value is over the
ceiling, and the log names which. It is a row of the scheduled-job family
(`conventions.schedule.STATE_UPKEEP`), which gives the CronJob its name, its
period, the deadline a run is cut off at and the deadline it starts by, and
whose rules report a failed run and a stale one (physical/state-backend.md
§7.6).

**The program is `table`**, the module the workstation's command imports,
rendered into a ConfigMap (`PROGRAM_CONFIGMAP`) as it is on disk, beside a
settings file rendered from the values of `kluster.lib.state_backend.settings`
it reads (`kluster.lib.state_backend.state.table_settings`): the database, the
retention rule's lengths, the ceiling and a client bundle's file names. The
module imports the standard library alone, so the `postgres-client` image runs
it with nothing of this repository beside it; the image carries the
interpreter and the Postgres client and no code of this repository
(framework/ci.md §4). The code and the values therefore reach the cluster in
the `up` of `k8s-base` that carries a change to them, and the job runs the
commit a workstation runs.

**It reaches the state backend as the role `upkeep`**, which reads and deletes
rows of the state table and nothing else, with the client bundle sealed for
it (`conventions.sealed.STATE_UPKEEP_BUNDLE`, credentials.md §3). The
Secret's keys are a bundle directory's file names (`kluster.lib.bundle`), so
the pod mounts it as one, readable by the pod's group alone, which libpq
accepts for a key root owns. The connection string goes through the pooler's
transaction entry with `verify-full` against the bundle's CA, and the census's
session is read-only (physical/state-backend.md §2). Nothing in the cluster
stands between a control plane and the box: the namespace is a platform
namespace, which no egress policy covers, and the box admits its port from
anywhere (physical/state-backend.md §4). The job speaks to Postgres alone, so
its pod mounts no Kubernetes token.

**The namespace holds the job and what it mounts, and nothing else**, so no
other workload can mount the bundle. The job runs on the control planes, as
the etcd snapshot does: it goes on through an outage of the home site, its
traffic stays in the cloud, and only the control planes' kubelets are served
the bundle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.conventions.schedule import STATE_UPKEEP
from kluster.lib import templates
from kluster.lib.k8s import namespace, sealed_value
from kluster.lib.state_backend import state
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'BUNDLE_DIR',
    'PROGRAM',
    'PROGRAM_CONFIGMAP',
    'PROGRAM_DIR',
    'PROGRAM_FILE',
    'SETTINGS',
    'SETTINGS_FILE',
    'UPKEEP_USER',
    'StateUpkeep',
    'cronjob_spec',
)

#: The time of day a run is scheduled at, in UTC, the box's clock: clear of
#: its nightly dump (`settings.DUMP_SCHEDULE` and the timer's randomized
#: delay, physical/state-backend.md §5) and of its weekly reboot window
#: (`settings.REBOOT_DAY`, `REBOOT_TIME` and `REBOOT_WINDOW_MINUTES`, §1).
#: A fixed time is what lets an operator keep a `pulumi stack rename` away
#: from a prune (physical/state-backend.md §2).
_HOUR = 6
_MINUTE = 15
_TIME_ZONE = 'Etc/UTC'

#: The ConfigMap carrying the program and its settings, where the pod mounts
#: it, and the two files in it.
PROGRAM_CONFIGMAP = 'state-upkeep-program'
PROGRAM_DIR = '/opt/kluster/state-upkeep'
PROGRAM_FILE = 'table.py'
SETTINGS_FILE = 'settings.json'

#: Where the pod mounts the client bundle's Secret, a file per key.
BUNDLE_DIR = '/etc/kluster/state-backend'

#: The program, as the ConfigMap carries it: the module the workstation's
#: command imports, as it is on disk.
PROGRAM = templates.load('kluster.lib.state_backend', PROGRAM_FILE)

#: The settings file, as `table.Settings.load` reads it.
SETTINGS = state.table_settings().dumps()

#: The user and group the job runs as: `nobody`, which owns nothing in the
#: image. The bundle's files are readable by the group alone.
UPKEEP_USER = 65534
_BUNDLE_MODE = 0o440
_PROGRAM_MODE = 0o444


def cronjob_spec(*, image: ImagePin) -> k8s.batch.v1.CronJobSpecArgs:
    """The upkeep once a day, its row's period, one run at a time, started and cut off by its row's deadlines.

    A failed run is left to the next day's: the prune and the ceiling read
    the table as it is then, so a missed run costs a day of the rows it would
    have deleted and nothing after it. The pod runs as `UPKEEP_USER` on a
    read-only root, with no capability, and reads the program, its settings
    and the bundle from what it mounts.
    """
    return k8s.batch.v1.CronJobSpecArgs(
        schedule=f'{_MINUTE} {_HOUR} * * *',
        time_zone=_TIME_ZONE,
        concurrency_policy='Forbid',
        starting_deadline_seconds=(
            int(STATE_UPKEEP.starting_deadline.total_seconds()) if STATE_UPKEEP.starting_deadline else None
        ),
        job_template=k8s.batch.v1.JobTemplateSpecArgs(
            spec=k8s.batch.v1.JobSpecArgs(
                backoff_limit=0,
                active_deadline_seconds=int(STATE_UPKEEP.deadline.total_seconds()),
                template=k8s.core.v1.PodTemplateSpecArgs(spec=_pod(image)),
            ),
        ),
    )


def _pod(image: ImagePin) -> k8s.core.v1.PodSpecArgs:
    return k8s.core.v1.PodSpecArgs(
        restart_policy='Never',
        # It speaks Postgres alone, and never Kubernetes.
        automount_service_account_token=False,
        affinity=k8s.core.v1.AffinityArgs(
            node_affinity=k8s.core.v1.NodeAffinityArgs(
                required_during_scheduling_ignored_during_execution=k8s.core.v1.NodeSelectorArgs(
                    node_selector_terms=[
                        k8s.core.v1.NodeSelectorTermArgs(
                            match_expressions=[
                                k8s.core.v1.NodeSelectorRequirementArgs(key=CONTROL_PLANE_LABEL, operator='Exists')
                            ]
                        )
                    ]
                )
            )
        ),
        security_context=k8s.core.v1.PodSecurityContextArgs(
            run_as_non_root=True,
            run_as_user=UPKEEP_USER,
            run_as_group=UPKEEP_USER,
            fs_group=UPKEEP_USER,
            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
        ),
        containers=[
            k8s.core.v1.ContainerArgs(
                name='upkeep',
                image=str(image),
                # Isolated: the interpreter reads no environment variable and
                # no user site, and writes no bytecode to the read-only root.
                command=[
                    'python',
                    '-I',
                    '-B',
                    f'{PROGRAM_DIR}/{PROGRAM_FILE}',
                    f'{PROGRAM_DIR}/{SETTINGS_FILE}',
                    BUNDLE_DIR,
                ],
                security_context=k8s.core.v1.SecurityContextArgs(
                    allow_privilege_escalation=False,
                    read_only_root_filesystem=True,
                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                ),
                volume_mounts=[
                    k8s.core.v1.VolumeMountArgs(name='program', mount_path=PROGRAM_DIR, read_only=True),
                    k8s.core.v1.VolumeMountArgs(name='bundle', mount_path=BUNDLE_DIR, read_only=True),
                ],
            )
        ],
        volumes=[
            k8s.core.v1.VolumeArgs(
                name='program',
                config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=PROGRAM_CONFIGMAP, default_mode=_PROGRAM_MODE),
            ),
            k8s.core.v1.VolumeArgs(
                name='bundle',
                secret=k8s.core.v1.SecretVolumeSourceArgs(
                    secret_name=conventions.sealed.STATE_UPKEEP_BUNDLE.name, default_mode=_BUNDLE_MODE
                ),
            ),
        ],
    )


class StateUpkeep(Component, pulumi_type='kluster:state_upkeep:StateUpkeep'):
    """The upkeep's namespace, its program, its client bundle and its CronJob.

    `bundle` is the ciphertext the stack's configuration holds for
    `conventions.sealed.STATE_UPKEEP_BUNDLE`
    (`kluster.lib.k8s.sealed_ciphertext`). `image` is the pin the CronJob
    runs.

    `after` is what it is installed behind: Cilium, since nothing schedules
    before the CNI, and the sealing controller, whose chart defines the
    bundle's SealedSecret. It is stated on the children rather than left to
    the component's own `depends_on`, which Pulumi does not push down to a
    component's children.
    """

    def __init__(
        self,
        name: str,
        *,
        image: ImagePin,
        bundle: Mapping[str, str],
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)

        self.namespace = namespace(
            f'{name}-namespace',
            name=STATE_UPKEEP.namespace,
            opts=self.child_opts(depends_on=list(after)),
        )
        self.program = k8s.core.v1.ConfigMap(
            f'{name}-program',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=PROGRAM_CONFIGMAP, namespace=self.namespace.metadata.name),
            data={PROGRAM_FILE: PROGRAM, SETTINGS_FILE: SETTINGS},
            opts=self.child_opts(depends_on=[self.namespace]),
        )
        # Behind the sealing controller, whose chart defines the kind, and the
        # namespace the bundle is sealed to.
        self.bundle = sealed_value(
            f'{name}-bundle',
            conventions.sealed.STATE_UPKEEP_BUNDLE,
            ciphertext=bundle,
            opts=self.child_opts(depends_on=[*after, self.namespace]),
        )
        # Behind what its pod mounts: the program's ConfigMap and the bundle.
        self.cronjob = k8s.batch.v1.CronJob(
            f'{name}-{STATE_UPKEEP.cronjob}',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=STATE_UPKEEP.cronjob, namespace=self.namespace.metadata.name),
            spec=cronjob_spec(image=image),
            opts=self.child_opts(depends_on=[self.program, self.bundle]),
        )

        self.register_outputs({})
