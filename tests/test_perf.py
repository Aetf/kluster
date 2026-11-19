"""`perf record` and `perf import`, against the pinned VictoriaMetrics and the readings it is read by.

**The recorder is driven by stand-ins for Talos and the API server**, which
answer with a control plane's `/proc` files and etcd's exposition as each
would read at the moment asked, from counters that grow at known rates. Its
clock is the stand-ins' clock, so a recording of most of an hour takes as
long as its rounds do to run.

**The import goes to a real store, through the real `kubectl`.** The store
is the history store's own binary, read out of the image `Pulumi.yaml` pins
for it (`versions:image-metrics-history`, `image_busybox.copied`) and run on
the host on a port it picks itself, so no port is published. `kubectl`
reaches it through a stand-in for the API server that serves the history
store's service-proxy path, forwarding it to the store as the API server
does, and vmagent's Service and metrics.

What the import wrote is judged by the readings (`kluster.lib.monitoring.readings`),
each evaluated by the store from its own expression. The readings a
recording must feed are named by the files the design records
(Aetf/kluster-ops#790 §4.5) and the etcd families, never by what a run
happened to write. **Where a recording meets the cluster's own scrape**, the
cluster's series of cp1 are written as its scrape writes them: a stretch
with two scrapes missed inside it, ending before the recording does, and a
leader change just before the stretch starts.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import http.client
import http.server
import json
import logging
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import image_busybox
import process_sessions
import psutil
import pytest

from kluster import conventions
from kluster.components.monitoring import stores
from kluster.lib import pulumi_cli, stack_addresses, stack_environment, workstation
from kluster.lib.monitoring.jobs import ETCD_JOB, NODE_EXPORTER_JOB, SCRAPE_INTERVAL
from kluster.lib.monitoring.readings import READINGS, Reading
from kluster.lib.monitoring.selectors import selectors
from kluster.scripts.perf import cluster, exposition, history, procfs, record

#: How long the store may take to listen and answer `/health`: a stop-loss.
STORE_TIMEOUT = 30.0
#: The bound of each `kubectl` call the import makes: a stop-loss.
KUBECTL_TIMEOUT = 60.0

#: The store's own binary in its image.
BINARY = '/victoria-metrics-prod'
#: The store's count of the index entries it holds in memory, not yet searchable.
PENDING_INDEX = 'vm_pending_rows{type="indexdb"}'

NODES = (cluster.Node('cp1', '10.0.0.11'), cluster.Node('cp2', '10.0.0.12'), cluster.Node('cp3', '10.0.0.13'))

#: Where the recording starts: two hours back, on a minute, inside the store's retention.
T0 = (int(time.time()) // 60) * 60 - 2 * 3600
#: How long a recording runs.
SPAN = dt.timedelta(minutes=40)
#: When the readings of a recorded period are read: inside the recording, before any scrape.
READ_AT = T0 + 18 * 60

#: The cluster's own scrape of cp1, in seconds from `T0`: a stretch from
#: `SCRAPED` to `SCRAPED_UNTIL`, with the two scrapes of `MISSED` missed, a
#: gap of three scrape intervals inside it.
SCRAPED = 20 * 60
SCRAPED_UNTIL = 30 * 60
MISSED = (24 * 60 + 30, 25 * 60)
#: When every member's leader changes: a quarter-minute before the stretch.
LEADER_CHANGED = SCRAPED - 15

#: The rates the stand-ins' counters grow at, per second, and the slow WAL
#: fsyncs, one a minute, each half a minute past it.
CPU_WAITING = 0.25
IO_WAITING = 0.05
WRITES = 100.0
SECTORS_PER_WRITE = 16
FSYNCS = 10.0

#: etcd's histogram buckets: doubling from a millisecond, as its WAL's are.
BUCKETS = tuple(0.001 * 2**power for power in range(11))

#: The node exporter's families of the files the design records (§4.5), each
#: by the prefix its collector names them with: pressure, disk statistics,
#: memory and load.
RECORDED_FILES_FAMILIES = re.compile(r'node_(pressure|disk|memory|load)_?.*')


# -- The stand-ins ------------------------------------------------------------------


def _slow(seconds: float) -> int:
    """The slow fsyncs by `seconds`: one at every minute and a half-minute."""
    return int((seconds + 30) // 60)


def _histogram(name: str, labels: str, count: float, duration: float, slow: int = 0) -> list[str]:
    """`count` observations of `duration` each and `slow` of two seconds, as etcd writes a histogram."""
    prefix = f'{labels},' if labels else ''
    lines = [f'{name}_bucket{{{prefix}le="{bound:g}"}} {count if duration <= bound else 0:g}' for bound in BUCKETS]
    return [
        f'# TYPE {name} histogram',
        *lines,
        f'{name}_bucket{{{prefix}le="+Inf"}} {count + slow:g}',
        f'{name}_sum{{{labels}}} {count * duration + 2 * slow:g}',
        f'{name}_count{{{labels}}} {count + slow:g}',
    ]


def _member_id(node: cluster.Node) -> str:
    return f'{NODES.index(node) + 1:x}' * 16


def etcd_exposition(node: cluster.Node, seconds: float) -> str:
    """What `node`'s etcd serves `seconds` into the recording: the families the readings read, and some they do not."""
    fsyncs = FSYNCS * seconds
    others = [peer for peer in NODES if peer != node]
    lines = [
        '# HELP etcd_disk_wal_fsync_duration_seconds The latency distributions of fsync called by WAL.',
        *_histogram('etcd_disk_wal_fsync_duration_seconds', '', fsyncs, 0.003, _slow(seconds)),
        *_histogram('etcd_disk_backend_commit_duration_seconds', '', fsyncs / 10, 0.006),
        *_histogram('etcd_server_apply_duration_seconds', 'op="Range",success="true",version="v3"', fsyncs, 0.002),
        *(
            line
            for peer in others
            for line in _histogram(
                'etcd_network_peer_round_trip_time_seconds', f'To="{_member_id(peer)}"', seconds / 5, 0.001
            )
        ),
        f'etcd_disk_wal_write_bytes_total {1000 * seconds:g}',
        f'etcd_server_leader_changes_seen_total {int(seconds >= LEADER_CHANGED)}',
        'etcd_server_heartbeat_send_failures_total 0',
        'etcd_server_proposals_failed_total 0',
        'etcd_server_slow_apply_total 0',
        'etcd_server_has_leader 1',
        f'etcd_server_is_leader {int(node == NODES[0])}',
        f'etcd_server_id{{server_id="{_member_id(node)}"}} 1',
        *(f'etcd_network_active_peers{{Local="{_member_id(node)}",Remote="{_member_id(peer)}"}} 1' for peer in others),
        f'process_cpu_seconds_total {0.1 * seconds:g}',
        'process_resident_memory_bytes 1.5e+08',
        # Families no reading reads: left out of the recording.
        'etcd_server_version{server_version="3.6.12"} 1',
        'etcd_debugging_mvcc_keys_total 300',
        'grpc_server_handled_total{grpc_code="OK",grpc_method="Range"} 5',
        'go_goroutines 42',
    ]
    return '\n'.join(lines) + '\n'


