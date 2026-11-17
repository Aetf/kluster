"""`credentials derived oci-physical terminate|read|detach`, against a fake OCI served over HTTP.

The SDK's own clients, signer, composite operations and waiter run unchanged:
what is faked is the service, one local server answering the compute, block
storage, network and work request APIs in their wire format (camelCase JSON,
`opc-work-request-id` and `opc-next-page` headers, a 404 body for a resource
the service does not hold). So what a case holds is what the module does with
OCI's answers -- which calls it makes in which order, what it waits on, what it
prints, what it refuses -- and not which SDK methods it happens to call.

The fake keeps state the way the service does for these calls: a termination
puts the instance `TERMINATING` and answers with a work request, and the
instance and its boot volume read `TERMINATED` only once that work request has
been read `SUCCEEDED`; a detach puts the attachment `DETACHING`, and it reads
`DETACHED` once its work request has succeeded. A work request's readings are
scripted per case, one per poll, the last repeated; a case scripts each work
request in the order they are made, the last script serving every one after.

The credential reaches the module the way a run's does: out of `physical`'s
configuration, read by `pulumi config get` under the passphrase in the stack's
workstation slot. `pulumi` is replaced by a runner that answers from a mapping
and records the passphrase it was handed.
"""

# The SDK ships no stubs; the same waiver the module under test carries.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from credentials_command_tree import named_leaves

from kluster import conventions
from kluster.lib import stack_environment
from kluster.scripts.credentials import cli, derived, oci_iam, oci_recovery, pulumi_config, workstation

COMPARTMENT = 'ocid1.compartment.oc1..physical'
AD = 'Uocm:PHX-AD-1'
USER = 'ocid1.user.oc1..physical'
PASSPHRASE = 'the passphrase in the slot'

CP1 = 'ocid1.instance.oc1.phx.cp1'
CP2 = 'ocid1.instance.oc1.phx.cp2'
BOOT1 = 'ocid1.bootvolume.oc1.phx.cp1'
BOOT2 = 'ocid1.bootvolume.oc1.phx.cp2'
VIP = 'ocid1.publicip.oc1.phx.vip1'
CACHE = 'ocid1.volume.oc1.phx.hathcache'
ATTACHMENT = 'ocid1.volumeattachment.oc1.phx.hathcache'

IN_PROGRESS = {'status': 'IN_PROGRESS', 'percentComplete': 40.0}
SUCCEEDED = {'status': 'SUCCEEDED', 'percentComplete': 100.0}
FAILED = {'status': 'FAILED', 'percentComplete': 10.0}


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    #: The `keyId` the request was signed with: `<tenancy>/<user>/<fingerprint>`.
    key_id: str
    user_agent: str


