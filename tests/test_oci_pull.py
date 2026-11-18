"""The pull of OCI's block-volume series, run against OCI's SDK and two stand-ins on the wire.

OCI is the SDK's own Monitoring client, signing with a key generated here,
whose HTTP session answers `SummarizeMetricsData` from
`data/oci-summarize-metrics-data.json`: responses in the API reference's wire
shape, written rather than recorded (the file says so), served for the window
each request asks for. So a request is serialized, and a response
deserialized, by the SDK the image carries. The stores are a local HTTP
server answering VictoriaMetrics' instant query for the newest sample of a
series, with the semantics of the one matcher the query carries, and
recording what is written to the import endpoint.

Literals written here are OCI's and VictoriaMetrics' contracts: the request's
field names, the dimension a series names its volume by, the import format.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import logging
import re
import sys
import threading
import urllib.parse
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, cast

import pytest
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from requests.adapters import BaseAdapter

from kluster.components.monitoring import oci_pull
from kluster.components.monitoring.oci_pull import Target

RESPONSES = json.loads((Path(__file__).parent / 'data' / 'oci-summarize-metrics-data.json').read_text())

NAMESPACE = 'oci_blockstore'
COMPARTMENT: str = RESPONSES['compartmentId']
TENANCY = 'ocid1.tenancy.oc1..exampletenancy'

#: The volumes the responses hold a series of, and the one among them no
#: census declares: a replaced volume's.
BOOT1 = 'ocid1.bootvolume.oc1.phx.exampleboot1'
BOOT2 = 'ocid1.bootvolume.oc1.phx.exampleboot2'
CACHE = 'ocid1.volume.oc1.phx.examplecache'
RETIRED = 'ocid1.bootvolume.oc1.phx.exampleretired'

TARGETS = {
    BOOT1: Target(node='cp1', volume='boot'),
    BOOT2: Target(node='cp2', volume='boot'),
    CACHE: Target(node='cp1', volume='hath-cache'),
}

#: The run's clock: a little after the responses' last minute.
NOW = dt.datetime(2026, 11, 17, 9, 5, 30, tzinfo=dt.UTC)


def _stamp(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace('Z', '+00:00'))


# -- OCI -----------------------------------------------------------------------------


class Telemetry(BaseAdapter):
    """OCI's Monitoring endpoint: each `SummarizeMetricsData` answered from the responses, inside the asked window.

    `responses` stands in for the file's, by metric. A request `refused` names
    is answered as OCI refuses a parameter it does not accept: a 400 with the
    service's error body.
    """

    def __init__(
        self,
        responses: Mapping[str, list[dict[str, Any]]] | None = None,
        refused: Callable[[str, dt.datetime], bool] | None = None,
    ) -> None:
        super().__init__()
        self.responses = RESPONSES['responses'] if responses is None else responses
        self.refused = refused or _refuses_nothing
        self.requests: list[tuple[dict[str, list[str]], dict[str, Any]]] = []

    def send(self, request: requests.PreparedRequest, *_: object, **__: object) -> requests.Response:
        url = urllib.parse.urlsplit(cast('str', request.url))
        assert url.path.endswith('/metrics/actions/summarizeMetricsData'), url.path
        body = json.loads(cast('bytes', request.body))
        self.requests.append((urllib.parse.parse_qs(url.query), body))
        metric = body['query'].split('[', 1)[0]
        start, end = _stamp(body['startTime']), _stamp(body['endTime'])
        if self.refused(metric, start):
            return _response(request, 400, {'code': 'InvalidParameter', 'message': f'{metric} is refused here'})
        answer = [
            stream
            | {
                'aggregatedDatapoints': [
                    point for point in stream['aggregatedDatapoints'] if start <= _stamp(point['timestamp']) < end
                ]
            }
            for stream in self.responses.get(metric, [])
        ]
        return _response(request, 200, [it for it in answer if it['aggregatedDatapoints']])

    def close(self) -> None:
        pass


def _refuses_nothing(metric: str, start: dt.datetime) -> bool:
    del metric, start
    return False


def _response(request: requests.PreparedRequest, status: int, payload: object) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers['Content-Type'] = 'application/json'
    response.headers['opc-request-id'] = 'test'
    response._content = json.dumps(payload).encode()  # pyright: ignore[reportPrivateUsage] -- how a Response is built without a socket
    response.request = request
    response.url = cast('str', request.url)
    return response


@pytest.fixture(scope='module')
def key() -> tuple[str, str]:
    """An API key's PEM and its fingerprint, as OCI computes it: the MD5 of the public key's DER."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    der = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashes.Hash(hashes.MD5())
    digest.update(der)
    return pem, ':'.join(f'{byte:02x}' for byte in digest.finalize())


