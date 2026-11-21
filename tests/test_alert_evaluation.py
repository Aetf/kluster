"""The cluster's alerting rules, evaluated by vmalert-tool over synthesized series.

`tests/test_monitoring.py` holds each rule to the fields it carries and to the
rows it is derived from; this module holds what each expression does. The
rule groups are the `VMRule` the `k8s-base` program declares against mocks,
written out as vmalert reads them, and `vmalert-tool unittest` -- vmalert's
rule engine, from the pinned `vmutils` archive, which is held to the release
the stack chart runs vmalert at -- evaluates each group over series the cases
synthesize, at the evaluation interval the component gives vmalert and over
series sampled at the scrape interval it gives vmagent, but for the
backup-freshness family, whose cases run for days (`BACKUP_CLOCK`).

A check states which alerts of one name are firing at one time. Every alert it
expects carries the declared rule's labels and annotations, which vmalert-tool
compares whole and `tests/test_monitoring.py` holds; what a case holds is the
labels its expression keeps from the series. Every rule of a group fires in
some check, and every alert name but the check-in's, which fires always, is
silent in some check.

Where a family is derived from rows, its cases are derived from the same rows:
a CronJob's series for each row of `conventions.schedule.SCHEDULED_JOBS`, a
backup source for each retention class, and a target for each job the
scrape-target family watches, read off its declared rules. Times inside a case
are the row's own: its period, its deadline and its `max_age`.

The kubeconfig copy's rule is built from the date the program reads off the
copy in its configuration, so the program is declared here with a copy whose
client certificate expires `KUBECONFIG_ALERT_MARGIN` and a few minutes after
`T0` (`COPY_NOT_AFTER`), which puts the rule's threshold inside a case of
minutes at the intervals every other family runs at.

Series are written in vmalert-tool's notation, one value per step of the case's
interval: `_` is a step with no sample, `_xN` is N of them, `stale` is a
staleness marker, and `a+0xN` is `a` held for N steps after the first. A value
naming a time is offset by `T0`, where vmalert-tool's clock starts.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import k8s_base_installation
import pinned_tools
import process_sessions
import promql_parser
import pytest
import pytest_asyncio
import yaml
from client_kubeconfig import certificate_user, kubeconfig
from k8s_base_installation import Run, applied

from kluster import conventions
from kluster.components.monitoring import EVALUATION_INTERVAL
from kluster.components.monitoring.operator_objects import type_token
from kluster.components.monitoring.rules import (
    BACKUP_SYNC_TIME,
    CLASS_LABEL,
    HEARTBEAT,
    KUBECONFIG_COPY,
    SCRAPER_DOWN,
    TARGET_DOWN_PENDING,
    TIER_LABEL,
    target_down,
)
from kluster.conventions import backup, schedule
from kluster.conventions.alert import Tier
from kluster.conventions.cluster import KUBECONFIG_ALERT_MARGIN
from kluster.conventions.outputs import BOOT_VOLUME
from kluster.conventions.providers import OCI_BLOCKSTORE_METRICS
from kluster.conventions.schedule import ScheduledJob
from kluster.lib.monitoring import readings
from kluster.lib.monitoring.jobs import (
    APISERVER_JOB,
    CILIUM_AGENT_JOB,
    ETCD_JOB,
    KUBE_STATE_METRICS_JOB,
    KUBELET_JOB,
    NODE_EXPORTER_JOB,
    SCRAPE_INTERVAL,
)
from kluster.lib.monitoring.selectors import JOB_LABEL
from kluster.lib.versions import CHART
from kluster.scripts.update_crds import record, sources

VM_RULE = type_token('VMRule')

#: The tool, as `mise.toml` puts it on `PATH` from the `vmutils` archive.
VMALERT_TOOL = 'vmalert-tool'

#: The `mise.toml` pin of the `vmutils` archive, by the key mise's GitHub
#: backend names it under.
VMUTILS = 'github:VictoriaMetrics/VictoriaMetrics'

#: The stack chart's pin, whose recorded `appVersion` is the release vmalert runs at.
STACK_CHART = 'victoria-metrics-k8s-stack'

ROOT = Path(__file__).resolve().parents[1]

#: The bound of each `vmalert-tool` run. The slowest run, one of a daily
#: scheduled job's cases, which runs for five days at the evaluation interval,
#: took about 55 s on a lightly loaded 24-core machine; this is over five
#: times that. A family whose cases together would run near it is split into
#: runs (`Family.parts`) rather than the bound raised. A stop-loss, which
#: nothing asserts on.
COMMAND_TIMEOUT = 300

#: The case bound, above the one run a case makes and the program's run under
#: mocks, which the first case on a worker sets up and which took about 10 s
#: on the same machine. A stop-loss, which nothing asserts on.
CASE_TIMEOUT = COMMAND_TIMEOUT + 120

#: The step of the backup-freshness family's series and of its evaluation,
#: whose cases run for one and a half of a retention class's period, days
#: long. Its rules hold no `for`, so evaluating them less often delays an
#: alert by at most one step, and each check lies two steps from the time its
#: alert changes; the sync times its series carry fall on these steps.
BACKUP_CLOCK = dt.timedelta(minutes=5)

#: The time vmalert-tool's clock starts at, 2000-01-01T00:00:00Z: `time()` at a
#: case's start, and the offset of every sample value that names a time.
T0 = 946684800

#: When the client certificate in the copy the program is declared with
#: expires: the alert's margin and ten minutes after `T0`, so the kubeconfig
#: copy's threshold lies ten minutes into its cases.
COPY_NOT_AFTER = dt.datetime.fromtimestamp(T0, dt.UTC) + KUBECONFIG_ALERT_MARGIN + dt.timedelta(minutes=10)

#: A staleness marker: the sample a scraper writes when a target leaves
#: service discovery, after which the series has no sample.
STALE = 'stale'

#: The scrape-target family's per-job alert, by name.
TARGET_DOWN = target_down('a-job').alert

#: The labels every series kube-state-metrics publishes carries beside its own.
KSM = {JOB_LABEL: KUBE_STATE_METRICS_JOB, 'instance': '10.0.0.1:8080'}

#: How long after its scheduled time each run of a healthy job succeeds.
RUN = dt.timedelta(minutes=3)

#: How far to either side of a threshold a check looks: past an evaluation
#: interval and a scrape interval both, so a check never lands on the step at
#: which an alert changes.
MARGIN = dt.timedelta(minutes=1)

type Point = float | Literal['stale'] | None
type Labels = Mapping[str, str]
type Rule = Mapping[str, Any]


def _minutes(minutes: float) -> dt.timedelta:
    return dt.timedelta(minutes=minutes)


def _pending(rule: Rule) -> dt.timedelta:
    """A declared rule's `for`, which the program writes as one count of one unit (`conventions.backup.duration`).

    Zero where the rule declares none.
    """
    if 'for' not in rule:
        return dt.timedelta(0)
    text: str = rule['for']
    return dict(backup.DURATION_UNITS)[text[-1]] * int(text[:-1])


def _equalities(expr: str) -> frozenset[tuple[str, str]]:
    """Every `label="value"` matcher of every selector in a PromQL expression."""
    found: set[tuple[str, str]] = set()

    def visit(node: promql_parser.Expr) -> bool:
        if isinstance(node, promql_parser.VectorSelector):
            found.update(
                (matcher.name, matcher.value)
                for matcher in node.matchers.matchers
                if matcher.op == promql_parser.MatchOp.Equal
            )
        return True

    _ = promql_parser.walk(promql_parser.parse(expr), pre_visit=visit)
    return frozenset(found)


@dataclass(frozen=True)
class Group:
    """One rule group as the program declares it."""

    name: str
    rules: Sequence[Rule]

    def rule(self, alert: str, **selected: str) -> Rule:
        """The one declared alert named `alert` that carries or selects each label of `selected` at its value."""
        found = [
            rule
            for rule in self.rules
            if rule.get('alert') == alert
            and set(selected.items()) <= set(rule['labels'].items()) | _equalities(rule['expr'])
        ]
        assert len(found) == 1, f'{self.name} declares {len(found)} rules named {alert} with {selected}'
        return found[0]

    def record(self, name: str) -> str:
        """The series the one declared recording rule whose record is named `name` writes, under any window."""
        found = [rule['record'] for rule in self.rules if 'record' in rule and rule['record'].split(':')[1] == name]
        assert len(found) == 1, f'{self.name} declares {len(found)} records named {name}'
        return found[0]

    @property
    def alerts(self) -> frozenset[str]:
        return frozenset(rule['alert'] for rule in self.rules if 'alert' in rule)

    @property
    def records(self) -> frozenset[str]:
        return frozenset(rule['record'] for rule in self.rules if 'record' in rule)


@dataclass(frozen=True)
class Alert:
    """An alert a check expects firing: the declared rule that raises it, and the labels it keeps from the series."""

    rule: Rule
    kept: Labels = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class Check:
    """Every alert named `alert` that is firing at `at`; none at all where `firing` is empty."""

    at: dt.timedelta
    alert: str
    firing: Sequence[Alert] = ()


@dataclass(frozen=True)
class Sample:
    """The series a recording rule writes, at `at`: one value per label set, and none at all where `values` is empty."""

    at: dt.timedelta
    record: str
    values: Sequence[tuple[Labels, float]] = ()


@dataclass(frozen=True)
class Series:
    """One input series: its metric, its labels and its values in vmalert-tool's notation."""

    metric: str
    labels: Labels
    values: str


@dataclass(frozen=True)
class Case:
    """A set of series and what the group's alerts do over them."""

    name: str
    series: Sequence[Series]
    checks: Sequence[Check] = ()
    interval: dt.timedelta = SCRAPE_INTERVAL
    samples: Sequence[Sample] = ()
    part: str = ''
    """The `vmalert-tool` run of its family the case is evaluated in, one of `Family.parts`."""


