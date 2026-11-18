"""The alert rules the monitoring component declares, and what every one of them carries (rfc-007 §7.2).

**Every rule carries a tier, a summary and a playbook**: a `tier` label from
`conventions.alert.Tier`, and `summary` and `playbook` annotations, the fields
the dispatch intake's payload carries too, so a push reads the same whichever
producer raised it (cluster/architecture.md §4.3). Home Assistant's intake for
alertmanager reads them off each alert (operations.md §4). The types below are
what makes a rule without one impossible to write: `AlertRule` has no default
for any of the three, the tier is the census's enumeration, and a playbook is
a document and a section, which `tests/test_monitoring.py` resolves against
the documentation for every rule the program declares. That test is the gate
on the playbook rule -- an alert whose response procedure is not written down
is not shipped (architecture.md §4.3).

The expressions are MetricsQL, which vmalert evaluates: a duration such as
`36h` stands for its number of seconds wherever a number does, which is how
the freshness family compares an age with a retention class's threshold in the
duration syntax `conventions.backup` writes it in.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from kluster.components.monitoring import oci_metrics, stores
from kluster.conventions import MONITORING_NAMESPACE, backup
from kluster.conventions.alert import Tier

__all__ = (
    'BACKUP_SYNC_TIME',
    'CLASS_LABEL',
    'HEARTBEAT',
    'HEARTBEAT_REPEAT',
    'HEARTBEAT_TIMER',
    'METRICS_BACKUP_STALE',
    'OCI_METRICS_PULL_STALE',
    'RULE_GROUPS',
    'TIER_LABEL',
    'AlertRule',
    'Playbook',
    'RuleGroup',
)

#: The label an alert's tier travels under, and the receiver's routing key.
TIER_LABEL = 'tier'

#: How often alertmanager repeats the cluster's check-in to Home Assistant,
#: whose timer it restarts.
HEARTBEAT_REPEAT = dt.timedelta(minutes=10)

#: The duration of Home Assistant's timer for the cluster's check-in, whose
#: expiry pushes "no check-in from the cluster": the one rule for stale that
#: every check-in and every backup is held to (`conventions.backup.max_age`),
#: over the check-in's repeat. Home Assistant's side is configured by hand to
#: it (operations.md §4).
HEARTBEAT_TIMER = backup.max_age(HEARTBEAT_REPEAT)

#: The series a backup's freshness is read from: the time of each VolSync
#: source's last sync, `.status.lastSyncTime`, one series per source. Each
#: carries the source's `namespace` and `name`, which the rule keeps apart, and
#: its retention class in `CLASS_LABEL`; a series without the first two folds
#: every source of a class into one, stale only when the class's newest backup
#: is. kube-state-metrics publishes it from the source object, since VolSync's
#: own metrics carry neither the time nor the class (rfc-008 §4.2), and its
#: configuration for that, the producer of this name and these labels, is
#: rfc-008 §10 slice 24's. Until it runs the family evaluates over no series,
#: which is the state it is designed to start from (rfc-007 §7.2).
BACKUP_SYNC_TIME = 'kube_customresource_volsync_last_sync_time_seconds'

#: The label carrying a backup source's retention class on `BACKUP_SYNC_TIME`.
CLASS_LABEL = 'class'


@dataclass(frozen=True)
class Playbook:
    """Where an alert's response procedure is written: a document, from the repository's root, and a section."""

    document: str
    section: str

    def __str__(self) -> str:
        """The form the payload's `playbook` field takes (`conventions.alert.FIELDS`)."""
        return f'{self.document} §{self.section}'


@dataclass(frozen=True, kw_only=True)
class AlertRule:
    """One alerting rule, with the three fields every rule here carries and no default for any of them."""

    alert: str
    expr: str
    tier: Tier
    summary: str
    """One line, static: what Home Assistant puts after the tier and the alert's name."""
    playbook: Playbook
    pending: dt.timedelta | None = None
    """How long the expression holds before the alert fires; `None` fires on the first evaluation."""
    labels: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    """Labels beside the tier, which tell two alerts of one name apart."""


@dataclass(frozen=True)
class RuleGroup:
    """Rules vmalert evaluates together."""

    name: str
    rules: Sequence[AlertRule]


#: The cluster's check-in: a rule that always fires, which alertmanager repeats
#: to Home Assistant every `HEARTBEAT_REPEAT`. A cluster that stops evaluating
#: rules, or stops delivering them, stops restarting the timer, and its expiry
#: is the alert (rfc-007 §7.3).
HEARTBEAT = AlertRule(
    alert='ClusterCheckIn',
    expr='vector(1)',
    tier=Tier.HEARTBEAT,
    summary="The cluster's alerting is evaluating rules and delivering them",
    playbook=Playbook('docs/operations.md', '4'),
)


def _backup_jobs() -> str:
    """The matchers selecting the metrics stores' backup CronJobs."""
    return f'namespace="{MONITORING_NAMESPACE}", cronjob=~"{stores.BACKUP_JOB_PREFIX}.*"'


