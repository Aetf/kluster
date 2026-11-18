# The OCI SDK ships no type information (no py.typed, no stubs), so every
# value that crosses its boundary is Unknown to a strict checker. Suppressing
# that here, in the one module of the component that touches the SDK, keeps
# the rest of it under the full standard.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
"""One run of the pull of OCI's block-volume series into the metrics stores, as the hourly CronJob runs it.

**A program of its own.** The monitoring component renders this file into a
ConfigMap beside the run's settings (`components.monitoring.oci_metrics`), and
the `oci-sdk` image runs it with the interpreter and the `oci` SDK it carries
(framework/ci.md §4). So it imports nothing of `kluster` and nothing beside
the standard library and `oci`: the image holds nothing else, and a test holds
the imports to that.

**What a run does.** For each metric of `METRICS`, it reads from the history
store the newest sample it holds of each target volume, asks OCI for the
compartment's series from the oldest of those to now, a `SPAN` at a time,
and writes every sample of a span newer than its volume's newest to vmagent's
import endpoint, which reaches both stores, before it asks for the next span.
A volume the history holds nothing of, or nothing within `BACKFILL`, is read
from `BACKFILL` back, the oldest one-minute data OCI returns, so the first
run after a rebuild, or after the pull stopped for a while, fills the gap by
itself.

**A metric that fails stops alone.** An error reaching OCI or the stores
(`IO_ERRORS`) ends the metric it happened on at the span it happened in, and
the run goes on to the next metric. What was written of the stopped metric
runs unbroken from each volume's newest sample, so the next run resumes from
there; asking for a later span would move the history's newest sample past
the hole, and no run would fill it. Any other error is a defect every metric
would meet alike, and ends the run with its traceback.

**Series and labels.** OCI names a volume by its OCID alone (`RESOURCE_ID`).
The run's settings map each OCID the census declares to its node and its name
there (`Target`), and a series is written as `<namespace>_<metric>` labeled
`node` and `volume` with them. A series OCI returns for an OCID the map does
not hold is skipped and logged: a replaced volume's last days are one. A
series that carries no `RESOURCE_ID` at all is an answer of another shape than
the query asks for, and is an error of its own, logged with the dimensions it
does carry. A run fails, after everything else is written, where a metric
stopped, where a series carried no volume, or where a target got no new
sample, so the CronJob's last success stops moving and `OciMetricsPullStale`
reports it.

**Every request to OCI names the metric namespace**, which is what the
reader's policy is conditioned on: a request that does not carry the
condition's variable is declined.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import oci

__all__ = (
    'BACKFILL',
    'CREDENTIAL_KEYS',
    'IO_ERRORS',
    'METRICS',
    'RESOLUTION',
    'RESOURCE_ID',
    'SPAN',
    'Job',
    'JobRefused',
    'Pulled',
    'Stores',
    'Target',
    'client_for',
    'main',
    'pull',
)

log = logging.getLogger('oci-pull')

#: Each series pulled, by OCI's name, and the statistic a minute of it is
#: aggregated by (OCI's Block Volume metrics reference): the counts of I/O
#: and bytes, and the throttled I/O, are per interval, so a minute is their
#: sum; the guaranteed levels are a level, so a minute is their mean.
METRICS: Mapping[str, str] = {
    'VolumeReadOps': 'sum',
    'VolumeWriteOps': 'sum',
    'VolumeReadThroughput': 'sum',
    'VolumeWriteThroughput': 'sum',
    'VolumeThrottledIOs': 'sum',
    'VolumeGuaranteedIOPS': 'mean',
    'VolumeGuaranteedThroughput': 'mean',
    'VolumeGuaranteedVPUsPerGB': 'mean',
}

#: The resolution asked for, the finest OCI returns.
RESOLUTION = '1m'
_STEP = dt.timedelta(minutes=1)

#: How far back OCI returns one-minute data, and so how far back a run reads.
BACKFILL = dt.timedelta(days=7)

#: The longest window one request asks for. A day of one-minute data per
#: volume is 1,440 points, so a request stays under the points OCI returns
#: in one response for any number of volumes the fleet will carry.
SPAN = dt.timedelta(days=1)

#: The dimension a series names its volume by, and the one the query groups by.
RESOURCE_ID = 'resourceId'

#: The files the credential directory holds, one per key of the sealed value,
#: named as the SDK's configuration names them.
CREDENTIAL_KEYS = ('fingerprint', 'key_content', 'user')

#: The errors of reaching OCI or the stores, which end the metric they happen
#: on and no other: OCI's answers (`ServiceError`, which `TransientServiceError`
#: is), the SDK's transport (`RequestException`, and `ConnectTimeout`, which
#: derives from the vendored `requests` class rather than from it), and
#: `urllib`'s, which are `OSError`s.
IO_ERRORS: tuple[type[Exception], ...] = (
    oci.exceptions.ServiceError,
    oci.exceptions.RequestException,
    oci.exceptions.ConnectTimeout,
    OSError,
)

#: How the history store is asked which series to compare with: this cluster's
#: own, which carry no `cluster` label, apart from any series imported from
#: another cluster under one.
_THIS_CLUSTER = 'cluster=""'


class JobRefused(ValueError):
    """The run's settings are not what a run reads; the message names the field and what is wrong with it."""


