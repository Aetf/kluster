"""Scheduled work: every CronJob a stack program declares, as a row of the scheduled-job rule family.

**Every CronJob a stack program declares is a row of `SCHEDULED_JOBS`**
(operations.md §4). A row holds what the component declaring the CronJob and
`components.monitoring.rules` share, so neither spells it for itself: the
component names the CronJob, places it and cuts each run off at the row's
deadline, and the rules derive the family's two alerts from the same row, both
`actionable` and both naming the row's playbook. *Stale* is the last success
older than `backup.max_age` of the period. *Failed* is a run scheduled after the
last success, held for the deadline, by which the run has either succeeded or
been cut off.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from kluster.conventions.alert import Playbook
from kluster.conventions.backup import max_age
from kluster.conventions.cluster import ETCD_BACKUP_NAMESPACE, MONITORING_NAMESPACE, STATE_UPKEEP_NAMESPACE

#: How long a change of a CronJob's status takes to reach the family's rules:
#: kube-state-metrics publishes it at the next scrape, and vmalert reads it at
#: the next evaluation after that. A deadline leaves it free, so a success at
#: the deadline is read before the stale alert would fire; a test holds the
#: scrape and the evaluation interval the monitoring component declares to it.
REPORT_LAG = dt.timedelta(minutes=1)


@dataclass(frozen=True, kw_only=True)
class ScheduledJob:
    """A CronJob that does the installation's own scheduled work, as the scheduled-job rule family reads it."""

    alert: str
    """The stem of its alerts' names, `<alert>Stale` and `<alert>Failed`.

    Jobs that one playbook answers share it: each alert carries its job's
    namespace and CronJob, which tell the two apart."""
    namespace: str
    cronjob: str
    """The CronJob's name, which its alerts select it by and name."""
    period: dt.timedelta
    """How often its schedule fires, the period `max_age` is derived from.

    Two spellings of one cadence, as a retention class's are: the component
    writes the cron line, and a test holds the one to the other."""
    deadline: dt.timedelta
    """How long a run may take before it is cut off, which the component
    sets as the Job's `activeDeadlineSeconds`; with `starting_deadline`,
    inside `slack`, and refused otherwise."""
    playbook: Playbook
    """Where the response to either alert is written."""
    starting_deadline: dt.timedelta | None = None
    """How late after its scheduled time a run may still start, which the
    component sets as the CronJob's `startingDeadlineSeconds`; a run the
    controller cannot start by then is skipped. `None` is a CronJob that sets
    none. A run that starts this late succeeds this much later, so the
    starting deadline and `deadline` together fit `slack`."""

    def __post_init__(self) -> None:
        if self.starting_deadline is not None and self.starting_deadline <= dt.timedelta(0):
            raise ValueError(
                f'the CronJob {self.cronjob!r} in {self.namespace!r} would skip every run, starting none '
                f'within {self.starting_deadline} of its time'
            )
        late = self.starting_deadline or dt.timedelta(0)
        if not dt.timedelta(0) < self.deadline <= self.slack - late:
            raise ValueError(
                f'the CronJob {self.cronjob!r} in {self.namespace!r} would cut a run off at {self.deadline} '
                f'after starting up to {late} late, outside the {self.slack} its period of {self.period} leaves: '
                'a run that succeeds that late would already be reported stale'
            )

    @property
    def slack(self) -> dt.timedelta:
        """The longest a run may take, from its scheduled time, under which a run that succeeds is never reported stale.

        The stale alert fires once the last success is `max_age` old. The next
        run is scheduled one period after the one that succeeded was, so at
        the latest one period after that success; it starts by its starting
        deadline after that, succeeds, if it does, by its deadline after the
        start, and its success reaches the rules `REPORT_LAG` later. So the
        slack is `max_age` of the period, less the period and the lag, and the
        starting deadline and the deadline share it. A deadline inside it also
        has the failed alert, held for the deadline, fire before the stale
        one.
        """
        return max_age(self.period) - self.period - REPORT_LAG