def proc_file(path: str, seconds: float) -> str:
    """What a control plane's `path` reads `seconds` into the recording."""
    match path:
        case '/proc/pressure/cpu':
            return f'some avg10=25.00 avg60=25.00 avg300=25.00 total={int(CPU_WAITING * 1e6 * seconds)}\n'
        case '/proc/pressure/io':
            waiting, stalled = int(IO_WAITING * 1e6 * seconds), int(IO_WAITING * 2e5 * seconds)
            return (
                f'some avg10=5.00 avg60=5.00 avg300=5.00 total={waiting}\n'
                f'full avg10=1.00 avg60=1.00 avg300=1.00 total={stalled}\n'
            )
        case '/proc/pressure/memory':
            return 'some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'
        case '/proc/diskstats':
            writes = int(WRITES * seconds)
            stats = f'500 0 4000 900 {writes} 7 {writes * SECTORS_PER_WRITE} {writes * 2} 1 {writes} {writes * 3}'
            return (
                f'   8       0 sda {stats} 0 0 0 0 0 0\n'
                f'   8       4 sda4 {stats} 0 0 0 0 0 0\n'
                '   7       0 loop0 1 0 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n'
            )
        case '/proc/loadavg':
            return '0.50 0.40 0.30 2/310 4711\n'
        case '/proc/meminfo':
            return (
                'MemTotal:        8000000 kB\nMemAvailable:    4000000 kB\nBuffers:           10000 kB\n'
                'Dirty:               120 kB\nWriteback:             0 kB\n'
            )
        case _:
            raise AssertionError(f'a path the recorder reads nothing of: {path}')


@dataclass
class Clock:
    """The recording's clock, which the recorder's waits move."""

    now: float = float(T0)

    def __call__(self) -> float:
        return self.now


@dataclass
class StandIns:
    """Talos and the API server as the recorder reaches them, at `clock`, each refusing where told to."""

    clock: Clock
    #: Whether a node's source refuses at a moment: (node, source, seconds into the recording).
    refuses: Callable[[str, str, float], bool] = lambda _node, _source, _seconds: False
    #: The node an address answers as at a moment, where it is not the node the address was read for.
    answers_as: Callable[[str, float], str | None] = lambda _node, _seconds: None
    #: Every Talos read, as (node, path).
    reads: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def _seconds(self) -> float:
        return self.clock.now - T0

    def read(self, address: str, path: str) -> str:
        (node,) = [node for node in NODES if node.address == address]
        self.reads.append((node.name, path))
        if self.refuses(node.name, path, self._seconds()):
            raise cluster.Unanswered(f'{node.name} does not answer')
        if path == record.HOSTNAME:
            return f'{self.answers_as(node.name, self._seconds()) or node.name}\n'
        return proc_file(path, self._seconds())

    def get(self, path: str) -> bytes:
        (node,) = [node for node in NODES if cluster.etcd_metrics(node.name) == path]
        if self.refuses(node.name, record.ETCD, self._seconds()):
            raise cluster.Unanswered('the API server does not answer')
        return etcd_exposition(node, self._seconds()).encode()

    def post(self, path: str, body: bytes) -> None:
        raise AssertionError(f'the recorder posts nothing, and posted to {path}')


