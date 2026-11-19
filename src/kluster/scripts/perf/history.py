"""`perf import`: a recording written into the history store, beside the cluster's own series and never on top of them.

**Where the cluster scraped, the recording is left out.** The recorder runs
from before a bring-up or a rebuild until after it, so for part of its run
the cluster's own scrapes of the same nodes are in the history too. The
recorded series are series of their own (their `source`, and none of the
scrape's target labels), and a reading that sums a node's series would add
the two. So for each node and job in the file, the import reads the times of
the cluster's own samples of that job's probe series on that node (`PROBES`):
a scrape returns every series of its target at once, so one series of the job
stands for all of them. Two of those samples no more than `STRETCH_GAP` apart
bracket a stretch of the cluster's scrape, missed scrapes inside it included,
and a recorded line inside a stretch is left out. Every other line is
written, up to a stretch's first sample and from its last, so where a
recording meets a scrape neither series holds at most one round.

**Each region of the recording is a series of its own.** The lines written
between two of the cluster's stretches are a region, labeled with the time it
starts (`exposition.REGION_LABEL`). A region that resumed the series written
before a stretch would read, in a reading's `increase`, as the counter's
growth across the whole stretch, which the cluster's series counts already;
a series that starts after the stretch counts only its own growth. The
cluster's own series can resume too, under the labels it had, after a gap
the recording filled, and a range query reading across that gap counts the
gap's events twice in the first window after the resume; that is accepted,
and operations.md §8.5 says so.

**What an earlier import wrote is left out**: a line within `ROUND` of an
earlier import's sample of its probe. So a second import of a file writes
nothing, and an import cut off partway writes the rest when run again, but
for at most a round where it stopped, once the store has made the earlier
import's writes searchable, which it does within seconds of taking them. A
region's label depends only on the file and the cluster's own samples, so the
rest of a region joins the series its first part was written to.

**Every read of the store is one it has not answered before.** The store
caches the series each selector found over each range of days, and drops
those answers only on a periodic reset some seconds after new series become
searchable, a reset `/internal/force_flush` does not bring forward once the
store has flushed the new series on its own. Each import asks the same
selectors as the import before it, which asked them before it wrote, so a
second import run inside that interval would be answered that nothing is
there and write the file again. A read therefore matches a label no series
carries against a value of its own (`_UNCACHED`): a selector no earlier read
asked, so the store looks it up in what it holds.

**vmagent's queue is drained first.** A store that waited in `Init` has had
its scrapes queued by vmagent meanwhile, and they reach it after it starts.
The cluster's samples the import reads are therefore complete only once
vmagent holds nothing queued, and the import refuses while its pending data
is more than `QUEUE_SLACK`.

**It writes straight to the history store**, `/api/v1/import/prometheus`
through the API server's service proxy, in chunks of `CHUNK` lines, past the
relabeling vmagent applies to what it writes there: the recorder writes only
series of the history's class.
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
import logging
import urllib.parse
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from kluster import conventions
from kluster.lib.monitoring.jobs import ETCD_JOB, NODE_EXPORTER_JOB, SCRAPE_INTERVAL
from kluster.lib.monitoring.readings import series
from kluster.scripts.perf import exposition
from kluster.scripts.perf.cluster import CALL_TIMEOUT, Api, history
from kluster.scripts.perf.record import INTERVAL

__all__ = (
    'CHUNK',
    'PROBES',
    'QUEUE_SLACK',
    'ROUND',
    'STRETCH_GAP',
    'Imported',
    'QueueNotDrained',
    'import_file',
    'vmagent_pending',
)

log = logging.getLogger(__name__)

#: Each job's probe: a series its every scrape returns and the recorder
#: records, whose samples say when the job's target was scraped.
PROBES: Mapping[str, str] = {NODE_EXPORTER_JOB: 'node_load1', ETCD_JOB: exposition.ETCD_PROBE}

#: How far apart two of the cluster's samples may lie and still bracket one
#: stretch of its scrape: a few scrape intervals, so a scrape or two missed
#: inside a stretch leave it whole.
STRETCH_GAP = 4 * SCRAPE_INTERVAL

#: How near an earlier import's sample a line counts as written already: a
#: round of the recorder, its interval and the bound of a call, which is
#: further apart than any two of the lines one round stamps.
ROUND = INTERVAL + dt.timedelta(seconds=CALL_TIMEOUT)

#: Lines per request to the store.
CHUNK = 20_000

#: How much vmagent may hold queued for the stores while the import runs: what
#: it is about to send of the last moments' scrapes. A store that waited in
#: `Init` leaves far more queued than this.
QUEUE_SLACK = 2**20

#: A label no series carries, which every read of the store matches against a
#: value of its own, so that no cached answer to an earlier read is reused.
_UNCACHED = 'perf_import_read'

#: The labels the VictoriaMetrics operator selects a VMAgent's pods and
#: Service by, of which the namespace holds one.
_VMAGENT = 'app.kubernetes.io/name=vmagent'
_PENDING = 'vmagent_remotewrite_pending_data_bytes'


class QueueNotDrained(RuntimeError):
    """vmagent still holds data queued for the stores, so the history does not yet hold all the cluster scraped."""


@dataclass
class Imported:
    """What an import wrote and left out, per node and job."""

    written: dict[tuple[str, str], int] = field(default_factory=dict[tuple[str, str], int])
    scraped: dict[tuple[str, str], int] = field(default_factory=dict[tuple[str, str], int])
    """Left out as inside a stretch of the cluster's own scrape."""
    imported: dict[tuple[str, str], int] = field(default_factory=dict[tuple[str, str], int])
    """Left out as written by an earlier import."""