#: The metrics stores' backups, one CronJob per store, by the store's name
#: (`components.monitoring.stores`): hourly, each cut off at 25 minutes,
#: inside the slack.
METRICS_BACKUPS: Mapping[str, ScheduledJob] = MappingProxyType(
    {
        store: ScheduledJob(
            alert='MetricsBackup',
            namespace=MONITORING_NAMESPACE,
            cronjob=f'vmbackup-{store}',
            period=dt.timedelta(hours=1),
            deadline=dt.timedelta(minutes=25),
            playbook=Playbook('docs/operations.md', '6.2'),
        )
        for store in ('recent', 'history')
    }
)

#: The pull of OCI's block-volume series into the stores
#: (`components.monitoring.oci_metrics`), hourly and cut off as the backups
#: are.
OCI_METRICS_PULL = ScheduledJob(
    alert='OciMetricsPull',
    namespace=MONITORING_NAMESPACE,
    cronjob='oci-metrics-pull',
    period=dt.timedelta(hours=1),
    deadline=dt.timedelta(minutes=25),
    playbook=Playbook('docs/operations.md', '6.3'),
)

#: The hourly etcd snapshot (`components.backup.etcd`), the period storage.md
#: §5 gives its RPO. A run streams one snapshot and uploads it, so it is cut
#: off well inside the hour, and a run the controller could not start within
#: ten minutes is left to the next hour's.
ETCD_SNAPSHOT = ScheduledJob(
    alert='EtcdSnapshot',
    namespace=ETCD_BACKUP_NAMESPACE,
    cronjob='etcd-snapshot',
    period=dt.timedelta(hours=1),
    deadline=dt.timedelta(minutes=15),
    starting_deadline=dt.timedelta(minutes=10),
    playbook=Playbook('docs/operations.md', '7.1'),
)

#: The state table's daily upkeep (`components.state_upkeep`): the census,
#: the prune of the rows the retention rule expires and the ceiling, as the
#: role `upkeep` (physical/state-backend.md §2). A run is a handful of
#: statements, each cut off at `table.LISTING_TIMEOUT`, so it is cut off at a
#: quarter of an hour; a run the controller could not start within the hour
#: is left to the next day's.
STATE_UPKEEP = ScheduledJob(
    alert='StateUpkeep',
    namespace=STATE_UPKEEP_NAMESPACE,
    cronjob='state-upkeep',
    period=dt.timedelta(days=1),
    deadline=dt.timedelta(minutes=15),
    starting_deadline=dt.timedelta(hours=1),
    playbook=Playbook('docs/physical/state-backend.md', '7.6'),
)

#: The state backend's two checks from the cluster
#: (`components.monitoring.state_backend`): the age of its newest dump, and
#: the server certificate it serves. Hourly, each a CronJob of its own, so a
#: finding of one never hides a later finding of the other behind an alert
#: already firing; a run retries a dropped connection twice and is cut off at a
#: quarter of an hour. One playbook answers both: physical/state-backend.md §6
#: routes each finding, which the run's log names, to its own.
STATE_BACKEND_DUMPS = ScheduledJob(
    alert='StateBackendDumps',
    namespace=MONITORING_NAMESPACE,
    cronjob='state-backend-dumps',
    period=dt.timedelta(hours=1),
    deadline=dt.timedelta(minutes=15),
    playbook=Playbook('docs/physical/state-backend.md', '6'),
)
STATE_BACKEND_CERTIFICATE = ScheduledJob(
    alert='StateBackendCertificate',
    namespace=MONITORING_NAMESPACE,
    cronjob='state-backend-certificate',
    period=dt.timedelta(hours=1),
    deadline=dt.timedelta(minutes=15),
    playbook=Playbook('docs/physical/state-backend.md', '6'),
)

#: Every CronJob a stack program declares, as a row of the family:
#: `components.monitoring.rules` derives the family's alerts from each row,
#: and `tests/test_monitoring.py` holds every CronJob the programs declare to
#: a row of it, and every row to a CronJob some program declares.
SCHEDULED_JOBS: Sequence[ScheduledJob] = (
    *METRICS_BACKUPS.values(),
    OCI_METRICS_PULL,
    ETCD_SNAPSHOT,
    STATE_UPKEEP,
    STATE_BACKEND_DUMPS,
    STATE_BACKEND_CERTIFICATE,
)