@dataclass
class FakeOci:
    """The service's side of the calls: resources by OCID, and what each work request reads, poll by poll."""

    instances: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    boot_volumes: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    boot_volume_attachments: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    volumes: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    volume_attachments: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    public_ips: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    #: What each work request a termination or a detach answers with reads, one entry per poll, in the
    #: order the work requests are made; the last serves every work request after it.
    scripts: list[list[dict[str, Any]]] = field(default_factory=lambda: [[IN_PROGRESS, SUCCEEDED]])
    #: The errors a work request that does not succeed carries.
    errors: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    #: False answers a termination or a detach with no work request, the change already made.
    answers_with_work_requests: bool = True
    requests: list[Request] = field(default_factory=list[Request])
    #: Per work request: its readings still to serve, and what its success does to the resources.
    _pending: dict[str, tuple[list[dict[str, Any]], Any]] = field(
        default_factory=dict[str, tuple[list[dict[str, Any]], Any]]
    )

    def instance(self, ocid: str, name: str, boot_volume: str, state: str = 'RUNNING') -> None:
        self.instances[ocid] = {
            'id': ocid,
            'displayName': name,
            'lifecycleState': state,
            'availabilityDomain': AD,
            'compartmentId': COMPARTMENT,
            'shape': 'VM.Standard.A1.Flex',
            'shapeConfig': {'ocpus': 4.0, 'memoryInGBs': 24.0},
            'region': 'phx',
        }
        self.boot_volumes[boot_volume] = {
            'id': boot_volume,
            'displayName': f'{name} (Boot Volume)',
            'lifecycleState': 'AVAILABLE',
            'availabilityDomain': AD,
            'compartmentId': COMPARTMENT,
        }
        self.boot_volume_attachments.append(
            {
                'id': f'ocid1.instance.bva.{name}',
                'bootVolumeId': boot_volume,
                'instanceId': ocid,
                'lifecycleState': 'ATTACHED',
                'availabilityDomain': AD,
                'compartmentId': COMPARTMENT,
            }
        )

    def volume(self, ocid: str, name: str, *, attached_to: str | None) -> None:
        self.volumes[ocid] = {
            'id': ocid,
            'displayName': name,
            'lifecycleState': 'AVAILABLE',
            'availabilityDomain': AD,
            'compartmentId': COMPARTMENT,
        }
        if attached_to is not None:
            self.volume_attachments[ATTACHMENT] = {
                'attachmentType': 'paravirtualized',
                'id': ATTACHMENT,
                'instanceId': attached_to,
                'volumeId': ocid,
                'lifecycleState': 'ATTACHED',
                'availabilityDomain': AD,
                'compartmentId': COMPARTMENT,
            }

    def answer(self, method: str, path: str, query: dict[str, list[str]]) -> tuple[int, Any, dict[str, str]]:
        """The status, the JSON body and the headers the service answers a request with."""
        parts = path.removeprefix('/20160918/').strip('/').split('/')
        match method, parts:
            case 'GET', ['instances', ocid]:
                return self._found(self.instances.get(ocid))
            case 'DELETE', ['instances', ocid] if ocid in self.instances:
                if query.get('preserveBootVolume') != ['false']:
                    return self._invalid(f'a termination that keeps the boot volume: {query}')
                self.instances[ocid]['lifecycleState'] = 'TERMINATING'
                boots = [a['bootVolumeId'] for a in self.boot_volume_attachments if a['instanceId'] == ocid]
                return self._accepted('TerminateInstance', lambda: self._terminated(ocid, boots))
            case 'GET', ['bootVolumeAttachments']:
                if query.get('availabilityDomain') != [AD] or query.get('compartmentId') != [COMPARTMENT]:
                    return self._invalid(f'boot volume attachments listed outside the instance: {query}')
                listed = [a for a in self.boot_volume_attachments if a['instanceId'] == query['instanceId'][0]]
                return 200, listed, {}
            case 'GET', ['bootVolumes', ocid]:
                return self._found(self.boot_volumes.get(ocid))
            case 'GET', ['volumes', ocid]:
                return self._found(self.volumes.get(ocid))
            case 'GET', ['publicIps', ocid]:
                return self._found(self.public_ips.get(ocid))
            case 'GET', ['volumeAttachments']:
                if query.get('compartmentId') != [COMPARTMENT]:
                    return self._invalid(f'volume attachments listed outside the volume: {query}')
                volume = query['volumeId'][0]
                return 200, [a for a in self.volume_attachments.values() if a['volumeId'] == volume], {}
            case 'GET', ['volumeAttachments', ocid]:
                return self._found(self.volume_attachments.get(ocid))
            case 'DELETE', ['volumeAttachments', ocid] if ocid in self.volume_attachments:
                self.volume_attachments[ocid]['lifecycleState'] = 'DETACHING'
                return self._accepted('DetachVolume', lambda: self._detached(ocid))
            case 'GET', ['workRequests', ocid] if ocid in self._pending:
                return 200, self._poll(ocid), {}
            case 'GET', ['workRequests', ocid, 'errors'] if ocid in self._pending:
                return 200, self.errors, {}
            case _:
                return self._found(None)

    def _invalid(self, why: str) -> tuple[int, Any, dict[str, str]]:
        """A 400, which the SDK does not retry: a request the fake did not expect fails the case at once."""
        return 400, {'code': 'InvalidParameter', 'message': why}, {'opc-request-id': 'request-400'}

    def _found(self, resource: dict[str, Any] | None) -> tuple[int, Any, dict[str, str]]:
        if resource is None:
            body = {
                'code': 'NotAuthorizedOrNotFound',
                'message': 'Authorization failed or requested resource not found.',
            }
            return 404, body, {'opc-request-id': 'request-404'}
        return 200, resource, {}

    def _accepted(self, operation: str, on_success: Any) -> tuple[int, Any, dict[str, str]]:
        if not self.answers_with_work_requests:
            on_success()
            return 204, None, {}
        ocid = f'ocid1.coreservicesworkrequest.oc1.phx.{len(self._pending) + 1}'
        script = self.scripts.pop(0) if len(self.scripts) > 1 else self.scripts[0]
        readings = [{'id': ocid, 'operationType': operation, 'compartmentId': COMPARTMENT, **r} for r in script]
        self._pending[ocid] = (readings, on_success)
        return 204, None, {'opc-work-request-id': ocid}

    def _poll(self, ocid: str) -> dict[str, Any]:
        readings, on_success = self._pending[ocid]
        reading = readings.pop(0) if len(readings) > 1 else readings[0]
        if reading['status'] == 'SUCCEEDED':
            on_success()
        return reading

    def _terminated(self, ocid: str, boots: Sequence[str]) -> None:
        self.instances[ocid]['lifecycleState'] = 'TERMINATED'
        for boot in boots:
            self.boot_volumes[boot]['lifecycleState'] = 'TERMINATED'
        for attachment in self.boot_volume_attachments:
            if attachment['instanceId'] == ocid:
                attachment['lifecycleState'] = 'DETACHED'

    def _detached(self, ocid: str) -> None:
        self.volume_attachments[ocid]['lifecycleState'] = 'DETACHED'

    def calls(self) -> list[str]:
        """The requests made, as `METHOD path`, the version prefix and the query left out."""
        return [f'{r.method} {r.path.removeprefix("/20160918")}' for r in self.requests]