def vmagent_pending(api: Api) -> float:
    """The bytes vmagent holds queued for every store it writes to, read off its own metrics."""
    namespace = conventions.MONITORING_NAMESPACE
    query = urllib.parse.urlencode({'labelSelector': _VMAGENT})
    found = json.loads(api.get(f'/api/v1/namespaces/{namespace}/services?{query}'))
    services = found.get('items', [])
    if len(services) != 1:
        raise QueueNotDrained(f'{len(services)} Services in {namespace} carry {_VMAGENT}, where vmagent has one')
    (service,) = services
    ports = service['spec']['ports']
    port = next((port['port'] for port in ports if port.get('name') == 'http'), ports[0]['port'])
    metrics = api.get(f'/api/v1/namespaces/{namespace}/services/{service["metadata"]["name"]}:{port}/proxy/metrics')
    pending = [
        float(line.rsplit(' ', 1)[-1])
        for line in metrics.decode().splitlines()
        if line.startswith((f'{_PENDING}{{', f'{_PENDING} '))
    ]
    if not pending:
        raise QueueNotDrained(f'vmagent reports no {_PENDING}, so whether its queue is drained is unknown')
    return sum(pending)


def _lines(path: Path) -> Iterator[tuple[str, exposition.Recorded]]:
    with path.open() as text:
        yield from exposition.parse(text)


def _spans(path: Path) -> dict[tuple[str, str], tuple[int, int]]:
    """Each node and job the file records, with its first and last time."""
    spans: dict[tuple[str, str], tuple[int, int]] = {}
    for _, recorded in _lines(path):
        key = (recorded.node, recorded.job)
        first, last = spans.get(key, (recorded.at_ms, recorded.at_ms))
        spans[key] = (min(first, recorded.at_ms), max(last, recorded.at_ms))
    return spans


def _ms(span: dt.timedelta) -> int:
    return int(span.total_seconds() * 1000)


def _probed(api: Api, node: str, job: str, source: str, span: tuple[int, int]) -> list[int]:
    """The times, sorted, of the history's samples of `job`'s probe on `node` from `source` within `span`, widened."""
    margin = max(_ms(STRETCH_GAP), _ms(ROUND))
    selector = series(
        PROBES[job],
        f'node="{node}"',
        f'{exposition.SOURCE_LABEL}="{source}"',
        f'{_UNCACHED}!="{uuid.uuid4().hex}"',
        job=job,
    )
    first, last = span
    query = urllib.parse.urlencode(
        {'match[]': selector, 'start': (first - margin) / 1000, 'end': (last + margin) / 1000}
    )
    times: list[int] = []
    for row in api.get(history(f'/api/v1/export?{query}')).decode().splitlines():
        if row.strip():
            times.extend(int(at) for at in json.loads(row)['timestamps'])
    return sorted(times)