def _freshness(retention: backup.RetentionClass) -> AlertRule:
    """The backup-freshness rule for one retention class: a source whose last sync is older than the class allows."""
    return AlertRule(
        alert='BackupStale',
        expr=(
            f'time() - max by (namespace, name) ({BACKUP_SYNC_TIME}{{{CLASS_LABEL}="{retention.name}"}}) '
            f'> {retention.max_age}'
        ),
        tier=Tier.ACTIONABLE,
        summary='A backup is older than its retention class allows',
        playbook=Playbook('docs/cluster/storage.md', '5'),
        labels={CLASS_LABEL: retention.name},
    )


#: The metrics stores' backups going stale: a backup CronJob whose last
#: success, or its creation where it has not succeeded yet, is older than the
#: one rule for stale allows over its period. kube-state-metrics publishes both
#: times per CronJob, under the same labels, so the creation stands in only
#: for a job with no success on record, and a job that never succeeds is
#: reported as one that stopped succeeding is.
METRICS_BACKUP_STALE = AlertRule(
    alert='MetricsBackupStale',
    expr=(
        f'time() - max by (cronjob) ('
        f'kube_cronjob_status_last_successful_time{{{_backup_jobs()}}} or kube_cronjob_created{{{_backup_jobs()}}}'
        f') > {backup.duration(backup.max_age(stores.BACKUP_PERIOD))}'
    ),
    tier=Tier.ACTIONABLE,
    summary="A metrics store's backup is older than its period allows",
    playbook=Playbook('docs/operations.md', '6.2'),
)


def _pull_job() -> str:
    """The matchers selecting the pull of OCI's block-volume series."""
    return f'namespace="{MONITORING_NAMESPACE}", cronjob="{oci_metrics.PULL_JOB}"'


#: The pull of OCI's block-volume series going stale, read as the metrics
#: stores' backups are: its CronJob's last success, or its creation where it
#: has not succeeded yet, older than the one rule for stale allows over its
#: period. A run fails where OCI returned nothing of a volume the census
#: declares, so that is reported here too. Silent until the CronJob is
#: declared, which it is once the key is sealed in.
OCI_METRICS_PULL_STALE = AlertRule(
    alert='OciMetricsPullStale',
    expr=(
        f'time() - max by (cronjob) ('
        f'kube_cronjob_status_last_successful_time{{{_pull_job()}}} or kube_cronjob_created{{{_pull_job()}}}'
        f') > {backup.duration(backup.max_age(oci_metrics.PULL_PERIOD))}'
    ),
    tier=Tier.ACTIONABLE,
    summary="OCI's block-volume series have not been pulled within their period",
    playbook=Playbook('docs/operations.md', '6.3'),
)

#: Every rule the component declares, by group (rfc-007 §7.2).
RULE_GROUPS: Sequence[RuleGroup] = (
    RuleGroup(
        'alert-channel',
        (
            # A notification alertmanager failed to deliver, Home Assistant's
            # webhook among them (`notify/notify.go` L315-332 at alertmanager
            # v0.32.1). When it is that webhook which fails, this alert cannot
            # reach the phone either; the operations repository's poller is
            # the leg that carries it (architecture.md §4.3).
            AlertRule(
                alert='AlertNotificationsFailing',
                expr='increase(alertmanager_notifications_failed_total[15m]) > 0',
                tier=Tier.ACTIONABLE,
                summary='Alertmanager is failing to deliver notifications',
                playbook=Playbook('docs/cluster/architecture.md', '4.3'),
            ),
            HEARTBEAT,
        ),
    ),
    RuleGroup(
        'nodes',
        (
            AlertRule(
                alert='NodeNotReady',
                expr='kube_node_status_condition{condition="Ready",status="true"} == 0',
                tier=Tier.ACTIONABLE,
                summary='A node is not ready',
                playbook=Playbook('docs/operations.md', '3'),
                pending=dt.timedelta(minutes=10),
            ),
        ),
    ),
    RuleGroup('backup-freshness', tuple(_freshness(retention) for retention in backup.RETENTION_CLASSES)),
    RuleGroup(
        'metrics-store',
        (
            # A row a store accepted over the wire and did not keep, one
            # series per reason (`lib/timeserieslimits/timeseries_limits.go`
            # L52-63 and `app/vmstorage/main.go` L489-497 at VictoriaMetrics
            # v1.150.0). A series is lost whole, and nothing else reports it:
            # vmagent counts the row as delivered.
            AlertRule(
                alert='MetricsDropped',
                expr='increase(vm_rows_ignored_total[30m]) > 0',
                tier=Tier.ACTIONABLE,
                summary='The metrics store is ignoring rows it is sent',
                playbook=Playbook('docs/operations.md', '6.1'),
            ),
            METRICS_BACKUP_STALE,
            OCI_METRICS_PULL_STALE,
        ),
    ),
)