@dataclass(frozen=True)
class Target:
    """A volume the census declares: the node it belongs to, and its name there."""

    node: str
    volume: str


@dataclass(frozen=True)
class Job:
    """What one run reads: where the series are, which volumes they are of, and where they go."""

    namespace: str
    """OCI's metric namespace, which names the series too."""
    compartment: str
    tenancy: str
    region: str
    targets: Mapping[str, Target]
    """Each target volume, by its OCID."""
    history: str
    """The history store's address, which the newest samples are read from."""
    importer: str
    """vmagent's address, which the samples are written to."""
    credentials: Path
    """The directory the sealed key opens into, a file per `CREDENTIAL_KEYS`."""

    @staticmethod
    def read(path: Path, *, credentials: Path) -> Job:
        """The settings in `path`, refused by name unless each field is what a run reads."""
        try:
            raw: object = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise JobRefused(f'{path} is not JSON: {exc}') from exc
        settings = _mapping(raw, 'the settings')

        def text(name: str) -> str:
            value = settings.get(name)
            if not isinstance(value, str) or not value:
                raise JobRefused(f'the settings field {name!r} must be a non-empty string')
            return value

        targets: dict[str, Target] = {}
        for ocid, target in _mapping(settings.get('targets'), "the settings field 'targets'").items():
            entry = _mapping(target, f'the target {ocid!r}')
            node, volume = entry.get('node'), entry.get('volume')
            if not isinstance(node, str) or not node or not isinstance(volume, str) or not volume:
                raise JobRefused(f'the target {ocid!r} must name its node and its volume')
            targets[ocid] = Target(node=node, volume=volume)
        if not targets:
            raise JobRefused("the settings field 'targets' names no volume")
        return Job(
            namespace=text('namespace'),
            compartment=text('compartment'),
            tenancy=text('tenancy'),
            region=text('region'),
            targets=targets,
            history=text('history'),
            importer=text('importer'),
            credentials=credentials,
        )


def _mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise JobRefused(f'{what} must be a JSON object')
    return cast('dict[str, object]', value)


def client_for(job: Job) -> Any:
    """OCI's Monitoring client, signing with the key in `job.credentials` and nothing read from a file of the SDK's.

    The key is handed to the SDK in memory, as `key_content`, and reaches no
    argument of any process.
    """
    config: dict[str, str] = {key: (job.credentials / key).read_text().strip() for key in CREDENTIAL_KEYS}
    config |= {'tenancy': job.tenancy, 'region': job.region}
    oci.config.validate_config(config)
    return oci.monitoring.MonitoringClient(config, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY)


