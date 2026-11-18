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
from kluster.conventions.cluster import MONITORING_NAMESPACE

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
    sets as the Job's `activeDeadlineSeconds`; inside `slack`, and refused
    otherwise."""
    playbook: Playbook
    """Where the response to either alert is written."""

    def __post_init__(self) -> None:
        if not dt.timedelta(0) < self.deadline <= self.slack:
            raise ValueError(
                f'the CronJob {self.cronjob!r} in {self.namespace!r} would cut a run off at {self.deadline}, '
                f'outside the {self.slack} its period of {self.period} leaves: a run that succeeds that late '
                'would already be reported stale'
            )

    @property
    def slack(self) -> dt.timedelta:
        """The longest deadline under which a run that succeeds is never reported stale.

        The stale alert fires once the last success is `max_age` old. The next
        run starts one period after the one that succeeded started, so at the
        latest one period after that success; it succeeds, if it does, by its
        deadline after that, and its success reaches the rules `REPORT_LAG`
        later. So the slack is `max_age` of the period, less the period and
        the lag. A deadline inside it also has the failed alert, held for the
        deadline, fire before the stale one.
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

#: Every CronJob a stack program declares, as a row of the family:
#: `components.monitoring.rules` derives the family's alerts from each row,
#: and `tests/test_monitoring.py` holds every CronJob the programs declare to
#: a row of it, and every row to a CronJob some program declares.
SCHEDULED_JOBS: Sequence[ScheduledJob] = (*METRICS_BACKUPS.values(), OCI_METRICS_PULL)