class _Handler(BaseHTTPRequestHandler):
    fake: ClassVar[FakeOci]

    def _serve(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        signed = re.search(r'keyId="([^"]+)"', self.headers.get('authorization', ''))
        self.fake.requests.append(
            Request(
                self.command, url.path, query, signed.group(1) if signed else '', self.headers.get('user-agent', '')
            )
        )
        status, body, headers = self.fake.answer(self.command, url.path, query)
        payload = b'' if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(payload)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        _ = self.wfile.write(payload)

    # The method names `BaseHTTPRequestHandler` dispatches to.
    do_GET = _serve
    do_DELETE = _serve

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- the signature it overrides
        pass


@pytest.fixture
def fake() -> FakeOci:
    return FakeOci()


@pytest.fixture
def endpoint(fake: FakeOci) -> Iterator[str]:
    handler = type('Handler', (_Handler,), {'fake': fake})
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(autouse=True)
def short_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every wait's stop-loss cut to seconds: a wait that would not end fails the case rather than the run."""
    for bound in ('TERMINATE_SECONDS', 'SETTLE_SECONDS'):
        monkeypatch.setattr(oci_recovery, bound, 10)
    monkeypatch.setattr(oci_recovery, 'POLL_SECONDS', 1)


@dataclass
class ConfigRunner:
    """`pulumi`, answering `config get <key>` from `values` and recording the passphrase each run was handed."""

    values: Mapping[str, str]
    passphrases: list[str | None] = field(default_factory=list[str | None])

    def __call__(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        self.passphrases.append(env.get(stack_environment.PASSPHRASE_ENV))
        match list(args):
            case ['config', 'get', key, '--stack', 'physical'] if key in self.values:
                return self.values[key] + '\n'
            case _:
                raise pulumi_config.SlotRefused(f'pulumi {" ".join(args)}: no such key')


@pytest.fixture
def key() -> oci_iam.KeyPair:
    return oci_iam.generate_key()


@pytest.fixture
def runner(key: oci_iam.KeyPair) -> ConfigRunner:
    return ConfigRunner({derived.OCI_USER_KEY: USER, derived.OCI_PRIVATE_KEY_KEY: key.private_pem})


@pytest.fixture
def physical(credential_files: Path, runner: ConfigRunner, tmp_path: Path) -> pulumi_config.Stack:
    """`physical`'s configuration as a run opens it, under the passphrase in its slot, with `pulumi` replaced."""
    _ = workstation.write(workstation.physical_passphrase_path(), PASSPHRASE)
    return pulumi_config.Stack(
        name=pulumi_config.PHYSICAL,
        directory=tmp_path,
        environment=pulumi_config.BackendEnvironment(physical=oci_recovery.slot_passphrase),
        run=runner,
    )


@pytest.fixture
def session(physical: pulumi_config.Stack, endpoint: str) -> oci_recovery.Session:
    """The clients a run builds: the key read out of `physical`'s configuration."""
    return oci_recovery.Session.open(oci_recovery.physical_key(physical), endpoint=endpoint)


@pytest.fixture
def command(physical: pulumi_config.Stack, endpoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`credentials derived oci-physical …` as it runs, its stack the case's and its clients pointed at the fake."""

    def physical_stack(_bundle_dir: Path) -> pulumi_config.Stack:
        return physical

    def open_session(stack: pulumi_config.Stack) -> oci_recovery.Session:
        return oci_recovery.Session.open(oci_recovery.physical_key(stack), endpoint=endpoint)

    monkeypatch.setattr(oci_recovery, 'physical_stack', physical_stack)
    monkeypatch.setattr(oci_recovery, 'open_session', open_session)


def _lines(readings: Sequence[oci_recovery.Reading]) -> list[str]:
    return [reading.line() for reading in readings]


# -- terminate ------------------------------------------------------------------------------------------------


def test_terminate_returns_only_once_the_work_request_succeeded_and_both_read_terminated(
    session: oci_recovery.Session, fake: FakeOci
) -> None:
    fake.instance(CP1, 'kluster-cp1', BOOT1)

    readings = oci_recovery.terminate(session, CP1)

    assert _lines(readings) == [
        f'instance kluster-cp1 {CP1} TERMINATED',
        f'boot-volume kluster-cp1 (Boot Volume) {BOOT1} TERMINATED',
    ]
    calls = fake.calls()
    delete = calls.index(f'DELETE /instances/{CP1}')
    polls = [i for i, call in enumerate(calls) if call.startswith('GET /workRequests/')]
    # The boot volume is found while it is still attached, and the work
    # request is read until it succeeded -- twice, by the script -- before the
    # instance is read again.
    assert calls.index('GET /bootVolumeAttachments') < delete < polls[0]
    assert len(polls) == 2
    assert calls.index(f'GET /instances/{CP1}', delete) > polls[-1]


def test_terminate_takes_each_instance_in_turn(session: oci_recovery.Session, fake: FakeOci) -> None:
    fake.instance(CP1, 'kluster-cp1', BOOT1)
    fake.instance(CP2, 'kluster-cp2', BOOT2)

    for ocid in (CP1, CP2):
        _ = oci_recovery.terminate(session, ocid)

    assert fake.instances[CP1]['lifecycleState'] == fake.instances[CP2]['lifecycleState'] == 'TERMINATED'
    assert fake.boot_volumes[BOOT2]['lifecycleState'] == 'TERMINATED'


def test_a_failed_work_request_is_refused_with_its_status_and_errors(
    session: oci_recovery.Session, fake: FakeOci
) -> None:
    fake.instance(CP1, 'kluster-cp1', BOOT1)
    fake.scripts = [[FAILED]]
    fake.errors = [
        {'code': 'InternalError', 'message': 'the host refused the termination', 'timestamp': '2026-11-16T00:00:00Z'}
    ]

    with pytest.raises(oci_recovery.OciRefused) as refused:
        _ = oci_recovery.terminate(session, CP1)

    message = str(refused.value)
    assert 'kluster-cp1' in message
    assert 'ended FAILED' in message
    assert 'InternalError: the host refused the termination' in message
    assert re.search(r'work request ocid1\.coreservicesworkrequest\.\S+', message)
    # Nothing after the failure: the instance is not read as though it were gone.
    assert fake.calls()[-1].endswith('/errors')


def test_a_terminate_through_the_command_stops_at_the_first_failure(
    command: None, fake: FakeOci, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.instance(CP1, 'kluster-cp1', BOOT1)
    fake.instance(CP2, 'kluster-cp2', BOOT2)
    fake.scripts = [[FAILED]]

    code = cli.main(['derived', derived.OCI_PHYSICAL_ROW, 'terminate', CP1, CP2])

    assert code != 0
    assert f'DELETE /instances/{CP2}' not in fake.calls()
    assert fake.instances[CP2]['lifecycleState'] == 'RUNNING'
    assert capsys.readouterr().out == ''


def test_a_termination_answered_with_no_work_request_waits_on_the_instance(
    session: oci_recovery.Session, fake: FakeOci, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    fake.instance(CP1, 'kluster-cp1', BOOT1)
    fake.answers_with_work_requests = False

    readings = oci_recovery.terminate(session, CP1)

    assert [r.state for r in readings] == ['TERMINATED', 'TERMINATED']
    assert not [call for call in fake.calls() if call.startswith('GET /workRequests/')]
    assert 'answered with no work request' in caplog.text


def test_an_instance_already_terminated_is_read_and_left(session: oci_recovery.Session, fake: FakeOci) -> None:
    fake.instance(CP1, 'kluster-cp1', BOOT1, state='TERMINATED')

    readings = oci_recovery.terminate(session, CP1)

    assert _lines(readings) == [f'instance kluster-cp1 {CP1} TERMINATED']
    assert fake.calls() == [f'GET /instances/{CP1}']


def test_a_refusal_from_oci_is_reported_by_name(session: oci_recovery.Session, fake: FakeOci) -> None:
    with pytest.raises(oci_recovery.OciRefused) as refused:
        _ = oci_recovery.terminate(session, CP1)

    assert 'HTTP 404 NotAuthorizedOrNotFound' in str(refused.value)
    assert 'request-404' in str(refused.value)


# -- read -----------------------------------------------------------------------------------------------------


def test_read_tells_each_kind_by_its_ocid(session: oci_recovery.Session, fake: FakeOci) -> None:
    fake.instance(CP1, 'kluster-cp1', BOOT1)
    fake.volume(CACHE, 'kluster-hath-cache', attached_to=CP1)
    fake.public_ips[VIP] = {
        'id': VIP,
        'displayName': 'kluster-vip1',
        'lifecycleState': 'AVAILABLE',
        'lifetime': 'RESERVED',
        'ipAddress': '203.0.113.10',
        'compartmentId': COMPARTMENT,
    }

    lines = [line for ocid in (CP1, VIP, CACHE, BOOT1) for line in _lines(oci_recovery.read(session, ocid))]

    assert lines == [
        f'instance kluster-cp1 {CP1} RUNNING shape=VM.Standard.A1.Flex ocpus=4 memory-gb=24',
        f'public-ip kluster-vip1 {VIP} AVAILABLE address=203.0.113.10 lifetime=RESERVED assigned=-',
        f'volume kluster-hath-cache {CACHE} AVAILABLE attached-to={CP1}',
        f'boot-volume kluster-cp1 (Boot Volume) {BOOT1} AVAILABLE',
    ]


def test_a_resource_oci_does_not_hold_reads_absent(session: oci_recovery.Session) -> None:
    assert _lines(oci_recovery.read(session, BOOT2)) == [f'boot-volume - {BOOT2} absent']


def test_read_refuses_a_kind_it_does_not_read(session: oci_recovery.Session, fake: FakeOci) -> None:
    with pytest.raises(oci_recovery.OciRefused, match='names a vcn resource'):
        _ = oci_recovery.read(session, 'ocid1.vcn.oc1.phx.network')
    assert fake.requests == []


# -- detach ---------------------------------------------------------------------------------------------------


def test_detach_waits_for_the_attachment_to_read_detached(session: oci_recovery.Session, fake: FakeOci) -> None:
    fake.volume(CACHE, 'kluster-hath-cache', attached_to=CP1)

    readings = oci_recovery.detach(session, CACHE)

    assert _lines(readings) == [f'volume kluster-hath-cache {CACHE} AVAILABLE attached-to=-']
    calls = fake.calls()
    delete = calls.index(f'DELETE /volumeAttachments/{ATTACHMENT}')
    polls = [i for i, call in enumerate(calls) if call.startswith('GET /workRequests/')]
    assert delete < polls[0] and len(polls) == 2
    assert calls.index(f'GET /volumeAttachments/{ATTACHMENT}', delete) > polls[-1]


def test_a_volume_on_no_instance_is_read_and_left(session: oci_recovery.Session, fake: FakeOci) -> None:
    fake.volume(CACHE, 'kluster-hath-cache', attached_to=None)

    assert _lines(oci_recovery.detach(session, CACHE)) == [f'volume kluster-hath-cache {CACHE} AVAILABLE attached-to=-']
    assert not [call for call in fake.calls() if call.startswith('DELETE')]


# -- the credential -------------------------------------------------------------------------------------------


def test_every_call_signs_as_physicals_key_under_the_slots_passphrase(
    session: oci_recovery.Session, fake: FakeOci, runner: ConfigRunner, key: oci_iam.KeyPair
) -> None:
    fake.instance(CP1, 'kluster-cp1', BOOT1)

    _ = oci_recovery.terminate(session, CP1)

    assert runner.passphrases and set(runner.passphrases) == {PASSPHRASE}
    tenancy = conventions.OCI_TENANCY.tenancy_ocid
    assert {r.key_id for r in fake.requests} == {f'{tenancy}/{USER}/{oci_iam.fingerprint(key.private_pem)}'}
    assert all(oci_recovery.USER_AGENT in r.user_agent for r in fake.requests)


def test_no_line_of_a_run_carries_the_key(
    command: None,
    fake: FakeOci,
    key: oci_iam.KeyPair,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The whole command, from the read of the key to the refusal it ends in,
    # with the clients pointed at the fake: every line it logs or prints, at
    # every level, is held against the key's.
    caplog.set_level(logging.DEBUG)
    fake.instance(CP1, 'kluster-cp1', BOOT1)
    fake.instance(CP2, 'kluster-cp2', BOOT2)
    fake.scripts = [[IN_PROGRESS, SUCCEEDED], [IN_PROGRESS, FAILED]]
    fake.errors = [{'code': 'InternalError', 'message': 'no', 'timestamp': '2026-11-16T00:00:00Z'}]

    code = cli.main(['derived', derived.OCI_PHYSICAL_ROW, 'terminate', CP1, CP2])
    captured = capfd.readouterr()

    assert code != 0
    body = [line for line in key.private_pem.splitlines() if line and not line.startswith('-----')]
    written = '\n'.join((captured.out, captured.err, caplog.text))
    assert [line for line in body if line in written] == []
    assert f'kluster-cp1 {CP1} TERMINATED' in captured.out and 'ended FAILED' in caplog.text, (
        'the run neither printed a reading nor logged its refusal, so the check above saw nothing'
    )


def test_a_missing_slot_is_refused_with_the_command_that_fills_it(credential_files: Path, tmp_path: Path) -> None:
    stack = oci_recovery.physical_stack(tmp_path / 'no-bundle')

    with pytest.raises(pulumi_config.PassphraseMissing) as refused:
        _ = stack.env

    assert ('derived', stack_environment.PHYSICAL_PASSPHRASE_ROW, 'recover') in named_leaves(str(refused.value))


def test_physicals_configuration_opens_under_its_own_slot(credential_files: Path, tmp_path: Path) -> None:
    _ = workstation.write(workstation.physical_passphrase_path(), PASSPHRASE)

    stack = oci_recovery.physical_stack(tmp_path / 'no-bundle')

    assert stack.name == pulumi_config.PHYSICAL
    assert stack.env[stack_environment.PASSPHRASE_ENV] == PASSPHRASE


def test_a_value_that_is_no_key_is_refused_without_quoting_it(credential_files: Path, tmp_path: Path) -> None:
    _ = workstation.write(workstation.physical_passphrase_path(), PASSPHRASE)
    stack = pulumi_config.Stack(
        name=pulumi_config.PHYSICAL,
        directory=tmp_path,
        environment=pulumi_config.BackendEnvironment(physical=oci_recovery.slot_passphrase),
        run=ConfigRunner({derived.OCI_USER_KEY: USER, derived.OCI_PRIVATE_KEY_KEY: 'not-a-key-at-all'}),
    )

    with pytest.raises(oci_recovery.OciRefused) as refused:
        _ = oci_recovery.physical_key(stack)

    assert derived.OCI_PRIVATE_KEY_KEY in str(refused.value)
    assert 'not-a-key-at-all' not in str(refused.value)
    assert ('derived', derived.OCI_PHYSICAL_ROW, 'mint') in named_leaves(str(refused.value))
