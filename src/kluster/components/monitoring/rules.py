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
duration syntax `conventions.backup` writes it in. Each is written in the
PromQL that `promql_parser` reads, too, since the scrape-target family reads
every expression through it, and it refuses MetricsQL's own syntax (`WITH`,
`default`, `if`, `limit`, `keep_metric_names`): a rule using that is refused
rather than declared.

**Every CronJob a stack program declares is watched by the scheduled-job
family**: each row of `conventions.schedule.SCHEDULED_JOBS` gives a *stale*
alert (`stale`) and a *failed* one (`failed`), read off kube-state-metrics'
series for that CronJob, both `actionable` and both naming the row's playbook
(operations.md §4).

**Every scrape job some alerting rule reads is watched by the scrape-target
family** (`rule_groups`): a rule that reads no series evaluates over none and
is silent, so a job that stops reporting raises `ScrapeTargetDown` for that
job instead. The set is read off the rules: the metric names each
expression selects (`selectors`, through GreptimeDB's `promql_parser`), mapped
to the jobs that export them
(`inputs`). A selector that names its `job` reads that job; one that does not
reads every `ScrapeJob` whose `exports` matches the name, and a name no job
exports is refused, so a rule cannot read a series whose job goes unwatched.
`ScraperDown` is the one alert for every job at once, when no target reports
at all, and while it holds the per-job alerts hold back
(operations.md §6.4).

**The kubeconfig copy's expiry is the one rule built from a value the
program reads** (`kubeconfig_copy`): `KubeconfigCopyExpiring` fires from
`conventions.cluster.KUBECONFIG_ALERT_MARGIN` before the client certificate
in `k8s-base`'s copy of the cluster-admin kubeconfig expires, the date the
program reads off that copy. It compares the evaluation's time with that
date and reads no series, so it has no scrape job to be watched by, and it
sits outside `ALERT_GROUPS`, whose rules are constants: the component
declares it in a rule object of its own. A cluster that stops evaluating it
is what the check-in reports (operations.md §4).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

import promql_parser

from kluster.conventions import backup, schedule
from kluster.conventions.alert import Playbook, Tier
from kluster.conventions.cluster import KUBECONFIG_ALERT_MARGIN
from kluster.conventions.schedule import ScheduledJob

__all__ = (
    'ALERT_GROUPS',
    'BACKUP_SYNC_TIME',
    'CLASS_LABEL',
    'HEARTBEAT',
    'HEARTBEAT_REPEAT',
    'HEARTBEAT_TIMER',
    'JOB_LABEL',
    'KUBECONFIG_COPY',
    'SCRAPER_DOWN',
    'TARGET_DOWN_PENDING',
    'TIER_LABEL',
    'AlertRule',
    'RuleGroup',
    'ScrapeJob',
    'Selector',
    'failed',
    'inputs',
    'kubeconfig_copy',
    'rule_groups',
    'scrape_targets',
    'selectors',
    'stale',
    'target_down',
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


#: The labels kube-state-metrics publishes a CronJob's series under, which a
#: job's alerts keep: they tell apart two jobs whose alerts share a name.
_JOB_LABELS = 'namespace, cronjob'


def _last_success(job: ScheduledJob) -> str:
    """The time of the job's last success, or of its creation where it has none, one series per CronJob.

    kube-state-metrics publishes both times per CronJob under the same labels,
    so the creation stands in only for a job with no success on record, and a
    job that never succeeds is reported as one that stopped succeeding is.
    """
    selector = f'namespace="{job.namespace}", cronjob="{job.cronjob}"'
    return (
        f'max by ({_JOB_LABELS}) ('
        f'kube_cronjob_status_last_successful_time{{{selector}}} or kube_cronjob_created{{{selector}}})'
    )


def stale(job: ScheduledJob) -> AlertRule:
    """The job's last success older than the one rule for stale allows over its period.

    It also reports a job that is no longer scheduled at all, suspended or
    with its controller stopped, since no success follows.
    """
    return AlertRule(
        alert=f'{job.alert}Stale',
        expr=f'time() - {_last_success(job)} > {backup.duration(backup.max_age(job.period))}',
        tier=Tier.ACTIONABLE,
        summary=f'{job.cronjob} has not succeeded for one and a half of its periods',
        playbook=job.playbook,
    )


def failed(job: ScheduledJob) -> AlertRule:
    """A run scheduled after the job's last success, held for the job's deadline.

    A run that succeeds moves the last success past its schedule inside the
    deadline, and one that does not has been cut off by then, so the alert
    reports a failed run within its deadline rather than after one and a half
    periods. It resolves when a later run succeeds.
    """
    return AlertRule(
        alert=f'{job.alert}Failed',
        expr=(
            f'max by ({_JOB_LABELS}) (kube_cronjob_status_last_schedule_time'
            f'{{namespace="{job.namespace}", cronjob="{job.cronjob}"}}) > {_last_success(job)}'
        ),
        tier=Tier.ACTIONABLE,
        summary=f"{job.cronjob}'s last run failed",
        playbook=job.playbook,
        pending=job.deadline,
    )


#: Every rule the component declares but the scrape-target family and the
#: kubeconfig copy's, by group (rfc-007 §7.2): the rules whose inputs that
#: family is derived from, so a new group of rules joins this tuple
#: (`rule_groups`).
ALERT_GROUPS: Sequence[RuleGroup] = (
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
        ),
    ),
    RuleGroup('scheduled-jobs', tuple(rule for job in schedule.SCHEDULED_JOBS for rule in (stale(job), failed(job)))),
)


