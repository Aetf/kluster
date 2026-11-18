"""The recorder's file: Prometheus exposition, one sample a line, each labeled with its job and node and timestamped.

**A line is `<name>{job="…",node="…",source="peak-recorder",<own labels>} <value> <ms>`**,
the form VictoriaMetrics' `/api/v1/import/prometheus` reads. Its `job` is the
one the cluster's own scrape gives the same series (`kluster.lib.monitoring.jobs`)
and its `node` the node it was read on, so every reading
(`kluster.lib.monitoring.readings`), which selects a series by its job and
groups it by node, reads a recorded period as it reads a scraped one. It
carries no `cluster` label, which marks the legacy cluster's series, and its
`source` tells it apart from the series the cluster scraped. The three
labels lead, in that order, so the import reads a line's job and node and its
timestamp without parsing the labels its source gave it, and it adds the
fourth that writes each region of the recording to a series of its own
(`REGION_LABEL`) after them.

**etcd's own exposition is kept by name** (`etcd_families`): the families a
reading reads from etcd's job, read off the readings' expressions by the walk
the component's rules read them with (`kluster.lib.monitoring.selectors`), and the
ones the controlled reproduction of Aetf/kluster-ops#785 §5 sampled beside
them. The rest of etcd's families, which no reading reads, would make the
file several times larger for nothing. A kept line is written with the three
labels put ahead of its own and the time it was read appended: etcd's
exposition carries no timestamps of its own.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from kluster.lib.monitoring.jobs import ETCD_JOB
from kluster.lib.monitoring.readings import Reading
from kluster.lib.monitoring.selectors import selectors
from kluster.scripts.perf.procfs import Sample

log = logging.getLogger(__name__)

__all__ = (
    'ETCD_PROBE',
    'REGION_LABEL',
    'SAMPLER_FAMILIES',
    'SOURCE',
    'SOURCE_LABEL',
    'NotRecorded',
    'Recorded',
    'etcd_families',
    'etcd_lines',
    'in_region',
    'line',
    'parse',
)

#: The label a recorded series carries, and its value.
SOURCE_LABEL = 'source'
SOURCE = 'peak-recorder'

#: The label the import gives each stretch of a recorded series it writes
#: between two of the cluster's own (`perf.history`): when the stretch starts,
#: which makes each such stretch a series of its own.
REGION_LABEL = 'recorded_from'

#: The families of etcd's the controlled reproduction sampled beside those a
#: reading reads (Aetf/kluster-ops#785 §5): whether a member leads, whether it
#: has a leader, and the applies it took too long over.
SAMPLER_FAMILIES = ('etcd_server_has_leader', 'etcd_server_is_leader', 'etcd_server_slow_apply_total')

#: The etcd series the import reads to find where the cluster scraped a member
#: (`perf.history`), kept whatever the readings read.
ETCD_PROBE = 'process_resident_memory_bytes'

_NAME = re.compile(r'[a-zA-Z_:][a-zA-Z0-9_:]*')


def etcd_families(readings: Sequence[Reading]) -> frozenset[str]:
    """The names of etcd's series the recorder keeps: every one a reading selects from etcd's job, and the sampler's.

    A name a reading reads from etcd's job that etcd does not export, such as
    the `up` vmagent writes for its scrape, selects nothing from etcd's
    exposition and so keeps nothing. A reading the walk cannot read whole is
    refused there (`kluster.lib.monitoring.selectors`).
    """
    read = {
        selector.metric
        for reading in readings
        for selector in selectors(reading.expr)
        if selector.jobs is not None and ETCD_JOB in selector.jobs
    }
    return frozenset({*read, *SAMPLER_FAMILIES, ETCD_PROBE})


def _escaped(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _leading(job: str, node: str) -> str:
    return f'job="{_escaped(job)}",node="{_escaped(node)}",{SOURCE_LABEL}="{SOURCE}"'


def line(sample: Sample, *, job: str, node: str, at_ms: int) -> str:
    """One recorded sample of `job` on `node`, read at `at_ms`."""
    labels = ''.join(f',{name}="{_escaped(value)}"' for name, value in sample.labels)
    return f'{sample.name}{{{_leading(job, node)}{labels}}} {sample.value!r} {at_ms}'


def etcd_lines(exposition: str, *, keep: frozenset[str], node: str, at_ms: int) -> Iterator[str]:
    """etcd's exposition from `node`, read at `at_ms`, as recorded lines: the families of `keep`, and nothing else."""
    leading = _leading(ETCD_JOB, node)
    for raw in exposition.splitlines():
        named = _NAME.match(raw)
        if named is None or named[0] not in keep:
            continue
        rest = raw[named.end() :]
        if rest.startswith('{}'):
            rest = rest[2:]
        # Its own labels follow the three, or the three close the set where it has none.
        own = f',{rest[1:]}' if rest.startswith('{') else f'}}{rest}'
        yield f'{named[0]}{{{leading}{own.rstrip()} {at_ms}'


@dataclass(frozen=True)
class Recorded:
    """What the import needs of a recorded line: whose it is, and when it was read."""

    job: str
    node: str
    at_ms: int


_LEADING = re.compile(rf'^[a-zA-Z_:][a-zA-Z0-9_:]*\{{job="([^"\\]*)",node="([^"\\]*)",{SOURCE_LABEL}="{SOURCE}"[,}}]')


class NotRecorded(ValueError):
    """A line of a file that is not one `perf record` writes."""


def parse(lines: Iterable[str]) -> Iterator[tuple[str, Recorded]]:
    """Each non-empty line of a recorded file, as read with its line ends, with its job, its node and its time.

    `NotRecorded` at the first line that is none of those, but for a last
    line with no line end that does not read whole: the line a recorder
    killed partway through its write leaves, which is skipped and logged.
    """
    for number, raw in enumerate(lines, start=1):
        text = raw.rstrip('\n')
        if not text:
            continue
        leading = _LEADING.match(text)
        stamp = text.rsplit(' ', 1)[-1]
        if leading is None or not stamp.isdigit():
            if not raw.endswith('\n'):
                log.warning('line %d, the last, was cut off partway through its write, and is skipped', number)
                return
            raise NotRecorded(
                f'line {number} is not a line `perf record` writes: it does not open with its job, its node and its '
                'source, or does not end with a timestamp'
            )
        yield text, Recorded(job=leading[1], node=leading[2], at_ms=int(stamp))


def in_region(text: str, region: str) -> str:
    """A recorded line as the series of the region `region` (`REGION_LABEL`), its label after the three that lead."""
    source = f'{SOURCE_LABEL}="{SOURCE}"'
    return text.replace(source, f'{source},{REGION_LABEL}="{_escaped(region)}"', 1)