@dataclass(frozen=True)
class _Judged:
    """The history's samples a node's job is judged against: the cluster's own, and an earlier import's."""

    scraped: list[int]
    imported: list[int]

    def inside(self, at_ms: int) -> bool:
        """Whether `at_ms` lies in a stretch of the cluster's scrape: between two of its samples `STRETCH_GAP` apart at most."""
        after = bisect.bisect_left(self.scraped, at_ms)
        if after < len(self.scraped) and self.scraped[after] == at_ms:
            return True
        return 0 < after < len(self.scraped) and self.scraped[after] - self.scraped[after - 1] <= _ms(STRETCH_GAP)

    def preceding(self, at_ms: int) -> int | None:
        """The cluster's last sample before `at_ms`, which ends the stretch a region after it follows."""
        before = bisect.bisect_left(self.scraped, at_ms)
        return self.scraped[before - 1] if before else None

    def written(self, at_ms: int) -> bool:
        """Whether an earlier import wrote a sample within `ROUND` of `at_ms`."""
        index = bisect.bisect_left(self.imported, at_ms - _ms(ROUND))
        return index < len(self.imported) and self.imported[index] <= at_ms + _ms(ROUND)


def _regions(path: Path, judged: Mapping[tuple[str, str], _Judged]) -> dict[tuple[str, str, int | None], str]:
    """Each region's label: the time its first line was read, by its node, its job and the stretch it follows."""
    first: dict[tuple[str, str, int | None], int] = {}
    for _, recorded in _lines(path):
        key = (recorded.node, recorded.job)
        if judged[key].inside(recorded.at_ms):
            continue
        region = (*key, judged[key].preceding(recorded.at_ms))
        first[region] = min(first.get(region, recorded.at_ms), recorded.at_ms)
    return {
        region: dt.datetime.fromtimestamp(at_ms / 1000, tz=dt.UTC).isoformat(timespec='milliseconds')
        for region, at_ms in first.items()
    }


def import_file(path: Path, api: Api) -> Imported:
    """Write the recording at `path` into the history store, beside the cluster's own series and an earlier import's."""
    log.info("reading vmagent's queue for the stores")
    pending = vmagent_pending(api)
    if pending > QUEUE_SLACK:
        raise QueueNotDrained(
            f'vmagent holds {pending:.0f} bytes queued for the stores, more than the {QUEUE_SLACK} it sends any '
            'moment; the history does not yet hold all the cluster scraped, so run the import again once it is drained'
        )
    spans = _spans(path)
    unknown = sorted({job for _, job in spans} - set(PROBES))
    if unknown:
        raise exposition.NotRecorded(f'{path} records jobs `perf import` has no probe for: {", ".join(unknown)}')
    judged: dict[tuple[str, str], _Judged] = {}
    for (node, job), span in sorted(spans.items()):
        log.info('reading where the history store already holds %s on %s', job, node)
        judged[(node, job)] = _Judged(
            scraped=_probed(api, node, job, '', span), imported=_probed(api, node, job, exposition.SOURCE, span)
        )
    regions = _regions(path, judged)
    imported = Imported()
    chunk: list[str] = []
    log.info('writing %s into the history store, %d lines at a time', path, CHUNK)
    for text, recorded in _lines(path):
        key = (recorded.node, recorded.job)
        if judged[key].inside(recorded.at_ms):
            imported.scraped[key] = imported.scraped.get(key, 0) + 1
            continue
        if judged[key].written(recorded.at_ms):
            imported.imported[key] = imported.imported.get(key, 0) + 1
            continue
        chunk.append(exposition.in_region(text, regions[(*key, judged[key].preceding(recorded.at_ms))]))
        imported.written[key] = imported.written.get(key, 0) + 1
        if len(chunk) == CHUNK:
            api.post(history('/api/v1/import/prometheus'), '\n'.join(chunk).encode())
            chunk = []
    if chunk:
        api.post(history('/api/v1/import/prometheus'), '\n'.join(chunk).encode())
    return imported
