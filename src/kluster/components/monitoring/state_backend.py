"""The state backend's two checks from the cluster: the ConfigMap carrying their program, and a CronJob for each.

**What each checks** is physical/state-backend.md §6's: `state-backend-dumps`
lists the dump prefix with the freshness key and holds the newest dump's age
to `settings.DUMP_MAX_AGE`, and `state-backend-certificate` reads the server
certificate off one handshake with the box and holds it to the alert margin
(`settings.EXPIRY_ALERT_MARGIN`) and the address. Each is a row of the
scheduled-job family (`conventions.schedule.STATE_BACKEND_DUMPS`,
`STATE_BACKEND_CERTIFICATE`), which gives the CronJob its name, its period and
the deadline a run is cut off at, and whose rules report a failed run and a
stale one, both into §6, which routes the finding the run's log names.

**The program is `state_backend_check`**, rendered into a ConfigMap
(`CHECKS_CONFIGMAP`) with the settings both checks read beside it (`settings`),
and run by the `oci-sdk` image: the interpreter, and the Debian base's
`openssl`, which the certificate check runs. The program imports the standard
library alone, so the image carries no code of this repository
(framework/ci.md §4), and the code and the settings reach the cluster in the
`up` of `k8s-base` that carries a change to them.

**Only the dumps pod holds a credential.** The freshness key
(`conventions.sealed.B2_FRESHNESS_DUMPS`) opens into a Secret that pod mounts
as a directory, a file per key, readable by its group alone, so no rendered
object and no process argument carries it. The certificate pod's spec is built
by a function that takes no key: the handshake needs none, since the box
sends its certificate before anything authenticates.

**Both retry a run twice.** Each crosses the internet, from the worker to the
appliance or to B2, and one dropped connection is no finding; a finding
repeats on the retry, and the Job fails after its third pod, inside the row's
deadline. Neither speaks to the Kubernetes API, so neither pod mounts a token.
"""

from __future__ import annotations

import json

import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.monitoring import memory
from kluster.conventions.schedule import STATE_BACKEND_CERTIFICATE, STATE_BACKEND_DUMPS, ScheduledJob
from kluster.lib import templates
from kluster.lib.state_backend import settings as backend
from kluster.lib.versions import ImagePin

__all__ = (
    'BACKOFF_LIMIT',
    'CHECKS_CONFIGMAP',
    'CHECK_USER',
    'KEY_DIR',
    'PROGRAM',
    'PROGRAM_DIR',
    'PROGRAM_FILE',
    'SETTINGS_FILE',
    'certificate_cronjob_spec',
    'dumps_cronjob_spec',
    'settings',
)

#: The minute of each hour each check runs at: apart from each other, and
#: from the stores' backups and the pull of OCI's series.
_DUMPS_MINUTE = 20
_CERTIFICATE_MINUTE = 50

#: The ConfigMap carrying the program and its settings, where each pod mounts
#: it, and the two files in it.
CHECKS_CONFIGMAP = 'state-backend-checks'
PROGRAM_DIR = '/opt/kluster/state-backend-checks'
PROGRAM_FILE = 'state_backend_check.py'
SETTINGS_FILE = 'settings.json'

#: Where the dumps pod mounts the Secret the freshness key opens into, a file per key.
KEY_DIR = '/etc/kluster/b2-freshness-dumps'

#: The program, as the ConfigMap carries it.
PROGRAM = templates.load('kluster.components.monitoring', PROGRAM_FILE)

#: The user and group each check runs as: `nobody`, which owns nothing in the
#: image. The key's files are readable by the group alone.
CHECK_USER = 65534
_KEY_MODE = 0o440
_PROGRAM_MODE = 0o444

#: How many times a Job runs its pod again after one fails: a dropped
#: connection is retried, and a finding fails every pod.
BACKOFF_LIMIT = 2


def settings() -> str:
    """The settings both checks read, as `state_backend_check.Settings.read` reads them."""
    key = conventions.sealed.B2_FRESHNESS_DUMPS
    return json.dumps(
        {
            'address': backend.ADDRESS,
            'port': backend.PORT,
            'alert_margin_seconds': int(backend.EXPIRY_ALERT_MARGIN.total_seconds()),
            'dump_prefix': f'{conventions.STATE_DUMP_PREFIX}/',
            'dump_max_age_seconds': int(backend.DUMP_MAX_AGE.total_seconds()),
            'key_secret': f'{key.namespace}/{key.name}',
        },
        indent=2,
        sort_keys=True,
    )