def _every_read(reads: Collection[Future[Any]], _timeout: float) -> None:
    """A round's wait that ends once every read has answered, under a stop-loss bound, not at the round's real seconds."""
    _, pending = concurrent.futures.wait(reads, timeout=STORE_TIMEOUT)
    assert not pending, f'{len(pending)} reads did not answer within {STORE_TIMEOUT:g}s'


def recorder(stand_ins: StandIns, *, every_read: bool = True) -> record.Recorder:
    """A recorder of `NODES` through `stand_ins`, whose rounds wait for every read where `every_read`, at the stand-ins' clock."""
    recording = record.Recorder(
        nodes=NODES, talos=stand_ins, api=stand_ins, keep=exposition.etcd_families(READINGS), clock=stand_ins.clock
    )
    if every_read:
        recording.settle = _every_read
    return recording


def recording(path: Path, span: dt.timedelta, stand_ins: StandIns) -> int:
    """`span` of rounds from `T0` appended to `path`; the number of rounds."""
    clock = stand_ins.clock
    clock.now = float(T0)
    end = T0 + span.total_seconds()

    def wait(seconds: float) -> bool:
        clock.now += seconds
        return clock.now > end

    with record.appending(path) as out:
        return record.record(recorder(stand_ins), out, wait=wait).rounds


def recorded_lines(path: Path) -> list[tuple[str, exposition.Recorded]]:
    with path.open() as text:
        return list(exposition.parse(text))


# -- The store and the API server ------------------------------------------------------


@dataclass(frozen=True)
class Store:
    """The running store, on the host port it listens at."""

    port: int

    def request(self, method: str, path: str, body: bytes | None = None) -> bytes:
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=STORE_TIMEOUT)
        try:
            connection.request(method, path, body=body)
            response = connection.getresponse()
            answer = response.read()
        finally:
            connection.close()
        assert response.status < 300, f'{method} {path}: {response.status} {answer[:300]!r}'
        return answer

    def flushed(self) -> None:
        """What the store was written made searchable.

        A selector the store answered before the write can still be answered
        from its cache afterwards, until the store's own periodic reset, once
        the store had flushed the new series by itself before this call.
        `perf import` never asks a selector twice for that reason.
        """
        _ = self.request('GET', '/internal/force_flush')

    def indexed(self) -> None:
        """Every series the store was written made searchable by the store's own flusher, within `STORE_TIMEOUT`."""
        deadline = time.monotonic() + STORE_TIMEOUT
        while True:
            metrics = self.request('GET', '/metrics').decode().splitlines()
            (pending,) = [float(line.rsplit(' ', 1)[1]) for line in metrics if line.startswith(f'{PENDING_INDEX} ')]
            if pending == 0:
                return
            assert time.monotonic() < deadline, (
                f'the store held {pending:g} index entries unflushed after {STORE_TIMEOUT:g}s'
            )
            time.sleep(0.05)

    def query(self, expr: str, at: float) -> list[dict[str, Any]]:
        answer = json.loads(self.request('GET', '/api/v1/query?' + urllib.parse.urlencode({'query': expr, 'time': at})))
        assert answer['status'] == 'success', answer
        return answer['data']['result']

    def value(self, expr: str, node: str, at: float) -> float:
        """The value of `expr` on `node` at `at`, out of a range query over the whole recording, as a dashboard asks."""
        query = {'query': expr, 'start': T0, 'end': T0 + SPAN.total_seconds(), 'step': 60}
        answer = json.loads(self.request('GET', '/api/v1/query_range?' + urllib.parse.urlencode(query)))
        assert answer['status'] == 'success', answer
        (row,) = [row for row in answer['data']['result'] if row['metric'].get('node') == node]
        (found,) = [float(value) for stamp, value in row['values'] if float(stamp) == at]
        return found

    def times(self, selector: str) -> list[int]:
        """The time of every sample of `selector`, in milliseconds, repeats kept."""
        query = urllib.parse.urlencode({'match[]': selector, 'start': T0 - 3600, 'end': T0 + 3 * 3600})
        rows = self.request('GET', f'/api/v1/export?{query}').decode().splitlines()
        return sorted(at for row in rows if row.strip() for at in json.loads(row)['timestamps'])


def _listening(pid: int) -> int:
    """The port the process `pid` listens at once it does, within `STORE_TIMEOUT`."""
    process = psutil.Process(pid)
    deadline = time.monotonic() + STORE_TIMEOUT
    while time.monotonic() < deadline:
        ports = [held.laddr.port for held in process.net_connections(kind='tcp') if held.status == psutil.CONN_LISTEN]
        if ports:
            return ports[0]
        time.sleep(0.05)
    raise AssertionError(f'the store did not listen within {STORE_TIMEOUT:g}s')