# -- The kubeconfig copy ---------------------------------------------------------

#: The name of the kubeconfig copy's alert.
KUBECONFIG_COPY = 'KubeconfigCopyExpiring'


def kubeconfig_copy(expiry: dt.datetime) -> RuleGroup:
    """`KubeconfigCopyExpiring`, firing from `KUBECONFIG_ALERT_MARGIN` before `expiry` on, until a later one moves it.

    `expiry` is the `NotAfter` of the client certificate in `k8s-base`'s copy
    of the cluster-admin kubeconfig (`kluster.lib.k8s.client_certificate_expiry`).
    The threshold is a number of seconds since the epoch, compared with the
    evaluation's own time: `vector(time())`, since PromQL compares no two bare
    scalars without `bool`, which would yield a series at every evaluation.
    """
    if expiry.tzinfo is None:
        raise ValueError(f'{expiry} names no time zone, so it is no instant')
    threshold = expiry - KUBECONFIG_ALERT_MARGIN
    return RuleGroup(
        'kubeconfig-copy',
        (
            AlertRule(
                alert=KUBECONFIG_COPY,
                expr=f'vector(time()) > {int(threshold.timestamp())}',
                tier=Tier.ACTIONABLE,
                summary="The cluster-admin kubeconfig's copies are near their client certificate's expiry",
                playbook=Playbook('docs/operations.md', '2.5'),
            ),
        ),
    )


# -- The scrape-target family ------------------------------------------------------

#: The label vmagent gives every series of a scrape job, the job's name.
JOB_LABEL = 'job'


@dataclass(frozen=True)
class ScrapeJob:
    """A job vmagent scrapes: the `job` label its series carry, and the metric names a rule reads of it by name alone."""

    job: str
    exports: str
    """A regular expression matching, in full, the metric names this job
    exports that a selector naming no job reads. Every job a name matches is
    one the selector reads, so a name two jobs export is claimed by both."""


@dataclass(frozen=True)
class Selector:
    """One series selector of an expression: the metric it names, and the jobs its `job` matcher names, if it has one."""

    metric: str
    jobs: frozenset[str] | None


#: How long a job reports nothing before its alert fires: past a single failed
#: scrape and a restart, each a scrape interval or two. A target that leaves
#: service discovery is marked stale, so its job is absent at the next
#: evaluation and the alert fires this long after. A series that ends with no
#: stale marker -- every series when vmagent stops, since it writes none on
#: shutdown (`lib/promscrape/scrapework.go` L337-339) -- is read until its last
#: sample is one query step old: vmalert sends each instant query with `step`
#: set to `-datasource.queryStep`, five minutes at vmalert's default, which the
#: stack leaves unset, and an instant query reads a bare selector over that
#: step (`app/vmalert/datasource/init.go` L52, `app/vmselect/promql/rollup.go`
#: L719-755, at VictoriaMetrics v1.150.0). So `ScraperDown` fires about ten
#: minutes after vmagent stops. The jobs' last samples lie within one scrape
#: interval of one another, so its condition holds the per-job alerts back
#: long before any of them has waited this long. A test holds it above two
#: scrape intervals and an evaluation.
TARGET_DOWN_PENDING = dt.timedelta(minutes=5)

#: The scrape-target family's playbook.
_TARGETS_PLAYBOOK = Playbook('docs/operations.md', '6.4')

#: No target reports at all: vmagent is down, or none of what it scrapes
#: reaches the store. vmagent writes `up` for every target it scrapes, so a
#: store holding no `up` series is a scraper that stopped, whichever job is
#: watched.
SCRAPER_DOWN = AlertRule(
    alert='ScraperDown',
    expr='absent(up)',
    tier=Tier.ACTIONABLE,
    summary='No scrape target is reporting, so every alert that reads one is silent',
    playbook=_TARGETS_PLAYBOOK,
    pending=TARGET_DOWN_PENDING,
)


