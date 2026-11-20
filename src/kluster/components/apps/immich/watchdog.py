"""The job-slot watchdog: the layer that works around porsager/postgres#1195, which leaks immich's job slots.

`kysely-postgres-js` reserves a pool connection for every query. When a
connection closes while a reservation is queued behind it, postgres.js 3.x
drops that reservation and its promise never settles: the job awaiting it
holds its queue slot forever, and BullMQ keeps renewing the job's lock, so its
stalled check never reclaims it either. Slots leak one at a time until a queue
stops, while the web interface and the API stay healthy, and only a restart
releases them. immich-app/immich#32502 was closed as this upstream bug.

The layer has two parts:

-   `SERVER_ENV`, which the server's container takes, removes the client-side
    trigger: postgres.js reads `PG<OPTION>` from the environment for every
    option immich does not set itself, and a `max_lifetime` of 0 turns off
    recycling connections (immich already leaves `idle_timeout` off). A
    close from the database's side, a restart of its instance, still
    triggers it.
-   `job_slot_watchdog`, a CronJob that restarts the server when an active job
    has held its lock for longer than any legitimate job runs, logging the
    jobs it released. A run that restarts the server fails, so the leak is
    reported. It runs a script of its own, `templates/watchdog.mjs`,
    in the server's image, which carries the Redis client it reads the queues
    with and the dependency versions it reads its own obsolescence from.
    Deleting the server's pods is a call to the API, so its pod carries
    `conventions.API_CLIENT_LABEL`, under a role that lists and deletes pods
    in the namespace and does nothing else.

**Its runs are watched by the scheduled-job family.** `watchdog_job` is its
`ScheduledJob`, which names the CronJob, sets its deadlines and gives the
family's rules, which the layer declares beside the CronJob as a `VMRule`
in the namespace (operations.md §10.1). The failed alert is how a leak, a
watchdog that cannot read the queues or delete a pod, and an obsolete
watchdog are reported; it resolves at the next run that succeeds.

**Its tripwire is a test of ours.** `tests/test_apps_immich.py` reads
postgres.js's version from the pinned server image, at `POSTGRES_PACKAGE`,
and goes red on the bump that ships the fix, or that moves the file. A
watchdog that runs on such an image restarts nothing and every run fails,
naming what to delete, which is the in-cluster report of the same fact.

**The layer is everything in this package and in `tests/test_apps_immich.py`
that names the watchdog, and operations.md §10 with its row of §5's index.**
Today that is this module and `templates/watchdog.mjs`; in `__init__.py`,
the module docstring's paragraph on the queue watchdog, the import of
`watchdog`, the `watchdog` attribute of `Immich` with its mention in the
class's docstring, the server's `SERVER_ENV` entries, the
`job_slot_watchdog` call, the watchdog's clause of the namespace's API
clients, and the comments on `USER` and `REDIS` that name it; and in the
tests, the import of `watchdog_layer`, `SCRIPT`, `KUBELET_SET`, `CONFIG_MAP`,
`ROLE`, `ROLE_BINDING` and `VMRULE`, `watchdog_pod` and `every_pod`'s use of
it, `selected`, the watchdog's line and the drill's filter in
`test_each_workload_runs_the_pin_it_is_handed`, and `called_verbs` and the
cases under "the watchdog".
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.monitoring.operator_objects import operator_object
from kluster.components.monitoring.rules import RuleGroup, rule_group_spec, scheduled_job_rules
from kluster.conventions.alert import Playbook
from kluster.conventions.schedule import ScheduledJob
from kluster.lib import templates
from kluster.lib.versions import ImagePin

__all__ = (
    'FIXED_POSTGRES_MAJOR',
    'MAX_JOB_AGE',
    'NAME',
    'PLAYBOOK',
    'POSTGRES_PACKAGE',
    'QUEUE_PREFIX',
    'SCHEDULE',
    'SCRIPT',
    'SERVER_ENV',
    'Watchdog',
    'job_slot_watchdog',
    'watchdog_job',
)

#: The CronJob's name, and its role's, service account's and script's.
NAME = 'immich-job-slot-watchdog'

#: What the server's container takes in its environment: no connection is
#: ever recycled.
SERVER_ENV: Mapping[str, str] = {'PGMAX_LIFETIME': '0'}

#: Every ten minutes, so a leaked slot is released within ten minutes of
#: crossing `MAX_JOB_AGE`. A run is one Redis scan and a few calls to the API,
#: seconds long, so it is cut off at three minutes, and a run the controller
#: could not start within a minute is left to the next one: together inside
#: the slack the period leaves (`ScheduledJob.slack`).
SCHEDULE = '*/10 * * * *'
_PERIOD = dt.timedelta(minutes=10)
_DEADLINE = dt.timedelta(minutes=3)
_STARTING_DEADLINE = dt.timedelta(minutes=1)

#: Where the response to the watchdog's alerts is written.
PLAYBOOK = Playbook('docs/operations.md', '10.1')

#: How long an active job may hold its lock before its slot counts as leaked:
#: twelve hours, against the 4.2 hours the longest-busy queue stayed busy over
#: the 90 days to 2026-11-01, leaks aside. A leaked slot otherwise lasts days,
#: so the margin costs nothing.
MAX_JOB_AGE = dt.timedelta(hours=12)

#: The prefix BullMQ keeps immich's queues under in Redis.
QUEUE_PREFIX = 'immich_bull'

#: The first major release of postgres.js with the fix.
FIXED_POSTGRES_MAJOR = 4

#: Where the server's image keeps postgres.js's own `package.json`, which the
#: script reads the version from: under the server's `node_modules`, since the
#: package's `exports` map hides it from `require`.
POSTGRES_PACKAGE = '/usr/src/app/server/node_modules/postgres/package.json'

#: Where the script is mounted, and its name there.
_DIRECTORY = '/watchdog'
SCRIPT = 'watchdog.mjs'

#: What the watchdog's pod asks for and is held to: one Redis scan and a few
#: calls to the API.
_RESOURCES = k8s.core.v1.ResourceRequirementsArgs(
    requests={'cpu': '50m', 'memory': '128Mi'},
    limits={'cpu': '500m', 'memory': '256Mi'},
)


@dataclass(frozen=True)
class Watchdog:
    """What `job_slot_watchdog` emitted."""

    script: k8s.core.v1.ConfigMap
    account: k8s.core.v1.ServiceAccount
    role: k8s.rbac.v1.Role
    binding: k8s.rbac.v1.RoleBinding
    cronjob: k8s.batch.v1.CronJob
    rules: k8s.apiextensions.CustomResource
    """The scheduled-job family's rules over the CronJob."""