@pytest.fixture
def store(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, tmp_path: Path) -> Iterator[Store]:
    """A history store of its own, empty, on a port it picked."""
    cache = image_busybox.cache_directory(request.config, tmp_path_factory)
    binary = image_busybox.copied(image_busybox.pinned('metrics-history'), BINARY, cache)
    argv = [
        str(binary),
        '-httpListenAddr=127.0.0.1:0',
        f'-storageDataPath={tmp_path / "store"}',
        f'-retentionPeriod={stores.HISTORY_RETENTION}',
        '-search.disableCache',
        '-loggerLevel=ERROR',
    ]
    with process_sessions.started(argv, stdout=process_sessions.DEVNULL, stderr=process_sessions.DEVNULL) as running:
        served = Store(_listening(running.pid))
        deadline = time.monotonic() + STORE_TIMEOUT
        while True:
            try:
                if served.request('GET', '/health') == b'OK':
                    break
            except (OSError, AssertionError):
                if time.monotonic() > deadline:
                    raise
            time.sleep(0.05)
        yield served


#: vmagent as the API server finds it: its Service, by the operator's labels.
VMAGENT_SERVICE = 'vmagent-monitoring-victoria-metrics-k8s-stack'


@dataclass
class ApiServer:
    """A stand-in for the API server: the history store's service-proxy path, forwarded to `store`, and vmagent."""

    store: Store
    #: What vmagent reports it holds queued, for each of its remote writes.
    pending: tuple[float, ...] = (0.0, 0.0)
    #: Whether the store's `n`th write, from 1, is refused.
    refuses_post: Callable[[int], bool] = lambda _n: False
    posts: int = 0
    server: http.server.ThreadingHTTPServer = field(init=False)

    def _vmagent(self, path: str) -> bytes | None:
        namespace = conventions.MONITORING_NAMESPACE
        if path == f'/api/v1/namespaces/{namespace}/services?labelSelector=app.kubernetes.io%2Fname%3Dvmagent':
            service = {'metadata': {'name': VMAGENT_SERVICE}, 'spec': {'ports': [{'name': 'http', 'port': 8429}]}}
            return json.dumps({'kind': 'ServiceList', 'items': [service]}).encode()
        if path == f'/api/v1/namespaces/{namespace}/services/{VMAGENT_SERVICE}:8429/proxy/metrics':
            return ''.join(
                f'vmagent_remotewrite_pending_data_bytes{{path="/q/{index}",url="{index + 1}:secret-url"}} {value:g}\n'
                for index, value in enumerate(self.pending)
            ).encode()
        return None

    def __post_init__(self) -> None:
        stand_in, prefix = self, cluster.history('')

        class Proxy(http.server.BaseHTTPRequestHandler):
            def _body(self) -> bytes | None:
                # `kubectl create --raw -f -` streams its file, chunked.
                if self.headers.get('Transfer-Encoding', '').lower() != 'chunked':
                    length = int(self.headers.get('Content-Length') or 0)
                    return self.rfile.read(length) if length else None
                chunks: list[bytes] = []
                while size := int(self.rfile.readline().split(b';')[0], 16):
                    chunks.append(self.rfile.read(size))
                    _ = self.rfile.readline()
                _ = self.rfile.readline()
                return b''.join(chunks)

            def _answer(self, status: int, answer: bytes) -> None:
                self.send_response(status)
                self.send_header('Content-Length', str(len(answer)))
                self.end_headers()
                _ = self.wfile.write(answer)

            def _forward(self) -> None:
                body = self._body()
                vmagent = stand_in._vmagent(self.path)
                if vmagent is not None:
                    self._answer(200, vmagent)
                    return
                if not self.path.startswith(f'{prefix}/'):
                    self._answer(404, f'no route to {self.path}'.encode())
                    return
                if self.command == 'POST':
                    stand_in.posts += 1
                    if stand_in.refuses_post(stand_in.posts):
                        self._answer(503, b'the service is unavailable')
                        return
                try:
                    answer = stand_in.store.request(self.command, self.path.removeprefix(prefix), body)
                except AssertionError as refused:
                    self._answer(502, str(refused)[:200].encode())
                    return
                self._answer(200, answer)

            do_GET = _forward
            do_POST = _forward

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the base class's own name
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Proxy)

    def kubeconfig(self, into: Path) -> Path:
        server = f'http://127.0.0.1:{self.server.server_address[1]}'
        config: dict[str, Any] = {
            'apiVersion': 'v1',
            'kind': 'Config',
            'clusters': [{'name': 'stand-in', 'cluster': {'server': server}}],
            'users': [{'name': 'stand-in', 'user': {}}],
            'contexts': [{'name': 'stand-in', 'context': {'cluster': 'stand-in', 'user': 'stand-in'}}],
            'current-context': 'stand-in',
        }
        _ = into.write_text(json.dumps(config))
        return into


@pytest.fixture
def server(store: Store) -> Iterator[ApiServer]:
    stand_in = ApiServer(store)
    serving = threading.Thread(target=stand_in.server.serve_forever, daemon=True)
    serving.start()
    try:
        yield stand_in
    finally:
        stand_in.server.shutdown()
        stand_in.server.server_close()