def target_down(job: str) -> AlertRule:
    """The job's targets report nothing: one whose scrape fails, or none listed at all, while some target reports.

    vmagent writes `up` as 0 for a target whose scrape fails, and stops writing
    it for a target that leaves service discovery, marking the series stale
    at once (`lib/promscrape/scrapework.go` L333-356 at VictoriaMetrics
    v1.150.0), so the first is `up == 0` and the second `absent`, and either
    alone misses the other. Neither holds while `ScraperDown` does: every job
    reports nothing then, and that one alert says so.
    """
    selector = f'up{{{JOB_LABEL}="{job}"}}'
    return AlertRule(
        alert='ScrapeTargetDown',
        expr=f'({selector} == 0 or absent({selector})) unless on() ({SCRAPER_DOWN.expr})',
        tier=Tier.ACTIONABLE,
        summary=f'{job} reports nothing, so the alerts that read it are silent',
        playbook=_TARGETS_PLAYBOOK,
        pending=TARGET_DOWN_PENDING,
        labels={JOB_LABEL: job},
    )


#: A `job` value the reader takes: a literal name, or for `=~` an alternation of
#: them. A Service name holds none of a regular expression's other
#: metacharacters, and the empty name selects series with no `job` at all.
_JOB_ALTERNATION = re.compile(r'[\w:-]+(?:\|[\w:-]+)*')


def _selector(expr: str, node: promql_parser.VectorSelector) -> Selector:
    """One parsed selector as the reader takes it, refusing one it could read only short."""
    if node.name is None:
        # A `__name__` matcher is among these: the parser takes one only on a
        # selector that names no metric otherwise.
        raise ValueError(f'{expr!r} selects series by their labels alone, which names no metric')
    if node.matchers.or_matchers:
        # `{a="x" or b="y"}` leaves `matchers` empty and puts each alternative
        # here, so reading `matchers` alone would read it as naming no job.
        raise ValueError(f'{expr!r} selects {node.name} by alternatives of matchers, which the reader does not follow')
    named: frozenset[str] | None = None
    for matcher in node.matchers.matchers:
        if matcher.name != JOB_LABEL:
            continue
        literal = matcher.op == promql_parser.MatchOp.Equal
        if (
            named is not None
            or not (literal or matcher.op == promql_parser.MatchOp.Re)
            or not _JOB_ALTERNATION.fullmatch(matcher.value)
            or (literal and '|' in matcher.value)
        ):
            raise ValueError(f'{expr!r}: {matcher} names no definite set of jobs')
        named = frozenset(matcher.value.split('|'))
    return Selector(node.name, named)


def selectors(expr: str) -> tuple[Selector, ...]:
    """Every series selector of a PromQL expression, in order, as `promql_parser` parses it.

    An expression the reader cannot follow is refused rather than read short:
    one the parser refuses, MetricsQL's own syntax among them; a selector by
    labels alone; a metric named by a `__name__` matcher; alternatives of
    matchers; and a `job` matcher that names no definite set of jobs.
    """
    try:
        tree = promql_parser.parse(expr)
    except ValueError as error:
        raise ValueError(f'{expr!r}: {error}') from error
    found: list[Selector] = []

    def visit(node: promql_parser.Expr) -> bool:
        if isinstance(node, promql_parser.VectorSelector):
            found.append(_selector(expr, node))
        return True

    _ = promql_parser.walk(tree, pre_visit=visit)
    return tuple(found)


def inputs(expr: str, jobs: Sequence[ScrapeJob]) -> frozenset[str]:
    """The jobs whose series an expression reads, refusing a metric it reads by name alone that no job exports."""
    read: set[str] = set()
    for selector in selectors(expr):
        if selector.jobs is not None:
            read |= selector.jobs
            continue
        exporters = {job.job for job in jobs if re.fullmatch(job.exports, selector.metric)}
        if not exporters:
            raise ValueError(
                f'{expr!r} reads {selector.metric!r}, which it selects by no {JOB_LABEL} and no scrape job exports'
            )
        read |= exporters
    return frozenset(read)


def scrape_targets(groups: Sequence[RuleGroup], jobs: Sequence[ScrapeJob]) -> RuleGroup:
    """The scrape-target family over `groups`: `ScraperDown`, and an alert for each job some rule of them reads."""
    read = {job for group in groups for rule in group.rules for job in inputs(rule.expr, jobs)}
    return RuleGroup('scrape-targets', (SCRAPER_DOWN, *(target_down(job) for job in sorted(read))))


def rule_groups(jobs: Sequence[ScrapeJob]) -> tuple[RuleGroup, ...]:
    """The component's rules but the kubeconfig copy's, by group: `ALERT_GROUPS`, and the scrape-target family over them.

    The family is derived from `ALERT_GROUPS` alone, so a group of rules joins
    `ALERT_GROUPS` rather than this tuple, or the jobs it reads go unwatched.
    The kubeconfig copy's rule reads no series, so it has no job to add.
    """
    return (*ALERT_GROUPS, scrape_targets(ALERT_GROUPS, jobs))