def _number(value: float) -> str:
    return str(int(value)) if value == int(value) else repr(value)


def values(points: Mapping[dt.timedelta, Point], until: dt.timedelta, interval: dt.timedelta) -> str:
    """A series' values from `points`: each held from its time until the next, none before the first.

    A `None` point ends the series there, with no marker; a `STALE` point
    writes one marker at its step and leaves the series with no sample after it.
    """
    at: dict[int, Point] = {}
    for time, point in points.items():
        assert time % interval == dt.timedelta(0), f'{time} is not a step of {interval}'
        at[time // interval] = point
    runs: list[tuple[Point, int]] = []
    current: Point = None
    for step in range(until // interval + 1):
        current = at.get(step, current)
        if runs and runs[-1][0] == current and current != STALE:
            runs[-1] = (current, runs[-1][1] + 1)
        else:
            runs.append((current, 1))
        if current == STALE:
            current = None
    words: list[str] = []
    for point, count in runs:
        if point is None:
            words.append('_' if count == 1 else f'_x{count}')
        elif point == STALE:
            words.append(STALE)
        else:
            words.append(f'{_number(point)}+0x{count - 1}')
    return ' '.join(words)


def counter(
    metric: str,
    labels: Labels,
    slopes: Mapping[dt.timedelta, float],
    until: dt.timedelta,
    interval: dt.timedelta = SCRAPE_INTERVAL,
) -> Series:
    """A counter that starts at 0 with the case and grows by each slope a step from its time on, 0 before the first."""
    words: list[str] = []
    value = 0.0
    slope = 0.0
    run: tuple[float, float, int] | None = None
    for step in range(until // interval + 1):
        at = interval * step
        slope = slopes.get(at, slope)
        if step:
            value += slope
        if run is not None and run[1] == slope:
            run = (run[0], slope, run[2] + 1)
            continue
        if run is not None:
            words.append(f'{_number(run[0])}+{_number(run[1])}x{run[2] - 1}')
        run = (value, slope, 1)
    assert run is not None
    words.append(f'{_number(run[0])}+{_number(run[1])}x{run[2] - 1}')
    return Series(metric, labels, ' '.join(words))


def series(
    metric: str,
    labels: Labels,
    points: Mapping[dt.timedelta, Point],
    until: dt.timedelta,
    interval: dt.timedelta = SCRAPE_INTERVAL,
) -> Series:
    return Series(metric, labels, values(points, until, interval))


def timestamp(at: dt.timedelta) -> float:
    """A sample value naming the time `at` after the case's start."""
    return T0 + at.total_seconds()


def _test(case: Case, group: str) -> dict[str, object]:
    """One case as vmalert-tool's test file spells it."""

    def labels(selector: Series) -> str:
        return ', '.join(f'{name}="{value}"' for name, value in selector.labels.items())

    def expected(check: Check, alert: Alert) -> dict[str, object]:
        assert alert.rule['alert'] == check.alert, f'{case.name}: a {alert.rule["alert"]} under {check.alert}'
        return {
            'exp_labels': {**alert.kept, **alert.rule['labels'], 'alertname': check.alert, 'alertgroup': group},
            'exp_annotations': dict(alert.rule['annotations']),
        }

    def sample(record: str, at: Labels, value: float) -> dict[str, object]:
        spelled = ', '.join(f'{name}="{label}"' for name, label in at.items())
        return {'labels': f'{record}{{{spelled}}}', 'value': value}

    return {
        'name': case.name,
        'interval': backup.duration(case.interval),
        'input_series': [{'series': f'{one.metric}{{{labels(one)}}}', 'values': one.values} for one in case.series],
        'metricsql_expr_test': [
            {
                'expr': one.record,
                'eval_time': backup.duration(one.at),
                'exp_samples': [sample(one.record, at, value) for at, value in one.values],
            }
            for one in case.samples
        ],
        'alert_rule_test': [
            {
                'eval_time': backup.duration(check.at),
                'groupname': group,
                'alertname': check.alert,
                'exp_alerts': [expected(check, alert) for alert in check.firing],
            }
            for check in case.checks
        ],
    }


# -- alert-channel ------------------------------------------------------------------


def alert_channel(group: Group) -> Iterator[Case]:
    """Alertmanager's failed deliveries, inside the rule's window and after it, and the check-in, firing always."""
    failing = group.rule('AlertNotificationsFailing')
    check_in = Alert(group.rule(HEARTBEAT.alert))
    metric = 'alertmanager_notifications_failed_total'
    labels = {'integration': 'webhook', JOB_LABEL: 'vmalertmanager', 'instance': '10.0.0.3:9093'}
    until = _minutes(40)
    yield Case(
        'no delivery fails',
        [series(metric, labels, {dt.timedelta(0): 0}, until)],
        [Check(_minutes(20), failing['alert']), Check(_minutes(20), HEARTBEAT.alert, [check_in])],
    )
    fails = _minutes(10)
    window = _minutes(15)
    yield Case(
        'a delivery fails',
        [series(metric, labels, {dt.timedelta(0): 0, fails: 1}, until)],
        [
            Check(fails - MARGIN, failing['alert']),
            Check(fails + MARGIN, failing['alert'], [Alert(failing, labels)]),
            Check(fails + window - MARGIN, failing['alert'], [Alert(failing, labels)]),
            Check(fails + window + MARGIN, failing['alert']),
        ],
    )
    yield Case('no series at all', [], [Check(dt.timedelta(0), HEARTBEAT.alert, [check_in])])


# -- nodes --------------------------------------------------------------------------


def _node(node: str, ready: Mapping[dt.timedelta, bool], until: dt.timedelta) -> list[Series]:
    """kube-state-metrics' Ready condition of `node`, one series per status, each 1 where it holds."""
    statuses = {
        'true': {time: float(state) for time, state in ready.items()},
        'false': {time: float(not state) for time, state in ready.items()},
        'unknown': dict.fromkeys(ready, 0.0),
    }
    return [
        series(
            'kube_node_status_condition',
            {'node': node, 'condition': 'Ready', 'status': status, **KSM},
            points,
            until,
        )
        for status, points in statuses.items()
    ]


def nodes(group: Group) -> Iterator[Case]:
    """A node that stops being ready, for longer than the rule's pending and for less, and for two scrapes.

    The last is timed by the program's scrape and evaluation intervals rather
    than by the rule, so it holds the floor of whatever `for` the rule declares.
    """
    rule = group.rule('NodeNotReady')
    pending = _pending(rule)
    until = _minutes(40)
    start = dt.timedelta(0)
    ready = _node('node-b', {start: True}, until)
    yield Case(
        'every node ready', [*_node('node-a', {start: True}, until), *ready], [Check(_minutes(30), rule['alert'])]
    )
    down = _minutes(5)
    yield Case(
        'a node stops being ready',
        [*_node('node-a', {start: True, down: False}, until), *ready],
        [
            Check(down + pending - MARGIN, rule['alert']),
            Check(
                down + pending + MARGIN,
                rule['alert'],
                [Alert(rule, {'node': 'node-a', 'condition': 'Ready', 'status': 'true', **KSM})],
            ),
        ],
    )
    yield Case(
        'a node not ready for less than the pending',
        [*_node('node-a', {start: True, down: False, down + pending - MARGIN: True}, until), *ready],
        [Check(down + pending - 2 * MARGIN, rule['alert']), Check(down + pending + MARGIN, rule['alert'])],
    )
    # A Ready status wrong for two scrapes is read for at most two scrapes and
    # an evaluation, which no `for` worth declaring fires inside.
    blip = 2 * SCRAPE_INTERVAL
    yield Case(
        'a node not ready for two scrapes',
        [*_node('node-a', {start: True, down: False, down + blip: True}, until), *ready],
        [
            Check(down + EVALUATION_INTERVAL * k, rule['alert'])
            for k in range((blip + EVALUATION_INTERVAL) // EVALUATION_INTERVAL + 1)
        ],
    )


# -- backup-freshness ---------------------------------------------------------------


def _source(name: str, retention: backup.RetentionClass, syncs: Sequence[dt.timedelta], until: dt.timedelta) -> Series:
    """A backup source of the class, in the namespace `app`, that synced at each of `syncs`."""
    labels = {'namespace': 'app', 'name': name, CLASS_LABEL: retention.name, **KSM}
    return series(BACKUP_SYNC_TIME, labels, {at: timestamp(at) for at in syncs}, until, BACKUP_CLOCK)


def _every(period: dt.timedelta, until: dt.timedelta) -> list[dt.timedelta]:
    return [period * k for k in range(until // period + 1)]


def backup_freshness(group: Group) -> Iterator[Case]:
    """For each retention class, a source that stops syncing beside one that keeps syncing.

    Every class's sources are written in every case, the classes apart from
    the case's own syncing throughout, so a rule that read another class's
    sources would fire on them.
    """
    for retention in backup.RETENTION_CLASSES:
        rule = group.rule('BackupStale', **{CLASS_LABEL: retention.name})
        age = backup.max_age(retention.period)
        until = age + 2 * BACKUP_CLOCK
        others = [
            _source(f'{other.name}-data', other, _every(other.period / 2, until), until)
            for other in backup.RETENTION_CLASSES
            if other != retention
        ]
        yield Case(
            f'{retention.name}: one source stops syncing, another keeps on',
            [
                _source('stopped', retention, [dt.timedelta(0)], until),
                _source('syncing', retention, _every(retention.period / 2, until), until),
                *others,
            ],
            [
                Check(age - 2 * BACKUP_CLOCK, rule['alert']),
                Check(age + 2 * BACKUP_CLOCK, rule['alert'], [Alert(rule, {'namespace': 'app', 'name': 'stopped'})]),
            ],
            BACKUP_CLOCK,
        )


# -- metrics-store ------------------------------------------------------------------


def metrics_store(group: Group) -> Iterator[Case]:
    """A store's count of ignored rows, steady and rising, inside the rule's window and after it."""
    rule = group.rule('MetricsDropped')
    metric = 'vm_rows_ignored_total'
    labels = {'reason': 'too_many_labels', JOB_LABEL: 'vmsingle-history', 'instance': '10.0.0.5:8428'}
    # A case's start is the series' first sample, which `increase` reads as a
    # rise from nothing, so each check looks at a window that starts after it.
    window = _minutes(30)
    ignored = window + _minutes(10)
    until = ignored + window + _minutes(10)
    yield Case(
        'the count holds steady',
        [series(metric, labels, {dt.timedelta(0): 5}, until)],
        [Check(ignored, rule['alert']), Check(until, rule['alert'])],
    )
    yield Case(
        'a row ignored',
        [series(metric, labels, {dt.timedelta(0): 5, ignored: 6}, until)],
        [
            Check(ignored - MARGIN, rule['alert']),
            Check(ignored + MARGIN, rule['alert'], [Alert(rule, labels)]),
            Check(ignored + window - MARGIN, rule['alert'], [Alert(rule, labels)]),
            Check(ignored + window + MARGIN, rule['alert']),
        ],
    )


# -- scheduled-jobs -----------------------------------------------------------------


def _cronjob(
    job: ScheduledJob,
    until: dt.timedelta,
    *,
    scheduled: Sequence[dt.timedelta] = (),
    succeeded: Sequence[dt.timedelta] = (),
) -> list[Series]:
    """kube-state-metrics' series for the row's CronJob, created at the case's start.

    Each scheduled run and each success is published at the time it names.
    """
    labels = {'namespace': job.namespace, 'cronjob': job.cronjob, **KSM}

    def times(at: Sequence[dt.timedelta]) -> dict[dt.timedelta, Point]:
        return {time: timestamp(time) for time in at}

    out = [series('kube_cronjob_created', labels, {dt.timedelta(0): timestamp(dt.timedelta(0))}, until)]
    if scheduled:
        out.append(series('kube_cronjob_status_last_schedule_time', labels, times(scheduled), until))
    if succeeded:
        out.append(series('kube_cronjob_status_last_successful_time', labels, times(succeeded), until))
    return out


def scheduled_jobs(group: Group) -> Iterator[Case]:
    """Each row's CronJob healthy, never scheduled, failing its first run, succeeding late, and failing after success.

    The last case fails the row's first run beside every other row's CronJob
    running healthily, which tells the alerts of rows that share a name apart.
    Each case is a run of its own (`SCHEDULED_JOB_PARTS`): a daily row's cases
    run for days at the evaluation interval, so the family's cases in one run
    would take a share of the run's bound that grows with every row.
    """

    def alerts(job: ScheduledJob) -> tuple[Rule, Rule]:
        selected = {'namespace': job.namespace, 'cronjob': job.cronjob}
        return group.rule(f'{job.alert}Stale', **selected), group.rule(f'{job.alert}Failed', **selected)

    def fire(rule: Rule, job: ScheduledJob) -> list[Alert]:
        return [Alert(rule, {'namespace': job.namespace, 'cronjob': job.cronjob})]

    def healthy(job: ScheduledJob, until: dt.timedelta) -> list[Series]:
        runs = [job.period * k for k in range(1, until // job.period + 1)]
        return _cronjob(job, until, scheduled=runs, succeeded=[run + RUN for run in runs])

    for job in schedule.SCHEDULED_JOBS:
        stale, failed = alerts(job)
        period, deadline, age = job.period, job.deadline, backup.max_age(job.period)
        name = f'{job.namespace}/{job.cronjob}'

        def of_row(kind: str, inputs: Sequence[Series], checks: Sequence[Check], row: str = name) -> Case:
            return Case(f'{row}: {kind}', inputs, checks, part=f'{row}: {kind}')

        def quiet(*at: dt.timedelta, stale: Rule = stale, failed: Rule = failed) -> list[Check]:
            return [Check(time, rule['alert']) for time in at for rule in (stale, failed)]

        until = period * 5
        yield of_row(
            'every run succeeds',
            healthy(job, until),
            quiet(period / 2, period + deadline + MARGIN, age + MARGIN, period * 2 + deadline + MARGIN),
        )
        yield of_row(
            'never scheduled',
            _cronjob(job, until),
            [
                *quiet(age - MARGIN),
                Check(age + MARGIN, stale['alert'], fire(stale, job)),
                Check(age + MARGIN, failed['alert']),
            ],
        )
        yield of_row(
            'the first run fails',
            _cronjob(job, until, scheduled=[period]),
            [
                *quiet(period + deadline - MARGIN),
                Check(period + deadline + MARGIN, failed['alert'], fire(failed, job)),
                Check(age + MARGIN, stale['alert'], fire(stale, job)),
                Check(age + MARGIN, failed['alert'], fire(failed, job)),
            ],
        )
        yield of_row(
            'the first run succeeds just inside its deadline',
            _cronjob(job, until, scheduled=[period], succeeded=[period + deadline - MARGIN]),
            quiet(period + deadline - 2 * MARGIN, period + deadline + MARGIN, age + MARGIN),
        )
        recovery = period * 3 + RUN
        yield of_row(
            'a run fails after one succeeds, and the next succeeds',
            _cronjob(job, until, scheduled=[period, period * 2, period * 3], succeeded=[period + RUN, recovery]),
            [
                *quiet(period * 2 + deadline - MARGIN),
                Check(period * 2 + deadline + MARGIN, failed['alert'], fire(failed, job)),
                Check(period + RUN + age - MARGIN, stale['alert']),
                Check(period + RUN + age + MARGIN, stale['alert'], fire(stale, job)),
                Check(recovery - MARGIN, failed['alert'], fire(failed, job)),
                *quiet(recovery + MARGIN),
            ],
        )
        failing = period + deadline + MARGIN
        until = failing + MARGIN
        others = [other for other in schedule.SCHEDULED_JOBS if other != job]
        names = sorted({rule['alert'] for other in schedule.SCHEDULED_JOBS for rule in alerts(other)})
        yield of_row(
            'the first run fails, every other CronJob healthy',
            [*_cronjob(job, until, scheduled=[period]), *(one for other in others for one in healthy(other, until))],
            [Check(failing, alert, fire(failed, job) if alert == failed['alert'] else []) for alert in names],
        )


# -- scrape-targets -----------------------------------------------------------------


def scrape_targets(group: Group) -> Iterator[Case]:
    """Each watched job's scrape failing, leaving discovery and never reporting; vmagent stopping; and restarts.

    The jobs are the ones the declared family watches, read off its rules, and
    each has a target of its own. The cases that need one job of them take the
    first. Both alerts are timed by `TARGET_DOWN_PENDING`, which each rule
    of the family is declared to wait out.
    """
    scraper = group.rule(SCRAPER_DOWN.alert)
    jobs = sorted(rule['labels'][JOB_LABEL] for rule in group.rules if rule['alert'] == TARGET_DOWN)
    instances = {job: f'10.0.0.{index}:8080' for index, job in enumerate(jobs, start=1)}
    pending = TARGET_DOWN_PENDING
    until = _minutes(40)
    start = dt.timedelta(0)
    stops = _minutes(10)

    def up(job: str, points: Mapping[dt.timedelta, Point], instance: str | None = None) -> Series:
        return series('up', {JOB_LABEL: job, 'instance': instance or instances[job]}, points, until)

    def healthy(*skip: str) -> list[Series]:
        return [up(job, {start: 1}) for job in jobs if job not in skip]

    def down(job: str, *, at: dt.timedelta, instance: bool = True) -> Check:
        rule = group.rule(TARGET_DOWN, **{JOB_LABEL: job})
        kept = {JOB_LABEL: job, 'instance': instances[job]} if instance else {JOB_LABEL: job}
        return Check(at, TARGET_DOWN, [Alert(rule, kept)])

    def quiet(*at: dt.timedelta) -> list[Check]:
        return [Check(time, TARGET_DOWN) for time in at]

    yield Case(
        'every target reports',
        healthy(),
        [*quiet(stops, until - MARGIN), Check(until - MARGIN, scraper['alert'])],
    )
    for job in jobs:
        yield Case(
            f"{job}: the target's scrape fails",
            [*healthy(job), up(job, {start: 1, stops: 0})],
            [
                *quiet(stops + pending - MARGIN),
                down(job, at=stops + pending + MARGIN),
                Check(stops + pending + MARGIN, scraper['alert']),
            ],
        )
        yield Case(
            f'{job}: the target leaves service discovery',
            [*healthy(job), up(job, {start: 1, stops: STALE})],
            [
                *quiet(stops + pending - MARGIN),
                down(job, at=stops + pending + MARGIN, instance=False),
                Check(stops + pending + MARGIN, scraper['alert']),
            ],
        )
        yield Case(
            f'{job}: no target ever reports',
            healthy(job),
            [*quiet(pending - MARGIN), down(job, at=pending + MARGIN, instance=False)],
        )
    first = jobs[0]
    # A series that ends with no marker is read until its last sample is a
    # query step old, five minutes at vmalert's default (`TARGET_DOWN_PENDING`).
    step = _minutes(5)
    yield Case(
        f"{first}: the target's series ends with no marker",
        [*healthy(first), up(first, {start: 1, stops: None})],
        [
            *quiet(stops + pending, stops + step + pending - MARGIN),
            down(first, at=stops + step + pending + 2 * MARGIN, instance=False),
        ],
    )
    yield Case(
        "vmagent stops: every target's series ends with no marker, within one scrape of one another",
        [up(job, {start: 1, stops + SCRAPE_INTERVAL * (index % 2): None}) for index, job in enumerate(jobs)],
        [
            *quiet(*(stops + _minutes(m) for m in (4, 5, 6, 7, 10, 15, 20))),
            Check(stops + step + pending - 2 * MARGIN, scraper['alert']),
            Check(stops + step + pending + 2 * MARGIN, scraper['alert'], [Alert(scraper)]),
            Check(until - MARGIN, scraper['alert'], [Alert(scraper)]),
        ],
    )
    yield Case(
        'every target leaves service discovery at once',
        [up(job, {start: 1, stops: STALE}) for job in jobs],
        [
            *quiet(*(stops + _minutes(m) for m in (1, 4, 6, 10))),
            Check(stops + pending - MARGIN, scraper['alert']),
            Check(stops + pending + MARGIN, scraper['alert'], [Alert(scraper)]),
        ],
    )
    restarted = pending - 2 * MARGIN
    yield Case(
        f'{first}: the target restarts, the new one reporting inside the pending after the old one leaves',
        [
            *healthy(first),
            up(first, {start: 1, stops: STALE}),
            up(first, {stops + restarted: 1}, '10.0.0.99:8080'),
        ],
        quiet(stops + MARGIN, stops + restarted - MARGIN, stops + pending + MARGIN, until - MARGIN),
    )
    yield Case(
        f"{first}: the target's scrape fails for less than the pending",
        [*healthy(first), up(first, {start: 1, stops: 0, stops + pending - MARGIN: 1})],
        quiet(*(stops + _minutes(m) for m in (2, 4, 5, 6, 10))),
    )


# -- kubeconfig-copy ----------------------------------------------------------------


def kubeconfig_copy(group: Group) -> Iterator[Case]:
    """The copy's certificate before the margin ahead of its expiry, and from it on.

    The time the alert fires from is the copy's `NotAfter` less the margin,
    read off the copy the program was declared with rather than off the
    expression. The rule reads no series, so no case writes one.
    """
    rule = group.rule(KUBECONFIG_COPY)
    fires = COPY_NOT_AFTER - KUBECONFIG_ALERT_MARGIN - dt.datetime.fromtimestamp(T0, dt.UTC)
    yield Case(
        'the copy is short of the margin ahead of its expiry',
        [],
        [Check(dt.timedelta(0), rule['alert']), Check(fires - MARGIN, rule['alert'])],
    )
    yield Case(
        'the copy is inside the margin ahead of its expiry',
        [],
        [
            Check(fires + MARGIN, rule['alert'], [Alert(rule)]),
            Check(fires + _minutes(20), rule['alert'], [Alert(rule)]),
        ],
    )


# -- The readings ------------------------------------------------------------------

#: The labels the legacy cluster's series carry in the history beside their own.
LEGACY = {readings.CLUSTER_LABEL: 'legacy'}

#: How many operations an etcd member observes a step in these cases: two a second.
OPERATIONS = 60.0

#: The bucket edges the cases' histograms are written with, `+Inf` last. etcd's
#: WAL fsync edge nearest a second is among them (`readings.WAL_FSYNC_SECOND`).
EDGES = ('0.008', '0.016', '0.032', '0.064', '0.128', '0.256', readings.WAL_FSYNC_SECOND, '+Inf')


def _legacy(inputs: Sequence[Series]) -> list[Series]:
    """The same series, as the history holds the legacy cluster's."""
    return [Series(one.metric, {**one.labels, **LEGACY}, one.values) for one in inputs]


def _empty(group: Group, at: dt.timedelta) -> list[Sample]:
    """Every record of the group, reading nothing at `at`."""
    return [Sample(at, record) for record in sorted(group.records)]


def _quiet(group: Group, *at: dt.timedelta) -> list[Check]:
    """No alert of the group firing at any of `at`."""
    return [Check(time, alert) for time in at for alert in sorted(group.alerts)]


def _p99(lower: float, upper: float) -> float:
    """The 99th percentile of observations that all fall in one bucket, as `histogram_quantile` places it."""
    return lower + (upper - lower) * 0.99


def _bucketed(edge: str) -> dict[str, float]:
    """Each bucket's share of a step's observations where every observation falls in the bucket ending at `edge`."""
    reached = False
    shares: dict[str, float] = {}
    for one in EDGES:
        reached = reached or one == edge
        shares[one] = OPERATIONS if reached else 0.0
    return shares


def _histogram(
    metric: str, labels: Labels, phases: Mapping[dt.timedelta, Mapping[str, float]], until: dt.timedelta
) -> list[Series]:
    """A histogram's buckets and count, each phase giving each bucket's growth a step from its time on."""
    out = [
        counter(f'{metric}_bucket', {**labels, 'le': edge}, {at: phase[edge] for at, phase in phases.items()}, until)
        for edge in EDGES
    ]
    out.append(counter(f'{metric}_count', labels, {at: phase['+Inf'] for at, phase in phases.items()}, until))
    return out


def _member(
    node: str, index: int, slow: dt.timedelta | None, until: dt.timedelta, recovered: dt.timedelta | None = None
) -> list[Series]:
    """An etcd member, healthy, and from `slow` on, where given, outside every budget etcd's readings carry.

    Healthy, every fsync, commit and peer round trip takes under 8 ms, and
    every apply between 32 and 64 ms, 50 ms on average. Slow, every fsync
    takes over 8 ms, half over 16 ms and one in sixty over
    `readings.WAL_FSYNC_SECOND`, every
    commit 32 to 64 ms, every apply 128 to 256 ms, 200 ms on average, every
    round trip 64 to 128 ms; one heartbeat and one proposal fail a step, the
    leader changes once at `slow`, and the member has no leader from then on.
    From `recovered` on, where given, it is healthy again.
    """
    labels = {JOB_LABEL: ETCD_JOB, 'node': node, 'instance': f'10.0.1.{index}:2381'}
    start = dt.timedelta(0)

    def turns[T](healthy: T, degraded: T) -> dict[dt.timedelta, T]:
        changes = {start: healthy}
        if slow is not None:
            changes[slow] = degraded
        if recovered is not None:
            changes[recovered] = healthy
        return changes

    def phases(healthy: Mapping[str, float], degraded: Mapping[str, float]) -> dict[dt.timedelta, Mapping[str, float]]:
        return turns(healthy, degraded)

    def slopes(healthy: float, degraded: float) -> dict[dt.timedelta, float]:
        return turns(healthy, degraded)

    # Every fsync over 8 ms, half of them over 16 ms, and one a step past the
    # edge nearest a second.
    stalled = dict.fromkeys(EDGES, OPERATIONS - 1) | {'0.008': 0.0, '0.016': OPERATIONS / 2, '+Inf': OPERATIONS}
    leader = {start: 0.0} if slow is None else {start: 0.0, slow: 1.0, slow + SCRAPE_INTERVAL: 0.0}
    has_leader: dict[dt.timedelta, Point] = dict(turns(1, 0))
    return [
        *_histogram('etcd_disk_wal_fsync_duration_seconds', labels, phases(_bucketed('0.008'), stalled), until),
        *_histogram(
            'etcd_disk_backend_commit_duration_seconds', labels, phases(_bucketed('0.008'), _bucketed('0.064')), until
        ),
        *_histogram(
            'etcd_server_apply_duration_seconds', labels, phases(_bucketed('0.064'), _bucketed('0.256')), until
        ),
        counter('etcd_server_apply_duration_seconds_sum', labels, slopes(3, 12), until),
        *_histogram(
            'etcd_network_peer_round_trip_time_seconds',
            {**labels, 'To': 'peer'},
            phases(_bucketed('0.008'), _bucketed('0.128')),
            until,
        ),
        counter('etcd_server_leader_changes_seen_total', labels, leader, until),
        counter('etcd_server_heartbeat_send_failures_total', labels, slopes(0, 1), until),
        counter('etcd_server_proposals_failed_total', labels, slopes(0, 1), until),
        series('etcd_server_has_leader', labels, has_leader, until),
    ]


def etcd(group: Group) -> Iterator[Case]:
    """Two members healthy; one turning slow beside a healthy one; and the legacy cluster's series alone.

    Each budget alert is silent before the member turns, and fires on that
    member once its reading has crossed and its pending has passed, which a
    check inside the pending holds back.
    """
    until = _minutes(40)
    at = _minutes(20)
    healthy = {
        'etcd_wal_fsync_p99': _p99(0, 0.008),
        'etcd_wal_fsyncs_over_1s': 0,
        'etcd_backend_commit_p99': _p99(0, 0.008),
        'etcd_apply_mean': 0.05,
        'etcd_apply_p99': _p99(0.032, 0.064),
        'etcd_leader_changes': 0,
        'etcd_heartbeat_send_failures': 0,
        'etcd_proposals_failed': 0,
    }

    def node(name: str) -> dict[str, str]:
        return {'node': name}

    yield Case(
        'every member healthy',
        [*_member('cp1', 11, None, until), *_member('cp2', 12, None, until)],
        _quiet(group, at),
        samples=[
            *(
                Sample(at, group.record(name), [(node('cp1'), value), (node('cp2'), value)])
                for name, value in healthy.items()
            ),
            Sample(
                at,
                group.record('etcd_peer_round_trip_p99'),
                [({'node': 'cp1', 'To': 'peer'}, _p99(0, 0.008)), ({'node': 'cp2', 'To': 'peer'}, _p99(0, 0.008))],
            ),
        ],
    )
    slow = _minutes(10)
    inputs = [*_member('cp1', 11, slow, until), *_member('cp2', 12, None, until)]
    late = slow + _minutes(10)
    sustained = {'EtcdWalFsyncSlow', 'EtcdBackendCommitSlow'}

    def fire(alert: str) -> Check:
        rule = group.rule(alert)
        kept = (
            {JOB_LABEL: ETCD_JOB, 'node': 'cp1', 'instance': '10.0.1.11:2381'}
            if alert == 'EtcdNoLeader'
            else node('cp1')
        )
        pending = _pending(rule)
        return Check(slow + max(pending, _minutes(5)) + 2 * MARGIN, alert, [Alert(rule, kept)])

    yield Case(
        'one member turns slow',
        inputs,
        [
            *_quiet(group, slow - MARGIN),
            *(Check(slow + _pending(group.rule(alert)) - MARGIN, alert) for alert in sorted(sustained)),
            *(fire(alert) for alert in sorted(group.alerts)),
        ],
        samples=[
            Sample(late, group.record('etcd_wal_fsyncs_over_1s'), [(node('cp1'), 10), (node('cp2'), 0)]),
            Sample(
                late,
                group.record('etcd_backend_commit_p99'),
                [(node('cp1'), _p99(0.032, 0.064)), (node('cp2'), _p99(0, 0.008))],
            ),
            Sample(late, group.record('etcd_apply_mean'), [(node('cp1'), 0.2), (node('cp2'), 0.05)]),
            Sample(late, group.record('etcd_leader_changes'), [(node('cp1'), 1), (node('cp2'), 0)]),
            Sample(late, group.record('etcd_heartbeat_send_failures'), [(node('cp1'), 10), (node('cp2'), 0)]),
        ],
    )
    # Elevated for the spell and a window after it: less than the pending a
    # latency's p99 is held for.
    spell = _minutes(2)
    yield Case(
        'one member slow for two minutes',
        [*_member('cp1', 11, slow, until, slow + spell), *_member('cp2', 12, None, until)],
        [Check(slow + _minutes(m), alert) for m in (3, 6, 9, 12, 15) for alert in sorted(sustained)],
    )
    yield Case(
        "the legacy cluster's members alone",
        _legacy(inputs),
        _quiet(group, late),
        samples=_empty(group, late),
    )
    yield from _etcd_gap(group)


def _etcd_gap(group: Group) -> Iterator[Case]:
    """A member's slow counters, standing above 0, read again after a scrape gap longer than their windows.

    No counter moves, so no record rises above 0, and the leader-change alert
    stays silent: a window that starts in the gap is not read at all until
    the counter has been read at its start again.
    """
    read, gap, after = 60, 40, 100
    resumed = SCRAPE_INTERVAL * (read + gap + 1)
    labels = {JOB_LABEL: ETCD_JOB, 'node': 'cp1', 'instance': '10.0.1.11:2381'}

    def standing(metric: str, value: int) -> Series:
        return Series(metric, labels, f'{value}+0x{read} _x{gap} {value}+0x{after - 1}')

    counters = {
        'etcd_leader_changes': standing('etcd_server_leader_changes_seen_total', 2),
        'etcd_heartbeat_send_failures': standing('etcd_server_heartbeat_send_failures_total', 5),
        'etcd_proposals_failed': standing('etcd_server_proposals_failed_total', 2),
    }
    leader = group.rule('EtcdLeaderChanged')
    yield Case(
        "a member's counters read again after a scrape gap",
        [*counters.values(), standing('etcd_server_has_leader', 1)],
        [Check(resumed + _minutes(m), leader['alert']) for m in (1, 3, 5, 10, 14, 16, 20)],
        samples=[
            *(Sample(resumed + _minutes(3), group.record(name)) for name in counters),
            *(Sample(resumed + _minutes(20), group.record(name), [({'node': 'cp1'}, 0)]) for name in counters),
        ],
    )


def _exporter(node: str) -> dict[str, str]:
    """The labels the node exporter's series carry on `node`."""
    return {JOB_LABEL: NODE_EXPORTER_JOB, 'node': node, 'instance': f'{node}.internal:9100'}


#: The nodes of the node exporter's cases: a control plane, and the worker,
#: which no budget binds.
CONTROL = conventions.CLOUD_NODES[0]
WORKER = conventions.HOMELAB_NODE


def _cpu_inputs(pressured: dt.timedelta | None, until: dt.timedelta) -> list[Series]:
    """A control plane using a core and its CPU pressure at 10 %, 30 % from `pressured` on; the worker pressured at 50 %."""
    start = dt.timedelta(0)
    modes = {'user': 15.0, 'system': 15.0, 'idle': 15.0, 'iowait': 15.0}
    pressure = {start: 3.0} if pressured is None else {start: 3.0, pressured: 9.0}
    return [
        *(
            counter('node_cpu_seconds_total', {**_exporter(CONTROL), 'cpu': '0', 'mode': mode}, {start: slope}, until)
            for mode, slope in modes.items()
        ),
        counter('node_cpu_seconds_total', {**_exporter(WORKER), 'cpu': '0', 'mode': 'user'}, {start: 6.0}, until),
        counter('node_pressure_cpu_waiting_seconds_total', _exporter(CONTROL), pressure, until),
        counter('node_pressure_cpu_waiting_seconds_total', _exporter(WORKER), {start: 15.0}, until),
    ]


def cpu(group: Group) -> Iterator[Case]:
    """A control plane's CPU pressure rising past its budget over ten minutes, beside a worker past it throughout."""
    rule = group.rule('ControlPlaneCpuPressure')
    until = _minutes(40)
    at = _minutes(20)
    yield Case(
        'steady',
        _cpu_inputs(None, until),
        [Check(at, rule['alert'])],
        samples=[
            Sample(at, group.record('node_cpu_used'), [({'node': CONTROL}, 1.0), ({'node': WORKER}, 0.2)]),
            *(
                Sample(at, record, [({'node': CONTROL}, 0.1), ({'node': WORKER}, 0.5)])
                for record in sorted(group.records)
                if 'pressure' in record
            ),
        ],
    )
    pressured = _minutes(15)
    inputs = _cpu_inputs(pressured, until)
    # Ten minutes' mean: 10 % before, 30 % after, so 20 % five minutes in.
    yield Case(
        'a control plane pressured',
        inputs,
        [
            Check(pressured - MARGIN, rule['alert']),
            Check(pressured + _minutes(4), rule['alert']),
            Check(pressured + _minutes(7), rule['alert'], [Alert(rule, {'node': CONTROL})]),
        ],
    )
    yield Case("the legacy cluster's nodes alone", _legacy(inputs), _quiet(group, at), samples=_empty(group, at))


def memory(group: Group) -> Iterator[Case]:
    """A control plane's available memory dipping for one scrape, and its memory pressure; and the legacy cluster's alone."""
    until = _minutes(30)
    start = dt.timedelta(0)
    dip = _minutes(10)
    gib = 2**30
    inputs = [
        series(
            'node_memory_MemAvailable_bytes',
            _exporter(CONTROL),
            {start: 2 * gib, dip: gib / 2, dip + SCRAPE_INTERVAL: 2 * gib},
            until,
        ),
        series('node_memory_MemAvailable_bytes', _exporter(WORKER), {start: 4 * gib}, until),
        counter('node_pressure_memory_stalled_seconds_total', _exporter(CONTROL), {start: 3.0}, until),
        counter('node_pressure_memory_stalled_seconds_total', _exporter(WORKER), {start: 0.0}, until),
    ]
    yield Case(
        'a dip in available memory',
        inputs,
        samples=[
            Sample(
                dip - MARGIN,
                group.record('node_memory_available'),
                [({'node': CONTROL}, 2 * gib), ({'node': WORKER}, 4 * gib)],
            ),
            Sample(
                dip + 2 * MARGIN,
                group.record('node_memory_available'),
                [({'node': CONTROL}, gib / 2), ({'node': WORKER}, 4 * gib)],
            ),
            Sample(
                dip + 2 * MARGIN,
                group.record('node_memory_pressure_full'),
                [({'node': CONTROL}, 0.1), ({'node': WORKER}, 0)],
            ),
        ],
    )
    yield Case("the legacy cluster's nodes alone", _legacy(inputs), samples=_empty(group, dip))


def _disk_inputs(pressured: dt.timedelta | None, until: dt.timedelta) -> list[Series]:
    """A control plane's boot volume and its second volume written, and its I/O pressure at 5 %, 20 % from `pressured` on.

    The worker's pressure is 20 % throughout.
    """
    start = dt.timedelta(0)
    boot, other = {**_exporter(CONTROL), 'device': 'sda'}, {**_exporter(CONTROL), 'device': 'sdb'}
    pressure = {start: 1.5} if pressured is None else {start: 1.5, pressured: 6.0}
    return [
        counter('node_disk_writes_completed_total', boot, {start: 1500.0}, until),
        counter('node_disk_writes_completed_total', other, {start: 3000.0}, until),
        counter('node_disk_written_bytes_total', boot, {start: 30e6}, until),
        counter('node_disk_written_bytes_total', other, {start: 90e6}, until),
        counter('node_disk_write_time_seconds_total', boot, {start: 7.5}, until),
        counter('node_disk_write_time_seconds_total', other, {start: 30.0}, until),
        counter('node_disk_io_time_weighted_seconds_total', boot, {start: 15.0}, until),
        counter('node_disk_io_time_weighted_seconds_total', other, {start: 30.0}, until),
        counter('node_pressure_io_waiting_seconds_total', _exporter(CONTROL), pressure, until),
        counter('node_pressure_io_waiting_seconds_total', _exporter(WORKER), {start: 6.0}, until),
        counter('node_pressure_io_stalled_seconds_total', _exporter(CONTROL), {start: 0.75}, until),
        # etcd's own: ten WAL fsyncs and five commits a second, and 0.2 MB/s
        # of WAL.
        *(
            counter(metric, {JOB_LABEL: ETCD_JOB, 'node': CONTROL}, {start: slope}, until)
            for metric, slope in (
                ('etcd_disk_wal_fsync_duration_seconds_count', 300.0),
                ('etcd_disk_backend_commit_duration_seconds_count', 150.0),
                ('etcd_disk_wal_write_bytes_total', 6e6),
            )
        ),
    ]


#: OCI's series step: a sample a minute.
OCI_STEP = dt.timedelta(minutes=1)


def _oci_inputs(throttled: dt.timedelta, until: dt.timedelta) -> list[Series]:
    """A control plane's boot volume throttled for one minute at `throttled`, and its node volume throttled throughout."""
    start = dt.timedelta(0)

    def volume(name: str) -> dict[str, str]:
        return {'node': CONTROL, 'volume': name}

    def oci(metric: str, labels: Labels, points: Mapping[dt.timedelta, Point]) -> Series:
        return series(f'{OCI_BLOCKSTORE_METRICS}_{metric}', labels, points, until, OCI_STEP)

    return [
        oci('VolumeThrottledIOs', volume(BOOT_VOLUME), {start: 0, throttled: 5, throttled + OCI_STEP: 0}),
        oci('VolumeThrottledIOs', volume('hath-cache'), {start: 7}),
        oci('VolumeGuaranteedIOPS', volume(BOOT_VOLUME), {start: 3000}),
        oci('VolumeGuaranteedThroughput', volume(BOOT_VOLUME), {start: 24}),
    ]


def disk(group: Group) -> Iterator[Case]:
    """The boot volume read apart from the node's other volumes; I/O pressure rising past its budget; OCI's throttling.

    The throttled minute raises the alert for as long as the window holds it,
    and a volume other than a boot volume raises nothing.
    """
    pressure = group.rule('ControlPlaneIoPressure')
    throttle = group.rule('BootVolumeThrottled')
    until = _minutes(40)
    at = _minutes(20)
    node = {'node': CONTROL}
    yield Case(
        'steady',
        _disk_inputs(None, until),
        [Check(at, pressure['alert'])],
        samples=[
            Sample(at, group.record('boot_volume_write_iops'), [(node, 50)]),
            Sample(at, group.record('boot_volume_write_bytes'), [(node, 1e6)]),
            Sample(at, group.record('boot_volume_write_latency'), [(node, 0.005)]),
            Sample(at, group.record('boot_volume_io_in_flight'), [(node, 0.5)]),
            Sample(at, group.record('node_io_pressure_some'), [(node, 0.05), ({'node': WORKER}, 0.2)]),
            Sample(at, group.record('node_io_pressure_full'), [(node, 0.025)]),
            Sample(at, group.record('boot_volume_other_write_iops'), [(node, 35)]),
            Sample(at, group.record('boot_volume_other_write_bytes'), [(node, 0.8e6)]),
        ],
    )
    pressured = _minutes(15)
    # Ten minutes' mean: 5 % before, 20 % after, so 10 % three minutes and
    # twenty seconds in.
    yield Case(
        'a control plane pressured',
        _disk_inputs(pressured, until),
        [
            Check(pressured - MARGIN, pressure['alert']),
            Check(pressured + _minutes(2), pressure['alert']),
            Check(pressured + _minutes(6), pressure['alert'], [Alert(pressure, node)]),
        ],
    )
    window = readings.OCI_WINDOW
    throttled = window + _minutes(10)
    oci_until = throttled + window + _minutes(10)
    boot = {'node': CONTROL, 'volume': BOOT_VOLUME}
    yield Case(
        'a boot volume throttled for a minute',
        _oci_inputs(throttled, oci_until),
        [
            Check(throttled - MARGIN, throttle['alert']),
            Check(throttled + MARGIN, throttle['alert'], [Alert(throttle, boot)]),
            Check(throttled + window - MARGIN, throttle['alert'], [Alert(throttle, boot)]),
            Check(throttled + window + 2 * MARGIN, throttle['alert']),
        ],
        OCI_STEP,
        samples=[
            Sample(
                throttled + _minutes(10),
                group.record('volume_throttled_ios'),
                [(boot, 5), ({'node': CONTROL, 'volume': 'hath-cache'}, 7 * (window // OCI_STEP))],
            ),
            Sample(throttled, group.record('volume_guaranteed_iops'), [(boot, 3000)]),
            Sample(throttled, group.record('volume_guaranteed_throughput'), [(boot, 24)]),
        ],
    )
    yield Case(
        "the legacy cluster's nodes and volumes alone",
        _legacy([*_disk_inputs(pressured, until), *_oci_inputs(_minutes(30), until)]),
        _quiet(group, at, _minutes(31)),
        samples=_empty(group, _minutes(31)),
    )


def network(group: Group) -> Iterator[Case]:
    """A control plane's bond and its member link, KubeSpan, a dummy and a pod's link; another's bond; the worker's link.

    The bond carries what its member does, so the uplink is read by the bond.
    etcd's peers and each node's Cilium agent's probes of every node's host
    stand for KubeSpan's paths: a path that missed a probe reads as down, an
    agent its scrape missed, or one that publishes no count, reads as its
    paths down, and a path not yet probed (`unknown`) and a health endpoint
    that missed one (`type="endpoint"`) read as nothing. And the legacy
    cluster's series alone.
    """
    until = _minutes(30)
    at = _minutes(20)
    start = dt.timedelta(0)

    # The second control plane's bond has a member the Ethernet names do not
    # cover, so only the bond reads its traffic.
    peers = conventions.CLOUD_NODES[1]

    def link(device: str, node: str = CONTROL) -> dict[str, str]:
        return {**_exporter(node), 'device': device}

    slopes = [
        (link('bond0'), 'receive', 30e6),
        (link('bond0'), 'transmit', 15e6),
        (link('enp0s6'), 'receive', 30e6),
        (link('enp0s6'), 'transmit', 15e6),
        (link(readings.KUBESPAN), 'receive', 6e6),
        (link(readings.KUBESPAN), 'transmit', 3e6),
        (link('dummy0'), 'receive', 3e6),
        (link('dummy0'), 'transmit', 3e6),
        (link('lxc0a1b2c'), 'receive', 3e6),
        (link('lxc0a1b2c'), 'transmit', 3e6),
        (link('ens18', WORKER), 'receive', 3e6),
        (link('ens18', WORKER), 'transmit', 3e6),
        (link('bond0', peers), 'receive', 6e6),
        (link('bond0', peers), 'transmit', 6e6),
    ]
    drops = [(link('bond0'), 1.0), (link('enp0s6'), 0.0), (link(readings.KUBESPAN), 0.0), (link('dummy0'), 5.0)]
    peer = {JOB_LABEL: ETCD_JOB, 'node': CONTROL, 'Local': 'a'}
    blip = at - _minutes(2)
    joining = conventions.CLOUD_NODES[2]
    # An agent that answers its scrape and publishes no count, as one does
    # before its first probe report.
    silent = 'node-silent'

    def agent(node: str) -> dict[str, str]:
        return {JOB_LABEL: CILIUM_AGENT_JOB, 'node': node, 'instance': f'{node}.internal:9962'}

    def paths(node: str, kind: str, status: str, points: Mapping[dt.timedelta, Point]) -> Series:
        return series(readings.NODE_HEALTH, {**agent(node), 'type': kind, 'status': status}, points, until)

    inputs = [
        *(
            counter(f'node_network_{direction}_bytes_total', labels, {start: slope}, until)
            for labels, direction, slope in slopes
        ),
        *(
            counter(f'node_network_{direction}_drop_total', labels, {start: slope}, until)
            for labels, slope in drops
            for direction in ('receive', 'transmit')
        ),
        series('etcd_network_active_peers', {**peer, 'Remote': 'b'}, {start: 1}, until),
        series(
            'etcd_network_active_peers', {**peer, 'Remote': 'c'}, {start: 1, blip: 0, blip + SCRAPE_INTERVAL: 1}, until
        ),
        *(_server_id(member, until) for member in ('a', 'b', 'c')),
        *(series('up', agent(node), {start: 1}, until) for node in (CONTROL, peers, joining, silent)),
        paths(CONTROL, 'node', 'unreachable', {start: 0}),
        paths(CONTROL, 'node', 'reachable', {start: 8}),
        paths(CONTROL, 'endpoint', 'unreachable', {start: 2}),
        paths(peers, 'node', 'unreachable', {start: 0, blip: 2, blip + SCRAPE_INTERVAL: 0}),
        paths(joining, 'node', 'unreachable', {start: 0}),
        paths(joining, 'node', 'unknown', {start: 2}),
        # The worker's agent stops answering its scrape: its counts go stale
        # and its `up` reads 0.
        series('up', agent(WORKER), {start: 1, blip: 0}, until),
        paths(WORKER, 'node', 'unreachable', {start: 0, blip: STALE}),
    ]
    node = {'node': CONTROL}
    worker = {'node': WORKER}
    yield Case(
        'nodes moving traffic',
        inputs,
        samples=[
            Sample(at, group.record('node_network_bytes'), [(node, 1.5e6), ({'node': peers}, 0.4e6), (worker, 0.2e6)]),
            Sample(
                at,
                group.record('node_link_bytes'),
                [
                    ({**node, 'device': 'bond0'}, 1.5e6),
                    ({**node, 'device': 'enp0s6'}, 1.5e6),
                    ({**node, 'device': readings.KUBESPAN}, 0.3e6),
                    ({**worker, 'device': 'ens18'}, 0.2e6),
                    ({'node': peers, 'device': 'bond0'}, 0.4e6),
                ],
            ),
            Sample(
                at,
                group.record('node_link_drops'),
                [
                    ({**node, 'device': 'bond0'}, 20),
                    ({**node, 'device': 'enp0s6'}, 0),
                    ({**node, 'device': readings.KUBESPAN}, 0),
                ],
            ),
            Sample(at, group.record('etcd_active_peers'), [({**node, 'Remote': 'b'}, 1), ({**node, 'Remote': 'c'}, 0)]),
            Sample(
                at,
                group.record('kubespan_paths_reachable'),
                [(node, 1), ({'node': peers}, 0), ({'node': joining}, 1), (worker, 0), ({'node': silent}, 0)],
            ),
        ],
    )
    yield Case("the legacy cluster's nodes alone", _legacy(inputs), samples=_empty(group, at))
    yield _replaced_member(group)


def _server_id(member: str, until: dt.timedelta, points: Mapping[dt.timedelta, Point] | None = None) -> Series:
    """A member's own report of its ID, `member`, on its listener."""
    labels = {JOB_LABEL: ETCD_JOB, 'node': f'node-{member}', 'server_id': member}
    return series('etcd_server_id', labels, points or {dt.timedelta(0): 1}, until)


def _replaced_member(group: Group) -> Case:
    """A peer replaced: the old member's series stays on the survivor at 0, and only members that report count.

    etcd deletes no peer's series when the peer is removed, so the old
    member's lingers for as long as the survivor's process runs.
    """
    until = _minutes(40)
    start = dt.timedelta(0)
    down, replaced = _minutes(8), _minutes(11)
    peer = {JOB_LABEL: ETCD_JOB, 'node': CONTROL, 'Local': 'a'}
    inputs = [
        series('etcd_network_active_peers', {**peer, 'Remote': 'b'}, {start: 1}, until),
        series('etcd_network_active_peers', {**peer, 'Remote': 'c0'}, {start: 1, down: 0}, until),
        series('etcd_network_active_peers', {**peer, 'Remote': 'c1'}, {replaced: 1}, until),
        _server_id('a', until),
        _server_id('b', until),
        _server_id('c0', until, {start: 1, down: STALE}),
        _server_id('c1', until, {replaced: 1}),
    ]
    node = {'node': CONTROL}

    def peers(*members: str) -> list[tuple[Labels, float]]:
        return [({**node, 'Remote': member}, 1) for member in members]

    return Case(
        'a peer replaced',
        inputs,
        samples=[
            Sample(down - MARGIN, group.record('etcd_active_peers'), peers('b', 'c0')),
            Sample(_minutes(35), group.record('etcd_active_peers'), peers('b', 'c1')),
        ],
    )


#: The image of a pod's sandbox, which `cAdvisor` reports under the pod with no
#: container, beside the pod's own control group.
PAUSE = 'registry.k8s.io/pause:3.10.1'


def _pod(
    node: str, namespace: str, name: str, container: str | None = None, image: str | None = None
) -> dict[str, str]:
    """The labels of a `cAdvisor` series of a pod: its own control group, or one of its containers', or its sandbox's."""
    labels = {JOB_LABEL: KUBELET_JOB, 'node': node, 'namespace': namespace, 'pod': name}
    return labels | ({'container': container} if container else {}) | ({'image': image} if image else {})


def _limits(
    name: str, resource: str, points: Mapping[dt.timedelta, Point], until: dt.timedelta, namespace: str = 'blog'
) -> Series:
    """kube-state-metrics' limit of a pod's one container on the control plane."""
    labels = {
        JOB_LABEL: KUBE_STATE_METRICS_JOB,
        'node': CONTROL,
        'namespace': namespace,
        'pod': name,
        'container': 'app',
        'resource': resource,
    }
    return series('kube_pod_container_resource_limits', labels, points, until)


def _phase(
    name: str, phase: str, points: Mapping[dt.timedelta, Point], until: dt.timedelta, namespace: str = 'blog'
) -> Series:
    """kube-state-metrics' series of a pod's phase: 1 where the pod is in it."""
    labels = {JOB_LABEL: KUBE_STATE_METRICS_JOB, 'namespace': namespace, 'pod': name, 'phase': phase}
    return series('kube_pod_status_phase', labels, points, until)


def load_classes(group: Group) -> Iterator[Case]:
    """Two control planes, one running a workload and Envoy beside the platform, one running neither; a rollout; and the legacy cluster's.

    Each pod is reported by its own control group, which the readings read,
    and by its containers' and its sandbox's, which they do not.
    """
    until = _minutes(30)
    at = _minutes(20)
    start = dt.timedelta(0)
    mib = 2**20
    other = conventions.CLOUD_NODES[1]
    slice_pods = ('kube-apiserver', 'kube-controller-manager', 'kube-scheduler')
    apiserver, manager, scheduler = (_pod(CONTROL, 'kube-system', f'{name}-{CONTROL}') for name in slice_pods)
    envoy = _pod(CONTROL, 'kube-system', 'cilium-envoy-x')
    blog = _pod(CONTROL, 'blog', 'blog-0')
    other_apiserver = _pod(other, 'kube-system', f'kube-apiserver-{other}')
    # Cores used, as a step's CPU seconds: a core is 30.
    cpu = [
        (apiserver, 7.5),
        (manager, 3.75),
        (scheduler, 3.75),
        (_pod(CONTROL, 'kube-system', 'cilium-agent-x'), 15.0),
        (envoy, 3.75),
        (blog, 3.75),
        (_pod(CONTROL, 'blog', 'blog-0', container='app'), 3.75),
        (_pod(CONTROL, 'blog', 'blog-0', image=PAUSE), 3.75),
        (_pod(CONTROL, conventions.MONITORING_NAMESPACE, 'node-exporter-x'), 1.5),
        (other_apiserver, 3.75),
    ]
    memory = [
        (apiserver, 1024 * mib),
        (manager, 64 * mib),
        (scheduler, 64 * mib),
        (envoy, 128 * mib),
        (blog, 256 * mib),
        (_pod(CONTROL, 'blog', 'blog-0', container='app'), 256 * mib),
        (_pod(CONTROL, 'blog', 'blog-0', image=PAUSE), 1 * mib),
        (other_apiserver, 512 * mib),
    ]
    # Bytes written, a step's: 3 MB is 0.1 MB/s.
    writes = [
        (apiserver, 3e6),
        (envoy, 0.0),
        (blog, 3e6),
        (_pod(CONTROL, 'blog', 'blog-0', container='app'), 3e6),
        (_pod(CONTROL, 'blog', 'blog-0', image=PAUSE), 3e6),
        (other_apiserver, 0.0),
    ]
    etcd = {node: {JOB_LABEL: ETCD_JOB, 'node': node} for node in (CONTROL, other)}
    inputs = [
        counter('node_cpu_seconds_total', {**_exporter(CONTROL), 'cpu': '0', 'mode': 'user'}, {start: 30.0}, until),
        counter('node_cpu_seconds_total', {**_exporter(CONTROL), 'cpu': '0', 'mode': 'system'}, {start: 15.0}, until),
        counter('node_cpu_seconds_total', {**_exporter(CONTROL), 'cpu': '0', 'mode': 'idle'}, {start: 15.0}, until),
        counter('node_cpu_seconds_total', {**_exporter(other), 'cpu': '0', 'mode': 'user'}, {start: 15.0}, until),
        *(counter('process_cpu_seconds_total', labels, {start: 7.5}, until) for labels in etcd.values()),
        *(counter('container_cpu_usage_seconds_total', labels, {start: slope}, until) for labels, slope in cpu),
        series('node_memory_MemTotal_bytes', _exporter(CONTROL), {start: 8192 * mib}, until),
        series('node_memory_MemAvailable_bytes', _exporter(CONTROL), {start: 5120 * mib}, until),
        series('node_memory_MemTotal_bytes', _exporter(other), {start: 8192 * mib}, until),
        series('node_memory_MemAvailable_bytes', _exporter(other), {start: 6144 * mib}, until),
        *(series('process_resident_memory_bytes', labels, {start: 128 * mib}, until) for labels in etcd.values()),
        *(series('container_memory_working_set_bytes', labels, {start: value}, until) for labels, value in memory),
        counter('node_disk_written_bytes_total', {**_exporter(CONTROL), 'device': 'sda'}, {start: 30e6}, until),
        counter('node_disk_written_bytes_total', {**_exporter(other), 'device': 'sda'}, {start: 3e6}, until),
        counter('etcd_disk_wal_write_bytes_total', etcd[CONTROL], {start: 6e6}, until),
        counter('etcd_disk_wal_write_bytes_total', etcd[other], {start: 1.5e6}, until),
        *(counter('container_fs_writes_bytes_total', labels, {start: slope}, until) for labels, slope in writes),
        _limits('blog-0', 'cpu', {start: 0.25}, until),
        _limits('blog-1', 'cpu', {start: 0.25}, until),
        _limits('blog-0', 'memory', {start: 256 * mib}, until),
        _limits('blog-1', 'memory', {start: 256 * mib}, until),
        _limits('cilium-agent-x', 'cpu', {start: 1.0}, until, 'kube-system'),
        _phase('blog-0', 'Running', {start: 1}, until),
        _phase('blog-1', 'Running', {start: 1}, until),
        _phase('cilium-agent-x', 'Running', {start: 1}, until, 'kube-system'),
    ]

    def by_class(node: str, **values: float) -> list[tuple[Labels, float]]:
        return [({'node': node, readings.CLASS_LABEL: name.replace('_', '-')}, value) for name, value in values.items()]

    yield Case(
        'a control plane with a workload beside one with none',
        inputs,
        samples=[
            Sample(
                at,
                group.record('load_class_cpu'),
                [
                    *by_class(CONTROL, control_plane=0.75, workloads=0.25, platform=0.5),
                    *by_class(other, control_plane=0.375, workloads=0, platform=0.125),
                ],
            ),
            Sample(
                at,
                group.record('load_class_memory'),
                [
                    *by_class(CONTROL, control_plane=1280 * mib, workloads=384 * mib, platform=1408 * mib),
                    *by_class(other, control_plane=640 * mib, workloads=0, platform=1408 * mib),
                ],
            ),
            Sample(
                at,
                group.record('load_class_io'),
                [
                    *by_class(CONTROL, control_plane=0.3e6, workloads=0.1e6, platform=0.6e6),
                    *by_class(other, control_plane=0.05e6, workloads=0, platform=0.05e6),
                ],
            ),
            Sample(at, group.record('workloads_cpu_limits'), [({'node': CONTROL}, 0.5)]),
            Sample(at, group.record('workloads_memory_limits'), [({'node': CONTROL}, 512 * mib)]),
        ],
    )
    # One pod replaced by another at `replaced`, its limit then lowered in
    # place at `resized`, and a Job's pod finished: only the pod that runs
    # holds its limits, at the limit it has now.
    replaced = _minutes(5)
    resized = _minutes(10)
    gone: dict[dt.timedelta, Point] = {start: 1, replaced: STALE}
    rollout = [
        _limits('blog-old', 'cpu', {start: 0.25, replaced: STALE}, until),
        _limits('blog-old', 'memory', {start: 256 * mib, replaced: STALE}, until),
        _phase('blog-old', 'Running', gone, until),
        _limits('blog-new', 'cpu', {replaced: 0.25, resized: 0.125}, until),
        _limits('blog-new', 'memory', {replaced: 256 * mib}, until),
        _phase('blog-new', 'Running', {replaced: 1}, until),
        _limits('backup-x', 'cpu', {start: 0.3}, until),
        _limits('backup-x', 'memory', {start: 128 * mib}, until),
        _phase('backup-x', 'Succeeded', {start: 1}, until),
        _phase('backup-x', 'Running', {start: 0}, until),
    ]
    yield Case(
        'a rollout beside a finished Job',
        rollout,
        samples=[
            *(
                Sample(time, group.record('workloads_cpu_limits'), [({'node': CONTROL}, 0.25)])
                for time in (replaced - MARGIN, replaced + 2 * MARGIN)
            ),
            Sample(resized + 2 * MARGIN, group.record('workloads_cpu_limits'), [({'node': CONTROL}, 0.125)]),
            Sample(replaced + 2 * MARGIN, group.record('workloads_memory_limits'), [({'node': CONTROL}, 256 * mib)]),
        ],
    )
    yield Case("the legacy cluster's nodes alone", _legacy([*inputs, *rollout]), samples=_empty(group, at))


def api_server(group: Group) -> Iterator[Case]:
    """Requests read by verb, a watch apart; and the legacy cluster's alone."""
    until = _minutes(30)
    at = _minutes(20)
    start = dt.timedelta(0)
    labels = {JOB_LABEL: APISERVER_JOB, 'instance': '10.0.1.11:6443'}
    inputs = [
        *_histogram(
            'apiserver_request_duration_seconds', {**labels, 'verb': 'GET'}, {start: _bucketed('0.128')}, until
        ),
        *_histogram(
            'apiserver_request_duration_seconds', {**labels, 'verb': 'WATCH'}, {start: _bucketed('+Inf')}, until
        ),
    ]
    yield Case(
        'reads and a watch',
        inputs,
        samples=[Sample(at, group.record('apiserver_request_p99'), [({'verb': 'GET'}, _p99(0.064, 0.128))])],
    )
    yield Case("the legacy cluster's API server alone", _legacy(inputs), samples=_empty(group, at))


@dataclass(frozen=True)
class Family:
    """How a declared group is evaluated: its cases, how often vmalert-tool evaluates its rules, and in how many runs."""

    cases: Callable[[Group], Iterator[Case]]
    evaluation: dt.timedelta = EVALUATION_INTERVAL
    parts: Sequence[str] = ('',)
    """The runs of `vmalert-tool` the cases are split across, each the cases whose `part` is its own.

    Each run evaluates the whole group, since a case holds every alert of a
    name, and only the cases are divided: a family whose cases together run
    long is split so that each run stays well inside `COMMAND_TIMEOUT`, and
    the runs are cases of their own, which the workers share."""


#: The cases `scheduled_jobs` gives each row, each a `vmalert-tool` run of its own.
SCHEDULED_JOB_CASES = (
    'every run succeeds',
    'never scheduled',
    'the first run fails',
    'the first run succeeds just inside its deadline',
    'a run fails after one succeeds, and the next succeeds',
    'the first run fails, every other CronJob healthy',
)
#: The scheduled-job family's runs, one per case, named as the case is.
SCHEDULED_JOB_PARTS = tuple(
    f'{job.namespace}/{job.cronjob}: {kind}' for job in schedule.SCHEDULED_JOBS for kind in SCHEDULED_JOB_CASES
)


#: Each declared group's cases, by the group's name.
FAMILIES: Mapping[str, Family] = {
    'alert-channel': Family(alert_channel),
    'nodes': Family(nodes),
    'backup-freshness': Family(backup_freshness, BACKUP_CLOCK),
    'metrics-store': Family(metrics_store),
    'scheduled-jobs': Family(scheduled_jobs, parts=SCHEDULED_JOB_PARTS),
    'scrape-targets': Family(scrape_targets),
    'kubeconfig-copy': Family(kubeconfig_copy),
    'etcd': Family(etcd),
    'cpu': Family(cpu),
    'memory': Family(memory),
    'disk': Family(disk),
    'network': Family(network),
    'load-classes': Family(load_classes),
    'apiserver': Family(api_server),
}


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='groups')
async def groups_fixture() -> dict[str, Group]:
    """Every rule group the program declares, by name, as its `VMRule` spells it.

    The program is configured with a copy of the kubeconfig whose certificate
    expires at `COPY_NOT_AFTER`, in place of the one `k8s_base_installation`
    configures; nothing else of the run differs.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(k8s_base_installation, 'KUBECONFIG', kubeconfig(certificate_user(COPY_NOT_AFTER)))
        run: Run = await applied()
    groups: dict[str, Group] = {}
    for rule in run.monitor.of_type(VM_RULE):
        for declared in rule.inputs['spec']['groups']:
            assert declared['name'] not in groups, f'two groups are declared under the name {declared["name"]}'
            groups[declared['name']] = Group(declared['name'], declared['rules'])
    return groups


#: Every `vmalert-tool` run, by its family and its part.
RUNS = [
    pytest.param(name, part, id=f'{name}[{part}]' if part else name)
    for name in FAMILIES
    for part in FAMILIES[name].parts
]


def test_every_declared_group_has_cases(groups: dict[str, Group]) -> None:
    assert set(groups) == set(FAMILIES)


@pytest.mark.parametrize('name', FAMILIES)
def test_every_case_is_in_a_run_and_every_run_has_cases(groups: dict[str, Group], name: str) -> None:
    """A case whose part names no run would be evaluated by none, and a run with no case passes on nothing."""
    parts = [case.part for case in FAMILIES[name].cases(groups[name])]

    assert set(parts) == set(FAMILIES[name].parts)


@pytest.mark.parametrize('name', FAMILIES)
def test_every_rule_of_the_group_fires_in_a_check_and_every_alert_is_silent_in_one(
    groups: dict[str, Group], name: str
) -> None:
    """Each declared alert, by identity, fires in some check; each alert name is silent in some check.

    The check-in is the exception to silence: its tier is the one whose rule
    fires always. Each record is read with a value in some case, and with
    none in another.
    """
    group = groups[name]
    cases = list(FAMILIES[name].cases(group))
    checks = [check for case in cases for check in case.checks]
    fired = [alert.rule for check in checks for alert in check.firing]
    alerts = [rule for rule in group.rules if 'alert' in rule]
    unfired = [rule['alert'] for rule in alerts if not any(rule is one for one in fired)]
    assert not unfired, f'{name}: no check fires {unfired}'
    silent = {check.alert for check in checks if not check.firing}
    always = {rule['alert'] for rule in alerts if rule['labels'][TIER_LABEL] == Tier.HEARTBEAT.value}
    assert group.alerts - always <= silent
    samples = [sample for case in cases for sample in case.samples]
    assert group.records <= {sample.record for sample in samples if sample.values}, f'{name}: a record is never read'
    assert group.records <= {sample.record for sample in samples if not sample.values}, (
        f'{name}: a record is never empty'
    )


@pytest.mark.timeout(CASE_TIMEOUT)
@pytest.mark.parametrize(('name', 'part'), RUNS)
def test_the_groups_alerts_are_what_each_case_says_they_are(
    groups: dict[str, Group], name: str, part: str, tmp_path: Path
) -> None:
    tool = pinned_tools.located(VMALERT_TOOL)
    group = groups[name]
    rules = tmp_path / 'rules.yaml'
    rules.write_text(yaml.safe_dump({'groups': [{'name': group.name, 'rules': list(group.rules)}]}, sort_keys=False))
    tests = tmp_path / 'tests.yaml'
    tests.write_text(
        yaml.safe_dump(
            {
                'rule_files': [rules.name],
                'evaluation_interval': backup.duration(FAMILIES[name].evaluation),
                'tests': [_test(case, group.name) for case in FAMILIES[name].cases(group) if case.part == part],
            },
            sort_keys=False,
            width=1 << 20,
        )
    )
    # Its storage is a temporary directory, made under the case's own.
    result = process_sessions.run(
        [tool, 'unittest', f'--files={tests.name}'],
        timeout=COMMAND_TIMEOUT,
        cwd=tmp_path,
        env={**os.environ, 'TMPDIR': str(tmp_path)},
        text=True,
    )
    assert result.returncode == 0, f'{result.stdout}\n{result.stderr}'


def test_vmalert_tool_is_the_release_of_the_vmalert_the_stack_chart_installs() -> None:
    """The cases run on the engine the cluster's vmalert runs: the `vmutils` pin is the stack chart's release.

    The cases encode that engine's reading of an expression -- the query
    step a bare selector is read over, `absent` after a staleness marker --
    so they hold for the cluster only while the two are one release. The
    release is the chart's declared `appVersion`, which `update_crds` records
    beside the bundle: a chart bump that moves it is red here once the record
    is rewritten on its branch, and a bump of the pin at once.
    """
    import tomllib

    pinned = tomllib.loads((ROOT / 'mise.toml').read_text())['tools'][VMUTILS]['version']
    path = sources.bundle_path(ROOT / 'Pulumi.yaml').parent / record.APP_VERSIONS_FILE_NAME
    recorded = json.loads(path.read_text())[f'versions:{CHART}-{STACK_CHART}']['appVersion']

    assert pinned == recorded.removeprefix('v')