@pytest.fixture
def api(server: ApiServer, tmp_path: Path) -> cluster.Kubectl:
    """The real `kubectl`, reaching the store and vmagent through the stand-in API server."""
    return cluster.Kubectl(server.kubeconfig(tmp_path / 'kubeconfig'), run=_sessioned, timeout=KUBECTL_TIMEOUT)


def _sessioned(argv: Sequence[str], *, stdin: bytes | None, timeout: float) -> bytes:
    """`cluster.run_command`'s contract, its command leading a session of its own (framework/testing.md §1.2)."""
    done = process_sessions.run(argv, input=stdin, timeout=timeout)
    if done.returncode != 0:
        raise cluster.Unanswered(f'`{argv[0]}` exited {done.returncode}: {done.stderr.decode().strip()}')
    return done.stdout


def _scraped(store: Store, node: cluster.Node) -> None:
    """The cluster's own scrape of `node`'s etcd and node exporter over its stretch, as vmagent writes it."""
    step = int(SCRAPE_INTERVAL.total_seconds())
    keep = exposition.etcd_families(READINGS)
    source = f'{exposition.SOURCE_LABEL}="{exposition.SOURCE}"'
    lines: list[str] = []
    for seconds in range(SCRAPED, SCRAPED_UNTIL + 1, step):
        if seconds in MISSED:
            continue
        at_ms = (T0 + seconds) * 1000
        etcd = exposition.etcd_lines(etcd_exposition(node, seconds), keep=keep, node=node.name, at_ms=at_ms)
        lines.extend(text.replace(source, f'instance="{node.address}:2381"') for text in etcd)
        lines.append(f'node_load1{{job="{NODE_EXPORTER_JOB}",node="{node.name}",pod="node-exporter-x"}} 0.5 {at_ms}')
    _ = store.request('POST', '/api/v1/import/prometheus', '\n'.join(lines).encode())
    store.flushed()


def _reading(name: str, window: dt.timedelta | None = None) -> Reading:
    (found,) = [r for r in READINGS if r.name == name and (window is None or r.window == window)]
    return found


# -- What the recorder reads -----------------------------------------------------------


@pytest.mark.parametrize(
    ('path', 'expected'),
    [
        (
            '/proc/pressure/io',
            {
                ('node_pressure_io_waiting_seconds_total', ()): IO_WAITING * 100,
                ('node_pressure_io_stalled_seconds_total', ()): IO_WAITING * 20,
            },
        ),
        ('/proc/pressure/cpu', {('node_pressure_cpu_waiting_seconds_total', ()): CPU_WAITING * 100}),
        (
            '/proc/meminfo',
            {
                ('node_memory_MemTotal_bytes', ()): 8000000 * 1024,
                ('node_memory_MemAvailable_bytes', ()): 4000000 * 1024,
                ('node_memory_Dirty_bytes', ()): 120 * 1024,
                ('node_memory_Writeback_bytes', ()): 0,
            },
        ),
    ],
    ids=['psi-some-and-full', 'psi-some-only', 'meminfo'],
)
def test_a_proc_file_reads_as_the_node_exporter_names_it_in_its_units(
    path: str, expected: dict[tuple[str, tuple[tuple[str, str], ...]], float]
) -> None:
    """A pressure total is microseconds and a memory figure kibibytes; a CPU with no `full` line has no stalled series."""
    samples = procfs.FILES[path](proc_file(path, 100))
    assert {(sample.name, sample.labels): sample.value for sample in samples} == pytest.approx(expected)


def test_a_disk_is_read_whole_and_its_partitions_and_loop_devices_are_not() -> None:
    """Sectors are 512 bytes and times milliseconds; `sda4` and `loop0` are left out as the node exporter leaves them."""
    samples = procfs.diskstats(proc_file('/proc/diskstats', 10))
    assert {value for sample in samples for _, value in sample.labels} == {'sda'}
    read = {sample.name: sample.value for sample in samples}
    writes = WRITES * 10
    assert read['node_disk_writes_completed_total'] == writes
    assert read['node_disk_written_bytes_total'] == writes * SECTORS_PER_WRITE * 512
    assert read['node_disk_write_time_seconds_total'] == pytest.approx(writes * 2 / 1000)
    assert read['node_disk_io_time_weighted_seconds_total'] == pytest.approx(writes * 3 / 1000)


def test_every_series_the_recorder_writes_is_one_the_history_keeps(tmp_path: Path) -> None:
    """The import writes past vmagent's relabeling, so the recorder keeps to the class the relabeling keeps."""
    path = tmp_path / 'recording'
    _ = recording(path, dt.timedelta(seconds=10), StandIns(Clock()))
    keep, drop = stores.history_relabeling()
    written = {(recorded.job, text.split('{', 1)[0]) for text, recorded in recorded_lines(path)}
    assert written
    outside = sorted(
        (job, name)
        for job, name in written
        if not re.fullmatch(keep['regex'], f'{job};{name}') or re.fullmatch(drop['regex'], name)
    )
    assert not outside, f'the history store does not keep {outside}'


# -- Recording -------------------------------------------------------------------------


def _since_t0(read: Sequence[exposition.Recorded], node: str, job: str) -> set[float]:
    return {(r.at_ms - T0 * 1000) / 1000 for r in read if r.node == node and r.job == job}