class Stores:
    """The metrics stores as a run reaches them: the history store read, vmagent written to."""

    def __init__(self, job: Job) -> None:
        self.job = job

    def newest(self, series: str, now: dt.datetime) -> dict[Target, dt.datetime]:
        """The time of the newest sample of `series` the history store holds per target, within `BACKFILL` of `now`."""
        lookback = f'{int(BACKFILL.total_seconds())}s'
        query = f'max by (node, volume) (tlast_over_time({series}{{{_THIS_CLUSTER}}}[{lookback}]))'
        url = f'{self.job.history}/api/v1/query?' + urllib.parse.urlencode({'query': query, 'time': now.timestamp()})
        with urllib.request.urlopen(url, timeout=60) as response:
            answer = json.load(response)
        found: dict[Target, dt.datetime] = {}
        for row in answer['data']['result']:
            labels = row['metric']
            _, seconds = row['value']
            found[Target(node=labels['node'], volume=labels['volume'])] = dt.datetime.fromtimestamp(
                float(seconds), tz=dt.UTC
            )
        return found

    def write(self, lines: Sequence[str]) -> None:
        """`lines`, VictoriaMetrics' JSON line format, to vmagent's import endpoint."""
        request = urllib.request.Request(
            f'{self.job.importer}/api/v1/import',
            data='\n'.join(lines).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=300):
            pass


@dataclass
class Pulled:
    """What one run did: what it wrote, what it skipped, and where it failed."""

    written: dict[Target, int] = field(default_factory=dict)
    """Samples written per target, every metric counted."""
    skipped: set[str] = field(default_factory=set)
    """The OCIDs of the series of volumes the settings do not map."""
    stopped: dict[str, str] = field(default_factory=dict)
    """Each metric that stopped on an error, and where and why."""
    shapeless: dict[str, list[str]] = field(default_factory=dict)
    """Each metric whose answer held a series with no volume, and the dimensions such a series carried."""

    def missing(self, job: Job) -> list[Target]:
        """The targets the run wrote no sample of, in the settings' order."""
        return [target for target in job.targets.values() if not self.written.get(target)]

    def failed(self, job: Job) -> bool:
        """Whether the run failed: a metric stopped, a series carried no volume, or a target got nothing."""
        return bool(self.stopped or self.shapeless or self.missing(job))


def _spans(start: dt.datetime, end: dt.datetime) -> Iterator[tuple[dt.datetime, dt.datetime]]:
    """`start` to `end` in windows of at most `SPAN`."""
    while start < end:
        stop = min(start + SPAN, end)
        yield start, stop
        start = stop


def _minute(at: dt.datetime) -> dt.datetime:
    return at.replace(second=0, microsecond=0)


def _streams(client: Any, job: Job, metric: str, start: dt.datetime, end: dt.datetime) -> list[Any]:
    """OCI's series of `metric` across the compartment over `start` to `end`, one per volume."""
    details = oci.monitoring.models.SummarizeMetricsDataDetails(
        namespace=job.namespace,
        query=f'{metric}[{RESOLUTION}].groupBy({RESOURCE_ID}).{METRICS[metric]}()',
        start_time=start,
        end_time=end,
        resolution=RESOLUTION,
    )
    response = client.summarize_metrics_data(compartment_id=job.compartment, summarize_metrics_data_details=details)
    return list(response.data)


def pull(job: Job, *, client: Any, stores: Stores, now: dt.datetime) -> Pulled:
    """Every metric of `METRICS` for every target, from its newest stored sample to `now`, written to the stores."""
    pulled = Pulled()
    for metric in METRICS:
        _pull_metric(job, metric, client=client, stores=stores, now=now, pulled=pulled)
    return pulled


