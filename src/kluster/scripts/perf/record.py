"""`perf record`: the control planes sampled every few seconds, into a file, from start until stop.

**A round** starts, on every control plane at once, two reads: through Talos,
the node's hostname and then every file of `procfs.FILES` at once; and through the API
server's node proxy, etcd's metrics. What answered is appended to the file,
each sample stamped with the moment its call answered (`exposition.line`),
and the file is flushed, so what was recorded survives the recorder being
killed. Rounds start `INTERVAL` apart, and one whose reads have not all
answered by then ends without them.

**A read that does not answer costs its own rounds and nothing else.** A read
still under way when a round ends is not started again until it has
answered, which its call bound (`cluster.CALL_TIMEOUT`) limits, and what it
returns then is recorded with its own time. Every other read keeps its
cadence. Before the API server answers, in the first minutes of a bring-up,
every etcd read fails and the files of each node are recorded alone; a node
that is being replaced answers nothing until it is back. A source's failure
is logged when it starts and its recovery when it ends, not at every round,
so a long outage is two lines.

**A node's files are recorded only from the node itself.** The addresses
are read once, when the recorder starts, and a node created since can hold
an address another node had. Talos sets every node's hostname to its name,
so a node whose hostname is not the name it was asked by has its files left
out, and that is logged once.

**It stops when asked**: at SIGINT or SIGTERM, after the reads under way have
answered.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import logging
import signal
import threading
import time
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from kluster.lib.monitoring.jobs import NODE_EXPORTER_JOB
from kluster.scripts.perf import exposition, procfs
from kluster.scripts.perf.cluster import CALL_TIMEOUT, Api, Node, Talos, etcd_metrics

__all__ = ('ETCD', 'FILES', 'HOSTNAME', 'INTERVAL', 'Recorded', 'Recorder', 'appending', 'record', 'stopping')

log = logging.getLogger(__name__)

#: How often a round starts: the design's 5 s (Aetf/kluster-ops#790 §4.5),
#: six samples to the cluster's one scrape.
INTERVAL = dt.timedelta(seconds=5)

#: The names a node's two reads go by, in the log and in the record of what is under way.
ETCD = 'etcd'
FILES = 'files'

#: The file Talos serves a node's hostname from.
HOSTNAME = '/proc/sys/kernel/hostname'

#: What one read returns: its lines, and each of its sources that failed, by name.
_Read = tuple[list[str], dict[str, Exception]]


class _Elsewhere(Exception):
    """An address that answers as another node than the one it was read for."""


@dataclass
class Recorded:
    """What a recording wrote: its rounds, and its lines per node."""

    rounds: int = 0
    lines: dict[str, int] = field(default_factory=dict[str, int])


@dataclass
class Recorder:
    """The sources a round reads, what it keeps of etcd's exposition, and the reads under way."""

    nodes: Sequence[Node]
    talos: Talos
    api: Api
    keep: frozenset[str]
    clock: Callable[[], float] = time.time
    settle: Callable[[Collection[Future[_Read]], float], object] = lambda reads, timeout: concurrent.futures.wait(
        reads, timeout=timeout
    )
    """How a round waits for its reads to answer, up to the seconds it is given."""
    failing: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    under_way: dict[tuple[str, str], Future[_Read]] = field(default_factory=dict[tuple[str, str], Future[_Read]])

    def _file(self, node: Node, path: str) -> tuple[str, int]:
        """One file of `node`, and the moment it answered."""
        text = self.talos.read(node.address, path)
        return text, round(self.clock() * 1000)

    def _files(self, node: Node) -> _Read:
        """The node's hostname, and then, where it is the node's own name, every file at once."""
        hostname = self.talos.read(node.address, HOSTNAME).strip()
        if hostname != node.name:
            return [], {'hostname': _Elsewhere(f'{node.address} answers as {hostname!r}, so its files are left out')}
        lines: list[str] = []
        failed: dict[str, Exception] = {}
        with ThreadPoolExecutor(max_workers=len(procfs.FILES), thread_name_prefix='perf-files') as files:
            reads = {path: files.submit(self._file, node, path) for path in procfs.FILES}
            for path, read in reads.items():
                try:
                    text, at_ms = read.result()
                except Exception as exc:  # noqa: BLE001 -- a file that does not answer is that file's round lost, logged
                    failed[path] = exc
                    continue
                lines.extend(
                    exposition.line(sample, job=NODE_EXPORTER_JOB, node=node.name, at_ms=at_ms)
                    for sample in procfs.FILES[path](text)
                )
        return lines, failed

    def _etcd(self, node: Node) -> _Read:
        text = self.api.get(etcd_metrics(node.name)).decode()
        at_ms = round(self.clock() * 1000)
        return list(exposition.etcd_lines(text, keep=self.keep, node=node.name, at_ms=at_ms)), {}

    def start(self, pool: ThreadPoolExecutor) -> None:
        """Start each read of every node that is not under way already."""
        for node in self.nodes:
            for source, read in ((FILES, self._files), (ETCD, self._etcd)):
                if (node.name, source) not in self.under_way:
                    self.under_way[(node.name, source)] = pool.submit(read, node)

    def _logged(self, node: str, source: str, failure: Exception | None) -> None:
        if failure is not None and (node, source) not in self.failing:
            self.failing.add((node, source))
            log.warning('%s on %s is not recorded until this ends: %s', source, node, failure)
        elif failure is None and (node, source) in self.failing:
            self.failing.discard((node, source))
            log.info('%s on %s is recorded again', source, node)

    def collect(self, out: TextIO, recorded: Recorded, *, timeout: float) -> None:
        """Wait up to `timeout` for the reads under way, and append what answered to `out`."""
        _ = self.settle(list(self.under_way.values()), timeout)
        for (node, source), future in list(self.under_way.items()):
            if not future.done():
                continue
            del self.under_way[(node, source)]
            try:
                lines, failed = future.result()
            except Exception as exc:  # noqa: BLE001 -- any failure of one read is that read's round lost, logged
                self._logged(node, source, exc)
                continue
            self._logged(node, source, None)
            if source == FILES:
                for name in ('hostname', *procfs.FILES):
                    self._logged(node, name, failed.get(name))
            if lines:
                _ = out.write(''.join(f'{text}\n' for text in lines))
                recorded.lines[node] = recorded.lines.get(node, 0) + len(lines)
        out.flush()