def test_a_source_that_does_not_answer_costs_its_rounds_and_is_logged_once_each_way(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """etcd before the API answers, and a node's files while it is away, are missing from those rounds alone."""
    api_up, away = 12.0, (20.0, 32.0)

    def refuses(node: str, source: str, seconds: float) -> bool:
        if source == record.ETCD:
            return seconds < api_up
        return node == 'cp3' and away[0] <= seconds < away[1]

    path = tmp_path / 'recording'
    with caplog.at_level(logging.INFO, logger=record.__name__):
        rounds = recording(path, dt.timedelta(seconds=45), StandIns(Clock(), refuses))
    assert rounds == 10
    read = [recorded for _, recorded in recorded_lines(path)]
    rounds_at = {5.0 * index for index in range(rounds)}
    for node in NODES:
        assert _since_t0(read, node.name, ETCD_JOB) == {at for at in rounds_at if at >= api_up}
        missing: set[float] = {at for at in rounds_at if away[0] <= at < away[1]} if node.name == 'cp3' else set()
        assert _since_t0(read, node.name, NODE_EXPORTER_JOB) == rounds_at - missing
    logged = [entry.getMessage() for entry in caplog.records]
    assert sum(line.startswith('files on cp3 is not recorded') for line in logged) == 1
    assert sum(line.startswith('files on cp3 is recorded again') for line in logged) == 1
    assert sum(line.startswith('etcd on cp1 is not recorded') for line in logged) == 1


def test_an_address_that_answers_as_another_node_has_its_files_left_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """cp3's address answering as cp1 records none of its files under cp3; its etcd, read by name, is recorded."""
    reused = (10.0, 25.0)

    def answers_as(node: str, seconds: float) -> str | None:
        return 'cp1' if node == 'cp3' and reused[0] <= seconds < reused[1] else None

    path = tmp_path / 'recording'
    with caplog.at_level(logging.INFO, logger=record.__name__):
        rounds = recording(path, dt.timedelta(seconds=35), StandIns(Clock(), answers_as=answers_as))
    read = [recorded for _, recorded in recorded_lines(path)]
    rounds_at = {5.0 * index for index in range(rounds)}
    assert _since_t0(read, 'cp3', NODE_EXPORTER_JOB) == {at for at in rounds_at if not reused[0] <= at < reused[1]}
    assert _since_t0(read, 'cp3', ETCD_JOB) == rounds_at
    assert _since_t0(read, 'cp1', NODE_EXPORTER_JOB) == rounds_at
    logged = [entry.getMessage() for entry in caplog.records]
    assert (
        sum(
            line.startswith("hostname on cp3 is not recorded until this ends: 10.0.0.13 answers as 'cp1'")
            for line in logged
        )
        == 1
    )


def test_a_read_that_hangs_costs_its_own_rounds_and_every_other_keeps_its_cadence(tmp_path: Path) -> None:
    """cp3's files hang across three rounds: they are read once, every other read every round, and theirs land late."""
    released = threading.Event()
    stand_ins = StandIns(Clock())
    hung = threading.Event()

    class Hanging(StandIns):
        def read(self, address: str, path: str) -> str:
            if address == NODES[2].address and path == record.HOSTNAME and not released.is_set():
                hung.set()
                assert released.wait(timeout=STORE_TIMEOUT), 'never released'
            return super().read(address, path)

    hanging = Hanging(stand_ins.clock)
    reading = recorder(hanging, every_read=False)
    path = tmp_path / 'recording'
    recorded = record.Recorded()
    with path.open('a') as out, ThreadPoolExecutor(max_workers=8) as pool:
        for _ in range(3):
            reading.start(pool)
            assert hung.wait(timeout=STORE_TIMEOUT)
            for (node, _source), future in list(reading.under_way.items()):
                if node != 'cp3' or _source != record.FILES:
                    _ = future.result(timeout=STORE_TIMEOUT)
            reading.collect(out, recorded, timeout=0)
            hanging.clock.now += 5
        released.set()
        _ = reading.under_way[('cp3', record.FILES)].result(timeout=STORE_TIMEOUT)
        reading.collect(out, recorded, timeout=0)
    read = [recorded for _, recorded in recorded_lines(path)]
    assert _since_t0(read, 'cp1', ETCD_JOB) == {0.0, 5.0, 10.0}
    assert _since_t0(read, 'cp1', NODE_EXPORTER_JOB) == {0.0, 5.0, 10.0}
    assert sum(1 for node, path_read in hanging.reads if node == 'cp3' and path_read == record.HOSTNAME) == 1
    assert _since_t0(read, 'cp3', NODE_EXPORTER_JOB) == {15.0}


def test_a_read_slower_than_a_round_in_real_time_changes_no_round_of_a_recording(tmp_path: Path) -> None:
    """A stand-in read that stalls past the round's real 5 s: the rounds are the stand-ins' clock's, every one whole."""

    class Stalling(StandIns):
        def get(self, path: str) -> bytes:
            if path == cluster.etcd_metrics('cp1') and self._seconds() == 10:
                time.sleep(record.INTERVAL.total_seconds() + 0.5)  # the stall the case is about, not a wait on anything
            return super().get(path)

    path = tmp_path / 'recording'
    rounds = recording(path, dt.timedelta(seconds=30), Stalling(Clock()))
    read = [recorded for _, recorded in recorded_lines(path)]
    assert _since_t0(read, 'cp1', ETCD_JOB) == {5.0 * index for index in range(rounds)}


def test_a_line_cut_off_by_a_killed_recorder_is_skipped_and_dropped_before_the_next_round(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'recording'
    _ = recording(path, dt.timedelta(seconds=5), StandIns(Clock()))
    whole = len(recorded_lines(path))
    with path.open('a') as out:
        _ = out.write('node_load1{job="node-exporter",node="cp1",sou')
    assert len(recorded_lines(path)) == whole
    _ = recording(path, dt.timedelta(seconds=5), StandIns(Clock()))
    assert len(recorded_lines(path)) == 2 * whole


def test_the_control_planes_are_physicals_addresses_read_under_its_own_passphrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`node_private_ips`, read with the slot `physical`'s passphrase is in and never the stack passphrase."""
    monkeypatch.setenv('PULUMI_CONFIG_PASSPHRASE', 'the stack passphrase')
    seen: list[tuple[list[str], Mapping[str, str]]] = []

    def pulumi(args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        del cwd, stdin
        seen.append((list(args), env))
        return json.dumps({'cp2': '10.0.0.12', 'cp1': '10.0.0.11'})

    assert cluster.control_planes(pulumi) == [cluster.Node('cp1', '10.0.0.11'), cluster.Node('cp2', '10.0.0.12')]
    ((args, env),) = seen
    assert args[:3] == ['stack', 'output', conventions.PHYSICAL_OUTPUTS.node_private_ips]
    assert args[args.index('--stack') + 1] == stack_environment.PHYSICAL_STACK
    # A whole environment: one laid over this process's would bring the stack passphrase back.
    assert isinstance(env, pulumi_cli.WholeEnvironment)
    assert 'PULUMI_CONFIG_PASSPHRASE' not in env
    assert env['PULUMI_CONFIG_PASSPHRASE_FILE'] == str(
        workstation.directory() / stack_environment.PHYSICAL_PASSPHRASE_SLOT
    )


@pytest.mark.parametrize(
    ('output', 'refused'),
    [({'cp1': 'not an address'}, stack_addresses.UnusableAddressOutput), ({}, stack_addresses.UnusableAddressOutput)],
    ids=['not-an-address', 'empty'],
)
def test_an_output_that_holds_no_control_plane_to_record_is_refused(
    output: dict[str, str], refused: type[Exception]
) -> None:
    def pulumi(args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        del args, cwd, env, stdin
        return json.dumps(output)

    with pytest.raises(refused):
        _ = cluster.control_planes(pulumi)


# -- Importing -------------------------------------------------------------------------


def _fed(reading: Reading) -> bool:
    """Whether every series `reading` reads is one the design records: an etcd family it keeps, or a recorded file's."""
    etcd = exposition.etcd_families(READINGS)
    return all(
        # `up` is what vmagent writes of a scrape, which no exporter serves.
        selector.metric != 'up'
        and selector.jobs is not None
        and (
            (ETCD_JOB in selector.jobs and selector.metric in etcd)
            or (NODE_EXPORTER_JOB in selector.jobs and RECORDED_FILES_FAMILIES.fullmatch(selector.metric) is not None)
        )
        for selector in selectors(reading.expr)
    )


def test_a_recorded_period_is_read_by_every_reading_of_what_the_design_records(
    store: Store, api: cluster.Kubectl, tmp_path: Path
) -> None:
    """Each reading of the recorded files and of etcd, from its own expression, has a series on every control plane."""
    path = tmp_path / 'recording'
    _ = recording(path, dt.timedelta(minutes=20), StandIns(Clock()))
    _ = history.import_file(path, api)
    store.flushed()
    fed = [reading for reading in READINGS if _fed(reading)]
    found = {
        reading.name: sorted({str(row['metric'].get('node')) for row in store.query(reading.expr, READ_AT)})
        for reading in fed
    }
    nodes = sorted(node.name for node in NODES)
    assert {name: on for name, on in found.items() if on != nodes} == {}


def test_a_reading_of_a_recorded_period_is_the_rate_the_counters_grew_at(
    store: Store, api: cluster.Kubectl, tmp_path: Path
) -> None:
    path = tmp_path / 'recording'
    _ = recording(path, dt.timedelta(minutes=20), StandIns(Clock()))
    _ = history.import_file(path, api)
    store.flushed()
    pressure = _reading('node_cpu_pressure_some', dt.timedelta(minutes=10))
    values = {str(row['metric']['node']): float(row['value'][1]) for row in store.query(pressure.expr, READ_AT)}
    assert values == pytest.approx(dict.fromkeys((node.name for node in NODES), CPU_WAITING))


@pytest.fixture
def seamed(store: Store, api: cluster.Kubectl, tmp_path: Path) -> tuple[history.Imported, Path]:
    """A recording of `SPAN` imported over the cluster's own scrape of cp1."""
    _scraped(store, NODES[0])
    path = tmp_path / 'recording'
    _ = recording(path, SPAN, StandIns(Clock()))
    imported = history.import_file(path, api)
    store.flushed()
    return imported, path


@pytest.mark.parametrize('minutes', [21, 26, 27, 31, 33])
def test_a_count_by_increase_reads_true_across_the_seams_and_a_missed_scrape(
    store: Store, seamed: tuple[history.Imported, Path], minutes: int
) -> None:
    """Five slow fsyncs in every five minutes: where the stretch starts, across its missed scrapes, where it ends."""
    over = _reading('etcd_wal_fsyncs_over_1s')
    assert store.value(over.expr, 'cp1', T0 + minutes * 60) == pytest.approx(5)


@pytest.mark.parametrize('minutes', [22, 28, 34])
def test_a_count_by_changes_reads_the_leader_change_just_before_the_stretch(
    store: Store, seamed: tuple[history.Imported, Path], minutes: int
) -> None:
    changes = _reading('etcd_leader_changes')
    assert store.value(changes.expr, 'cp1', T0 + minutes * 60) == pytest.approx(1)


def test_the_import_leaves_out_the_stretch_and_writes_each_region_as_a_series_of_its_own(
    store: Store, api: cluster.Kubectl, seamed: tuple[history.Imported, Path]
) -> None:
    """cp1's recording stops within a round of the stretch and resumes after it under a new label; a second import writes nothing."""
    imported, path = seamed
    labels = exposition.SOURCE_LABEL, exposition.SOURCE
    regions = {
        json.loads(row)['metric'][exposition.REGION_LABEL]
        for row in store.request(
            'GET',
            '/api/v1/export?'
            + urllib.parse.urlencode(
                {'match[]': f'node_load1{{node="cp1",{labels[0]}="{labels[1]}"}}', 'start': T0 - 60}
            ),
        )
        .decode()
        .splitlines()
        if row.strip()
    }
    assert len(regions) == 2
    round_ms = int(record.INTERVAL.total_seconds() * 1000)
    for job, probe in history.PROBES.items():
        written = store.times(f'{probe}{{job="{job}",node="cp1",{labels[0]}="{labels[1]}"}}')
        before = [at for at in written if at < (T0 + SCRAPED) * 1000]
        after = [at for at in written if at > (T0 + SCRAPED_UNTIL) * 1000]
        assert (T0 + SCRAPED) * 1000 - max(before) <= round_ms
        assert min(after) - (T0 + SCRAPED_UNTIL) * 1000 <= round_ms
        assert len(written) == len(before) + len(after)
    assert imported.scraped.get(('cp1', ETCD_JOB))
    assert not imported.scraped.get(('cp2', ETCD_JOB))
    again = history.import_file(path, api)
    assert again.written == {}


def test_an_import_cut_off_partway_writes_the_rest_when_run_again_but_for_at_most_a_round(
    store: Store, server: ApiServer, api: cluster.Kubectl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / 'recording'
    _ = recording(path, dt.timedelta(minutes=5), StandIns(Clock()))
    monkeypatch.setattr(history, 'CHUNK', 500)
    server.refuses_post = lambda n: n == 3
    with pytest.raises(cluster.Unanswered):
        _ = history.import_file(path, api)
    store.flushed()
    _ = history.import_file(path, api)
    store.flushed()
    by_series: dict[str, set[int]] = {}
    for text, recorded in recorded_lines(path):
        by_series.setdefault(text.rsplit(' ', 2)[0], set()).add(recorded.at_ms)
    holes: list[int] = []
    for selector, recorded_at in by_series.items():
        stored = store.times(selector)
        assert len(stored) == len(set(stored)), f'{selector} written twice'
        missing = recorded_at - set(stored)
        if missing:
            holes.append(max(missing) - min(missing))
    assert holes, 'the cut-off import left no hole, so the case judges nothing'
    assert max(holes) <= history.ROUND.total_seconds() * 1000


def test_a_second_import_reads_what_the_first_wrote_where_the_store_flushed_it_by_itself(
    store: Store, api: cluster.Kubectl, tmp_path: Path
) -> None:
    """The first import asked the store for its samples before it wrote them; the second import is not answered from that."""
    path = tmp_path / 'recording'
    _ = recording(path, dt.timedelta(minutes=1), StandIns(Clock()))
    first = history.import_file(path, api)
    store.indexed()
    store.flushed()
    again = history.import_file(path, api)
    assert first.written
    assert again.written == {}


def test_an_import_refuses_while_vmagent_holds_data_queued_for_the_stores(
    store: Store, server: ApiServer, api: cluster.Kubectl, tmp_path: Path
) -> None:
    path = tmp_path / 'recording'
    _ = recording(path, dt.timedelta(seconds=30), StandIns(Clock()))
    server.pending = (0.0, float(64 * history.QUEUE_SLACK))
    with pytest.raises(history.QueueNotDrained):
        _ = history.import_file(path, api)
    assert server.posts == 0
