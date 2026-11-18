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

from kluster.components.monitoring import EVALUATION_INTERVAL, KUBE_STATE_METRICS_JOB, SCRAPE_INTERVAL
from kluster.components.monitoring.operator_objects import type_token
from kluster.components.monitoring.rules import (
    BACKUP_SYNC_TIME,
    CLASS_LABEL,
    HEARTBEAT,
    JOB_LABEL,
    KUBECONFIG_COPY,
    SCRAPER_DOWN,
    TARGET_DOWN_PENDING,
    TIER_LABEL,
    target_down,
)
from kluster.conventions import backup, schedule
from kluster.conventions.alert import Tier
from kluster.conventions.cluster import KUBECONFIG_ALERT_MARGIN
from kluster.conventions.schedule import ScheduledJob
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

#: The bound of each `vmalert-tool` run. The slowest group's run, the
#: scheduled-job family's, took about 20 s on an idle 24-core machine, and
#: testing.md §8 measured the real-CLI cases taking three and a half times as
#: long beside four busy processes per core; this is four times that. A
#: stop-loss, which nothing asserts on.
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
        """The one declared rule named `alert` that carries or selects each label of `selected` at its value."""
        found = [
            rule
            for rule in self.rules
            if rule['alert'] == alert
            and set(selected.items()) <= set(rule['labels'].items()) | _equalities(rule['expr'])
        ]
        assert len(found) == 1, f'{self.name} declares {len(found)} rules named {alert} with {selected}'
        return found[0]

    @property
    def alerts(self) -> frozenset[str]:
        return frozenset(rule['alert'] for rule in self.rules)


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
    checks: Sequence[Check]
    interval: dt.timedelta = SCRAPE_INTERVAL


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

    return {
        'name': case.name,
        'interval': backup.duration(case.interval),
        'input_series': [{'series': f'{one.metric}{{{labels(one)}}}', 'values': one.values} for one in case.series],
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

        def quiet(*at: dt.timedelta, stale: Rule = stale, failed: Rule = failed) -> list[Check]:
            return [Check(time, rule['alert']) for time in at for rule in (stale, failed)]

        until = period * 5
        yield Case(
            f'{name}: every run succeeds',
            healthy(job, until),
            quiet(period / 2, period + deadline + MARGIN, age + MARGIN, period * 2 + deadline + MARGIN),
        )
        yield Case(
            f'{name}: never scheduled',
            _cronjob(job, until),
            [
                *quiet(age - MARGIN),
                Check(age + MARGIN, stale['alert'], fire(stale, job)),
                Check(age + MARGIN, failed['alert']),
            ],
        )
        yield Case(
            f'{name}: the first run fails',
            _cronjob(job, until, scheduled=[period]),
            [
                *quiet(period + deadline - MARGIN),
                Check(period + deadline + MARGIN, failed['alert'], fire(failed, job)),
                Check(age + MARGIN, stale['alert'], fire(stale, job)),
                Check(age + MARGIN, failed['alert'], fire(failed, job)),
            ],
        )
        yield Case(
            f'{name}: the first run succeeds just inside its deadline',
            _cronjob(job, until, scheduled=[period], succeeded=[period + deadline - MARGIN]),
            quiet(period + deadline - 2 * MARGIN, period + deadline + MARGIN, age + MARGIN),
        )
        recovery = period * 3 + RUN
        yield Case(
            f'{name}: a run fails after one succeeds, and the next succeeds',
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
        yield Case(
            f'{name}: the first run fails, every other CronJob healthy',
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


@dataclass(frozen=True)
class Family:
    """How a declared group is evaluated: its cases, and how often vmalert-tool evaluates its rules."""

    cases: Callable[[Group], Iterator[Case]]
    evaluation: dt.timedelta = EVALUATION_INTERVAL


#: Each declared group's cases, by the group's name.
FAMILIES: Mapping[str, Family] = {
    'alert-channel': Family(alert_channel),
    'nodes': Family(nodes),
    'backup-freshness': Family(backup_freshness, BACKUP_CLOCK),
    'metrics-store': Family(metrics_store),
    'scheduled-jobs': Family(scheduled_jobs),
    'scrape-targets': Family(scrape_targets),
    'kubeconfig-copy': Family(kubeconfig_copy),
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


def test_every_declared_group_has_cases(groups: dict[str, Group]) -> None:
    assert set(groups) == set(FAMILIES)


@pytest.mark.parametrize('name', FAMILIES)
def test_every_rule_of_the_group_fires_in_a_check_and_every_alert_is_silent_in_one(
    groups: dict[str, Group], name: str
) -> None:
    """Each declared rule, by identity, fires in some check; each alert name is silent in some check.

    The check-in is the exception to silence: its tier is the one whose rule
    fires always.
    """
    group = groups[name]
    checks = [check for case in FAMILIES[name].cases(group) for check in case.checks]
    fired = [alert.rule for check in checks for alert in check.firing]
    unfired = [rule['alert'] for rule in group.rules if not any(rule is one for one in fired)]
    assert not unfired, f'{name}: no check fires {unfired}'
    silent = {check.alert for check in checks if not check.firing}
    always = {rule['alert'] for rule in group.rules if rule['labels'][TIER_LABEL] == Tier.HEARTBEAT.value}
    assert group.alerts - always <= silent


@pytest.mark.timeout(CASE_TIMEOUT)
@pytest.mark.parametrize('name', FAMILIES)
def test_the_groups_alerts_are_what_each_case_says_they_are(
    groups: dict[str, Group], name: str, tmp_path: Path
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
                'tests': [_test(case, group.name) for case in FAMILIES[name].cases(group)],
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