def record(
    recorder: Recorder, out: TextIO, *, wait: Callable[[float], bool], interval: dt.timedelta = INTERVAL
) -> Recorded:
    """Rounds into `out`, `interval` apart, until `wait` says to stop; then what is under way, once it answers.

    `wait(seconds)` waits until the next round is due, and returns `True`
    where the recording is to stop instead: a `threading.Event`'s `wait`.
    """
    recorded = Recorded()
    period = interval.total_seconds()
    # Two of each read at most: one under way past its round, and the next round's.
    with ThreadPoolExecutor(max_workers=max(4 * len(recorder.nodes), 1), thread_name_prefix='perf-record') as pool:
        while True:
            started = recorder.clock()
            recorder.start(pool)
            recorder.collect(out, recorded, timeout=period)
            recorded.rounds += 1
            if wait(max(0.0, started + period - recorder.clock())):
                break
        # The hostname, and then the files at once: two calls, each within its bound.
        recorder.collect(out, recorded, timeout=2 * CALL_TIMEOUT)
    return recorded


def appending(path: Path) -> TextIO:
    """`path` opened to append rounds to, less a last line a recorder killed mid-write left cut off.

    The cut-off line is a sample lost; leaving it would join the next round's
    first line onto it, or leave a line no import reads in the middle of the
    file.
    """
    if path.exists():
        with path.open('r+b') as written:
            size = written.seek(0, 2)
            # A line is far shorter than this; what precedes the last line end is whole.
            start = written.seek(max(0, size - 2**16))
            kept = start + written.read().rfind(b'\n') + 1
            if kept != size:
                log.warning('%s ends in a line cut off partway through its write, which is dropped', path)
                _ = written.truncate(kept)
    return path.open('a')


def stopping() -> threading.Event:
    """An event set by SIGINT or SIGTERM, which the recording's `wait` waits on."""
    stop = threading.Event()

    def handler(signum: int, _frame: object) -> None:
        log.info('%s received: stopping once the reads under way have answered', signal.Signals(signum).name)
        stop.set()

    _ = signal.signal(signal.SIGINT, handler)
    _ = signal.signal(signal.SIGTERM, handler)
    return stop