# -- The stores ----------------------------------------------------------------------


@dataclass
class History:
    """What the stores hold and were written: the newest sample per series, node and volume, and the imports."""

    #: series → (labels, newest sample's time in seconds), the labels as the
    #: store keeps them: a series imported from another cluster carries `cluster`.
    newest: dict[str, list[tuple[dict[str, str], float]]] = field(default_factory=dict)
    #: The answer's `resultType`: `vector`, an instant query's, each row
    #: carrying one `value`; or `matrix`, a range query's, each carrying `values`.
    shape: Literal['vector', 'matrix'] = 'vector'
    queries: list[str] = field(default_factory=list)
    imported: list[dict[str, Any]] = field(default_factory=list)

    def answer(self, query: str) -> list[dict[str, Any]]:
        """The instant query's answer, in `shape`: the newest time per node and volume of the series the selector names.

        `cluster=""` matches a series without the label, as in Prometheus and
        VictoriaMetrics; a selector without it matches every series of the name.
        """
        found = re.search(r'tlast_over_time\((\w+)\{([^}]*)\}', query)
        assert found, query
        series, matchers = found.groups()
        this_cluster = 'cluster=""' in matchers
        best: dict[tuple[str, str], float] = {}
        for labels, seconds in self.newest.get(series, []):
            if this_cluster and labels.get('cluster'):
                continue
            key = (labels['node'], labels['volume'])
            best[key] = max(best.get(key, 0.0), seconds)
        rows = [
            ({'node': node, 'volume': volume}, [NOW.timestamp(), str(seconds)])
            for (node, volume), seconds in best.items()
        ]
        if self.shape == 'matrix':
            return [{'metric': labels, 'values': [sample]} for labels, sample in rows]
        return [{'metric': labels, 'value': sample} for labels, sample in rows]

    def written(self, series: str) -> dict[Target, dict[int, float]]:
        """The samples imported of `series`, per target, by time in milliseconds."""
        found: dict[Target, dict[int, float]] = {}
        for line in self.imported:
            metric = line['metric']
            if metric['__name__'] == series:
                samples = found.setdefault(Target(node=metric['node'], volume=metric['volume']), {})
                samples.update(zip(line['timestamps'], line['values'], strict=True))
        return found


@pytest.fixture
def history() -> Generator[tuple[History, str]]:
    """The stores, on a local port: what they hold and were written, and their address."""
    held = History()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            assert url.path == '/api/v1/query', url.path
            (query,) = urllib.parse.parse_qs(url.query)['query']
            held.queries.append(query)
            self._reply({'status': 'success', 'data': {'resultType': held.shape, 'result': held.answer(query)}})

        def do_POST(self) -> None:
            assert self.path == '/api/v1/import', self.path
            body = self.rfile.read(int(self.headers['Content-Length'])).decode()
            held.imported.extend(json.loads(line) for line in body.splitlines() if line)
            self.send_response(204)
            self.end_headers()

        def _reply(self, payload: object) -> None:
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the base class's parameter name
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield held, f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


# -- A run ---------------------------------------------------------------------------


def _settings(address: str, targets: Mapping[str, Target]) -> dict[str, Any]:
    return {
        'namespace': NAMESPACE,
        'compartment': COMPARTMENT,
        'tenancy': TENANCY,
        'region': 'us-phoenix-1',
        'targets': {ocid: {'node': it.node, 'volume': it.volume} for ocid, it in targets.items()},
        'history': address,
        'importer': address,
    }


@dataclass
class Run:
    """One run of the program's `main`, and what it asked of OCI."""

    status: int
    telemetry: Telemetry


def _run(
    tmp_path: Path,
    key: tuple[str, str],
    address: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    targets: Mapping[str, Target] = TARGETS,
    telemetry: Telemetry | None = None,
) -> Run:
    """`main` over a settings file and a credential directory as the pod mounts them, OCI answered by `Telemetry`."""
    pem, fingerprint = key
    credentials = tmp_path / 'key'
    credentials.mkdir()
    for name, value in {
        'user': 'ocid1.user.oc1..examplereader',
        'fingerprint': fingerprint,
        'key_content': pem,
    }.items():
        (credentials / name).write_text(value)
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps(_settings(address, targets)))
    answering = Telemetry() if telemetry is None else telemetry
    built = oci_pull.client_for

    def client_for(job: oci_pull.Job) -> Any:
        client = built(job)
        client.base_client.session.mount('https://', answering)
        return client

    monkeypatch.setattr(oci_pull, 'client_for', client_for)
    status = oci_pull.main([str(settings), str(credentials)], now=NOW)
    return Run(status, answering)