def dumps_cronjob_spec(*, image: ImagePin, affinity: k8s.core.v1.AffinityArgs) -> k8s.batch.v1.CronJobSpecArgs:
    """The dump-age check every hour, at its minute, under the freshness key."""
    return _cronjob_spec(STATE_BACKEND_DUMPS, minute=_DUMPS_MINUTE, pod=_dumps_pod(image, affinity))


def certificate_cronjob_spec(*, image: ImagePin, affinity: k8s.core.v1.AffinityArgs) -> k8s.batch.v1.CronJobSpecArgs:
    """The certificate check every hour, at its minute, with no credential."""
    return _cronjob_spec(STATE_BACKEND_CERTIFICATE, minute=_CERTIFICATE_MINUTE, pod=_certificate_pod(image, affinity))


def _cronjob_spec(row: ScheduledJob, *, minute: int, pod: k8s.core.v1.PodSpecArgs) -> k8s.batch.v1.CronJobSpecArgs:
    """A check hourly, its row's period, one run at a time, retried `BACKOFF_LIMIT` times and cut off at its row's deadline."""
    return k8s.batch.v1.CronJobSpecArgs(
        schedule=f'{minute} * * * *',
        concurrency_policy='Forbid',
        job_template=k8s.batch.v1.JobTemplateSpecArgs(
            spec=k8s.batch.v1.JobSpecArgs(
                backoff_limit=BACKOFF_LIMIT,
                active_deadline_seconds=int(row.deadline.total_seconds()),
                template=k8s.core.v1.PodTemplateSpecArgs(spec=pod),
            ),
        ),
    )


def _dumps_pod(image: ImagePin, affinity: k8s.core.v1.AffinityArgs) -> k8s.core.v1.PodSpecArgs:
    return _pod(
        image,
        affinity,
        name='dumps',
        args=['dumps', f'{PROGRAM_DIR}/{SETTINGS_FILE}', KEY_DIR],
        resources=memory.STATE_BACKEND_DUMPS.resources(),
        mounts=[k8s.core.v1.VolumeMountArgs(name='key', mount_path=KEY_DIR, read_only=True)],
        volumes=[
            k8s.core.v1.VolumeArgs(
                name='key',
                secret=k8s.core.v1.SecretVolumeSourceArgs(
                    secret_name=conventions.sealed.B2_FRESHNESS_DUMPS.name, default_mode=_KEY_MODE
                ),
            )
        ],
    )


def _certificate_pod(image: ImagePin, affinity: k8s.core.v1.AffinityArgs) -> k8s.core.v1.PodSpecArgs:
    return _pod(
        image,
        affinity,
        name='certificate',
        args=['certificate', f'{PROGRAM_DIR}/{SETTINGS_FILE}'],
        resources=memory.STATE_BACKEND_CERTIFICATE.resources(),
        mounts=[],
        volumes=[],
    )


def _pod(
    image: ImagePin,
    affinity: k8s.core.v1.AffinityArgs,
    *,
    name: str,
    args: list[str],
    resources: dict[str, dict[str, str]],
    mounts: list[k8s.core.v1.VolumeMountArgs],
    volumes: list[k8s.core.v1.VolumeArgs],
) -> k8s.core.v1.PodSpecArgs:
    """A check's pod: `CHECK_USER` on a read-only root, with no capability and no token, the program mounted beside what it reads."""
    return k8s.core.v1.PodSpecArgs(
        restart_policy='Never',
        # It speaks to the box and to B2, and never to Kubernetes.
        automount_service_account_token=False,
        affinity=affinity,
        security_context=k8s.core.v1.PodSecurityContextArgs(
            run_as_non_root=True,
            run_as_user=CHECK_USER,
            run_as_group=CHECK_USER,
            fs_group=CHECK_USER,
            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
        ),
        containers=[
            k8s.core.v1.ContainerArgs(
                name=name,
                image=str(image),
                # Isolated: the interpreter reads no environment variable and
                # no user site, and writes no bytecode to the read-only root.
                command=['python', '-I', '-B', f'{PROGRAM_DIR}/{PROGRAM_FILE}', *args],
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
                    *mounts,
                ],
            )
        ],
        volumes=[
            k8s.core.v1.VolumeArgs(
                name='program',
                config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=CHECKS_CONFIGMAP, default_mode=_PROGRAM_MODE),
            ),
            *volumes,
        ],
    )