def _pull_metric(job: Job, metric: str, *, client: Any, stores: Stores, now: dt.datetime, pulled: Pulled) -> None:
    """One metric, a span at a time, each span written before the next is asked for; the first error ends it."""
    end = _minute(now)
    # The oldest minute a run asks for, inside the data OCI still holds at
    # one-minute resolution.
    floor = _minute(now - BACKFILL) + _STEP
    series = f'{job.namespace}_{metric}'
    log.info('reading the newest %s the history store holds', series)
    try:
        newest = stores.newest(series, now)
    except IO_ERRORS as exc:
        _stop(pulled, metric, f'reading the newest {series} the history store holds', exc)
        return
    # Per target, the newest sample already stored: what a sample has to be
    # newer than to be written.
    since = {target: max(newest.get(target, floor - _STEP), floor - _STEP) for target in job.targets.values()}
    for span_start, span_end in _spans(min(since.values()) + _STEP, end):
        where = f'{span_start.isoformat()} to {span_end.isoformat()}'
        log.info('asking OCI for %s from %s', metric, where)
        try:
            streams = _streams(client, job, metric, span_start, span_end)
        except IO_ERRORS as exc:
            _stop(pulled, metric, f'asking OCI for {where}', exc)
            return
        log.info('OCI returned %d series of %s', len(streams), metric)
        samples: dict[Target, dict[int, float]] = {}
        for stream in streams:
            dimensions = cast('Mapping[str, str]', stream.dimensions or {})
            ocid = dimensions.get(RESOURCE_ID)
            if ocid is None:
                if metric not in pulled.shapeless:
                    log.error(
                        'a series of %s carries no %s, only the dimensions %s: the answer is not the shape the query '
                        'asks for',
                        metric,
                        RESOURCE_ID,
                        sorted(dimensions) or 'none',
                    )
                pulled.shapeless.setdefault(metric, sorted(dimensions))
                continue
            target = job.targets.get(ocid)
            if target is None:
                if ocid not in pulled.skipped:
                    log.warning('skipping the series of %s, a volume the census does not declare', ocid)
                pulled.skipped.add(ocid)
                continue
            for point in stream.aggregated_datapoints or ():
                stamp = cast('dt.datetime', point.timestamp)
                if stamp > since[target]:
                    samples.setdefault(target, {})[int(stamp.timestamp() * 1000)] = float(point.value)
        if samples:
            log.info('writing %d samples of %s to vmagent', sum(len(it) for it in samples.values()), series)
            try:
                stores.write([_line(series, target, points) for target, points in samples.items()])
            except IO_ERRORS as exc:
                _stop(pulled, metric, f'writing the samples of {where}', exc)
                return
        for target, points in samples.items():
            pulled.written[target] = pulled.written.get(target, 0) + len(points)


def _stop(pulled: Pulled, metric: str, step: str, exc: Exception) -> None:
    """Record and log that `metric` stopped at `step`, on `exc`; the run goes on to the next metric."""
    pulled.stopped[metric] = f'{step}: {exc!r}'
    log.error('%s stopped %s, and nothing later of it is asked for in this run: %r', metric, step, exc)


def _line(series: str, target: Target, points: Mapping[int, float]) -> str:
    stamps = sorted(points)
    return json.dumps(
        {
            'metric': {'__name__': series, 'node': target.node, 'volume': target.volume},
            'values': [points[it] for it in stamps],
            'timestamps': stamps,
        }
    )


def main(argv: Sequence[str] | None = None, *, now: dt.datetime | None = None) -> int:
    """One run: the settings file and the credential directory named on the command line, exit 1 on a missing volume."""
    parser = argparse.ArgumentParser(description="Pull OCI's block-volume series into the metrics stores, once.")
    parser.add_argument('settings', type=Path, help='the run settings, a JSON file')
    parser.add_argument('credentials', type=Path, help='the directory the sealed key opens into')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', stream=sys.stderr)
    job = Job.read(cast('Path', args.settings), credentials=cast('Path', args.credentials))
    log.info('pulling %d volumes of %s in %s', len(job.targets), job.namespace, job.compartment)
    pulled = pull(job, client=client_for(job), stores=Stores(job), now=now or dt.datetime.now(dt.UTC))
    for target in pulled.missing(job):
        log.error('no sample of %s on %s in the window: OCI returned no series of its OCID', target.volume, target.node)
    log.info(
        'wrote %d samples; skipped %d series of undeclared volumes', sum(pulled.written.values()), len(pulled.skipped)
    )
    return 1 if pulled.failed(job) else 0


if __name__ == '__main__':
    sys.exit(main())