def watchdog_job(namespace: str) -> ScheduledJob:
    """The watchdog as the scheduled-job family reads it: its CronJob, its cadence, its deadlines and its playbook."""
    return ScheduledJob(
        alert='ImmichJobSlotWatchdog',
        namespace=namespace,
        cronjob=NAME,
        period=_PERIOD,
        deadline=_DEADLINE,
        starting_deadline=_STARTING_DEADLINE,
        playbook=PLAYBOOK,
    )


def job_slot_watchdog(
    resource_name: str,
    *,
    namespace: str,
    image: ImagePin,
    server_labels: Mapping[str, str],
    redis_host: str,
    redis_port: int,
    redis_password: k8s.core.v1.SecretKeySelectorArgs,
    user: int,
    affinity: k8s.core.v1.AffinityArgs,
    opts: pulumi.ResourceOptions | None = None,
) -> Watchdog:
    """The CronJob that restarts the server whose worker holds a slot it will never release, its role, and the rules over it.

    :param resource_name: The logical name, which carries the declaring
        component's name.
    :param namespace: The application's namespace; the caller's `opts`
        carries the dependency on the Namespace resource, since this is a
        plain name.
    :param image: The server's pin, which the watchdog runs in.
    :param server_labels: The labels of the server's pods, which it deletes.
    :param redis_host: The name the queues' Redis is reached at, and its port.
    :param redis_password: The Secret's key the Redis password is in.
    :param user: The user the pod runs as, the server's.
    :param affinity: Where the pod runs: the server's site, whose node holds
        its image.
    """
    script = k8s.core.v1.ConfigMap(
        f'{resource_name}-script',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
        data={SCRIPT: templates.load(__name__.rpartition('.')[0], f'templates/{SCRIPT}')},
        opts=opts,
    )
    account = k8s.core.v1.ServiceAccount(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
        # The token is mounted into the watchdog's pod alone, which names it.
        automount_service_account_token=False,
        opts=opts,
    )
    role = k8s.rbac.v1.Role(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
        rules=[k8s.rbac.v1.PolicyRuleArgs(api_groups=[''], resources=['pods'], verbs=['list', 'delete'])],
        opts=opts,
    )
    binding = k8s.rbac.v1.RoleBinding(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
        role_ref=k8s.rbac.v1.RoleRefArgs(api_group='rbac.authorization.k8s.io', kind='Role', name=NAME),
        subjects=[k8s.rbac.v1.SubjectArgs(kind='ServiceAccount', name=NAME, namespace=namespace)],
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=[role, account])),
    )
    selector = ','.join(f'{key}={value}' for key, value in server_labels.items())
    container = k8s.core.v1.ContainerArgs(
        name='watchdog',
        image=str(image),
        command=['node', f'{_DIRECTORY}/{SCRIPT}'],
        env=[
            k8s.core.v1.EnvVarArgs(
                name='NAMESPACE',
                value_from=k8s.core.v1.EnvVarSourceArgs(
                    field_ref=k8s.core.v1.ObjectFieldSelectorArgs(field_path='metadata.namespace')
                ),
            ),
            k8s.core.v1.EnvVarArgs(name='SERVER_SELECTOR', value=selector),
            k8s.core.v1.EnvVarArgs(name='REDIS_HOSTNAME', value=redis_host),
            k8s.core.v1.EnvVarArgs(name='REDIS_PORT', value=str(redis_port)),
            k8s.core.v1.EnvVarArgs(
                name='REDIS_PASSWORD', value_from=k8s.core.v1.EnvVarSourceArgs(secret_key_ref=redis_password)
            ),
            k8s.core.v1.EnvVarArgs(name='QUEUE_PREFIX', value=QUEUE_PREFIX),
            k8s.core.v1.EnvVarArgs(name='MAX_JOB_AGE_SECONDS', value=str(int(MAX_JOB_AGE.total_seconds()))),
            k8s.core.v1.EnvVarArgs(name='FIXED_POSTGRES_MAJOR', value=str(FIXED_POSTGRES_MAJOR)),
            k8s.core.v1.EnvVarArgs(name='POSTGRES_PACKAGE', value=POSTGRES_PACKAGE),
        ],
        resources=_RESOURCES,
        security_context=k8s.core.v1.SecurityContextArgs(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
        ),
        volume_mounts=[k8s.core.v1.VolumeMountArgs(name='script', mount_path=_DIRECTORY, read_only=True)],
    )
    pod = k8s.core.v1.PodSpecArgs(
        service_account_name=NAME,
        automount_service_account_token=True,
        enable_service_links=False,
        restart_policy='Never',
        affinity=affinity,
        security_context=k8s.core.v1.PodSecurityContextArgs(
            run_as_non_root=True,
            run_as_user=user,
            run_as_group=user,
            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
        ),
        containers=[container],
        volumes=[
            k8s.core.v1.VolumeArgs(name='script', config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=NAME)),
        ],
    )
    job = watchdog_job(namespace)
    starting = job.starting_deadline
    cronjob = k8s.batch.v1.CronJob(
        resource_name,
        metadata=k8s.meta.v1.ObjectMetaArgs(name=job.cronjob, namespace=namespace),
        spec=k8s.batch.v1.CronJobSpecArgs(
            schedule=SCHEDULE,
            concurrency_policy='Forbid',
            starting_deadline_seconds=int(starting.total_seconds()) if starting is not None else None,
            failed_jobs_history_limit=1,
            successful_jobs_history_limit=1,
            job_template=k8s.batch.v1.JobTemplateSpecArgs(
                spec=k8s.batch.v1.JobSpecArgs(
                    # A failed run is not retried: the failure is the report.
                    backoff_limit=0,
                    active_deadline_seconds=int(job.deadline.total_seconds()),
                    template=k8s.core.v1.PodTemplateSpecArgs(
                        metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(conventions.API_CLIENT_LABEL)),
                        spec=pod,
                    ),
                ),
            ),
        ),
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=[script, binding])),
    )
    rules = operator_object(
        f'{resource_name}-rules',
        kind='VMRule',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=job.cronjob, namespace=namespace),
        spec={'groups': [rule_group_spec(RuleGroup(job.cronjob, scheduled_job_rules(job)))]},
        opts=opts,
    )
    return Watchdog(script=script, account=account, role=role, binding=binding, cronjob=cronjob, rules=rules)