def _recorded(ocid: str, metric: str) -> dict[int, float]:
    (stream,) = [it for it in RESPONSES['responses'][metric] if it['dimensions']['resourceId'] == ocid]
    return {int(_stamp(it['timestamp']).timestamp() * 1000): it['value'] for it in stream['aggregatedDatapoints']}


def test_each_series_is_written_under_its_volumes_node_and_name(
    tmp_path: Path, key: tuple[str, str], history: tuple[History, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A series OCI names by its OCID alone reaches the stores as `<namespace>_<metric>{node, volume}`."""
    held, address = history

    run = _run(tmp_path, key, address, monkeypatch)

    assert run.status == 0
    for metric in oci_pull.METRICS:
        assert held.written(f'{NAMESPACE}_{metric}') == {
            target: _recorded(ocid, metric) for ocid, target in TARGETS.items()
        }, metric


def test_a_series_of_a_volume_the_census_does_not_declare_is_skipped(
    tmp_path: Path, key: tuple[str, str], history: tuple[History, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replaced volume's last days come back under its old OCID; they are written nowhere and fail nothing."""
    held, address = history

    run = _run(tmp_path, key, address, monkeypatch)

    assert run.status == 0
    written = {(it['metric']['node'], it['metric']['volume']) for it in held.imported}
    assert written == {(it.node, it.volume) for it in TARGETS.values()}


def test_a_declared_volume_with_no_series_fails_the_run_after_the_rest_is_written(
    tmp_path: Path, key: tuple[str, str], history: tuple[History, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CronJob's last success stops moving, which is what the freshness rule reads, and nothing else is lost."""
    held, address = history
    unseen = Target(node='cp3', volume='boot')

    run = _run(tmp_path, key, address, monkeypatch, targets={**TARGETS, 'ocid1.bootvolume.oc1.phx.unseen': unseen})

    assert run.status == 1
    for metric in oci_pull.METRICS:
        assert set(held.written(f'{NAMESPACE}_{metric}')) == set(TARGETS.values()), metric


def test_every_request_names_the_metric_namespace_and_the_compartment(
    tmp_path: Path, key: tuple[str, str], history: tuple[History, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader's policy is conditioned on the namespace, and declines a request that does not carry it."""
    _, address = history

    run = _run(tmp_path, key, address, monkeypatch)

    assert run.telemetry.requests
    for query, body in run.telemetry.requests:
        assert body['namespace'] == NAMESPACE
        assert query['compartmentId'] == [COMPARTMENT]


def test_a_store_holding_nothing_is_read_seven_days_back_a_day_at_a_time(
    tmp_path: Path, key: tuple[str, str], history: tuple[History, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first run after a rebuild backfills what OCI still holds at one-minute resolution, and no further."""
    _, address = history

    run = _run(tmp_path, key, address, monkeypatch)

    for metric in oci_pull.METRICS:
        windows = [
            (_stamp(body['startTime']), _stamp(body['endTime']))
            for _, body in run.telemetry.requests
            if body['query'].startswith(f'{metric}[')
        ]
        assert min(start for start, _ in windows) >= NOW - oci_pull.BACKFILL, metric
        assert min(start for start, _ in windows) < NOW - oci_pull.BACKFILL + dt.timedelta(minutes=2), metric
        assert max(end for _, end in windows) <= NOW, metric
        assert all(end - start <= oci_pull.SPAN for start, end in windows), metric
        assert sum((end - start for start, end in windows), dt.timedelta()) == windows[-1][1] - windows[0][0], metric


def test_a_run_writes_only_what_is_newer_than_the_newest_sample_the_history_holds(
    tmp_path: Path, key: tuple[str, str], history: tuple[History, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each volume's window starts after its own newest sample, so nothing is written twice and nothing is skipped.

    One request covers every volume from the oldest of their newest samples,
    so the volumes stored further along are held to their own.
    """
    held, address = history
    stored = {
        BOOT1: dt.datetime(2026, 11, 17, 9, 2, tzinfo=dt.UTC),
        BOOT2: dt.datetime(2026, 11, 17, 9, 0, tzinfo=dt.UTC),
        CACHE: dt.datetime(2026, 11, 17, 9, 1, tzinfo=dt.UTC),
    }
    for metric in oci_pull.METRICS:
        held.newest[f'{NAMESPACE}_{metric}'] = [
            ({'node': TARGETS[ocid].node, 'volume': TARGETS[ocid].volume}, at.timestamp())
            for ocid, at in stored.items()
        ]

    run = _run(tmp_path, key, address, monkeypatch)

    assert run.status == 0
    for metric in oci_pull.METRICS:
        assert held.written(f'{NAMESPACE}_{metric}') == {
            target: {
                at: value for at, value in _recorded(ocid, metric).items() if at > int(stored[ocid].timestamp() * 1000)
            }
            for ocid, target in TARGETS.items()
        }, metric
    assert {_stamp(body['startTime']) for _, body in run.telemetry.requests} == {
        min(stored.values()) + dt.timedelta(minutes=1)
    }


def test_another_clusters_series_does_not_move_this_ones_window(
    tmp_path: Path, key: tuple[str, str], history: tuple[History, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history holds series imported from the legacy cluster under `cluster`; only this cluster's count."""
    held, address = history
    stored = dt.datetime(2026, 11, 17, 9, 1, tzinfo=dt.UTC)
    imported = dt.datetime(2026, 11, 17, 9, 3, tzinfo=dt.UTC)
    for metric in oci_pull.METRICS:
        held.newest[f'{NAMESPACE}_{metric}'] = [
            *(({'node': it.node, 'volume': it.volume}, stored.timestamp()) for it in TARGETS.values()),
            *(
                ({'node': it.node, 'volume': it.volume, 'cluster': 'legacy'}, imported.timestamp())
                for it in TARGETS.values()
            ),
        ]

    _ = _run(tmp_path, key, address, monkeypatch)

    after = int(stored.timestamp() * 1000)
    assert held.written(f'{NAMESPACE}_VolumeReadOps') == {
        target: {at: value for at, value in _recorded(ocid, 'VolumeReadOps').items() if at > after}
        for ocid, target in TARGETS.items()
    }


# -- The settings and the program itself ---------------------------------------------


#: A setting left out, in place of its value.
ABSENT = object()


@pytest.mark.parametrize(
    ('edit', 'named'),
    [
        ({'compartment': ABSENT}, "'compartment'"),
        ({'history': ''}, "'history'"),
        ({'targets': {}}, "'targets'"),
        ({'targets': {BOOT1: {'node': 'cp1'}}}, repr(BOOT1)),
    ],
    ids=['no compartment', 'an empty address', 'no target', 'a target with no volume'],
)
def test_settings_a_run_cannot_read_are_refused_by_the_field(
    tmp_path: Path, edit: dict[str, object], named: str
) -> None:
    """A mistake in the settings stops the run naming what is wrong, before any request."""
    settings = {
        name: value
        for name, value in {**_settings('http://stores.invalid', TARGETS), **edit}.items()
        if value is not ABSENT
    }
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(settings))

    with pytest.raises(oci_pull.JobRefused) as refused:
        _ = oci_pull.Job.read(path, credentials=tmp_path)

    assert named in str(refused.value)


def test_the_program_imports_nothing_the_image_does_not_carry() -> None:
    """The `oci-sdk` image holds the interpreter and the SDK alone, and no code of this repository."""
    tree = ast.parse(Path(oci_pull.__file__).read_text())
    imported = {
        name.split('.')[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ''])
    }

    assert imported - {'oci'} <= set(sys.stdlib_module_names) | {'__future__'}


# -- A run that meets an error ---------------------------------------------------------


def test_a_metric_oci_refuses_stops_alone_and_fails_the_run(
    tmp_path: Path, key: tuple[str, str], history: tuple[History, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other metric is written; the refused one leaves the CronJob's last success where it was."""
    held, address = history
    refused = 'VolumeWriteOps'

    run = _run(tmp_path, key, address, monkeypatch, telemetry=Telemetry(refused=lambda metric, _: metric == refused))

    assert run.status == 1
    assert held.written(f'{NAMESPACE}_{refused}') == {}
    for metric in oci_pull.METRICS:
        if metric != refused:
            assert held.written(f'{NAMESPACE}_{metric}') == {
                target: _recorded(ocid, metric) for ocid, target in TARGETS.items()
            }, metric


def _hourly(responses: Mapping[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """The responses with a sample at every hour of the backfill ahead of their own, so a backfill has days to write."""
    first = dt.datetime(2026, 11, 17, 9, 0, tzinfo=dt.UTC)
    hours = [first - dt.timedelta(hours=n) for n in range(int(oci_pull.BACKFILL / dt.timedelta(hours=1)), 0, -1)]
    earlier = [{'timestamp': at.strftime('%Y-%m-%dT%H:%M:%S.000Z'), 'value': 1.0} for at in hours]
    return {
        metric: [stream | {'aggregatedDatapoints': [*earlier, *stream['aggregatedDatapoints']]} for stream in streams]
        for metric, streams in responses.items()
    }


def test_a_refused_span_ends_its_metric_and_what_came_before_it_is_written(
    tmp_path: Path, key: tuple[str, str], history: tuple[History, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backfill's middle day refused: its metric's samples end where that day starts, and no later day is asked for.

    A later day written would move the history's newest sample past the hole,
    and no run after it would fill it. The next metric is written whole.
    """
    held, address = history
    refused_from = NOW - oci_pull.BACKFILL + 3 * oci_pull.SPAN

    run = _run(
        tmp_path,
        key,
        address,
        monkeypatch,
        telemetry=Telemetry(
            _hourly(RESPONSES['responses']),
            refused=lambda metric, start: metric == 'VolumeReadOps' and start >= refused_from,
        ),
    )

    assert run.status == 1
    written = held.written(f'{NAMESPACE}_VolumeReadOps')
    stamps = [at for samples in written.values() for at in samples]
    assert stamps
    assert max(stamps) < refused_from.timestamp() * 1000
    asked = [
        _stamp(body['startTime']) for _, body in run.telemetry.requests if body['query'].startswith('VolumeReadOps[')
    ]
    assert [start for start in asked if start >= refused_from] == [
        min(start for start in asked if start >= refused_from)
    ]
    after = held.written(f'{NAMESPACE}_VolumeWriteOps')
    assert set(after) == set(TARGETS.values())
    assert all(set(_recorded(ocid, 'VolumeWriteOps')) <= set(after[target]) for ocid, target in TARGETS.items())


def test_a_series_that_names_no_volume_is_an_error_of_its_own(
    tmp_path: Path,
    key: tuple[str, str],
    history: tuple[History, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An answer of another shape than the query asks for fails the run, naming what it carried, and is no skipped volume."""
    held, address = history
    shapeless = 'VolumeThrottledIOs'
    responses = {
        metric: [
            stream | {'dimensions': {'attachmentId': 'ocid1.volumeattachment.oc1.phx.example'}}
            if metric == shapeless
            else stream
            for stream in streams
        ]
        for metric, streams in RESPONSES['responses'].items()
    }

    with caplog.at_level(logging.INFO, logger='oci-pull'):
        run = _run(tmp_path, key, address, monkeypatch, telemetry=Telemetry(responses))

    assert run.status == 1
    assert held.written(f'{NAMESPACE}_{shapeless}') == {}
    named = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert [message for message in named if shapeless in message and 'attachmentId' in message]
    undeclared = [record.getMessage() for record in caplog.records if 'census does not declare' in record.getMessage()]
    assert all(RETIRED in message for message in undeclared), undeclared


def test_an_answer_of_a_shape_the_program_does_not_read_ends_the_run_with_its_traceback(
    tmp_path: Path, key: tuple[str, str], history: tuple[History, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A defect every metric would meet alike is no error of reaching the stores, and does not stop one metric alone.

    The history store answers the newest-sample query as a range query does, a
    matrix whose rows carry `values` where the program reads an instant query's
    `value`. The run ends there, on the first metric, rather than going on to
    the next and exiting 1.
    """
    held, address = history
    held.shape = 'matrix'
    stored = dt.datetime(2026, 11, 17, 9, 1, tzinfo=dt.UTC)
    for metric in oci_pull.METRICS:
        held.newest[f'{NAMESPACE}_{metric}'] = [
            ({'node': it.node, 'volume': it.volume}, stored.timestamp()) for it in TARGETS.values()
        ]

    with pytest.raises(Exception):  # noqa: B017 -- the program promises a traceback, not which exception carries it
        _ = _run(tmp_path, key, address, monkeypatch)

    assert len(held.queries) == 1
    assert held.imported == []
