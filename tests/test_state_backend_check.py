"""The state backend's two checks from the cluster: the program each CronJob runs, against its seams and in its image.

The program runs in-process through its two seams -- `openssl`, answered here
with a handshake's transcript and the leaf's fields as `openssl x509` prints
them, and the calls to B2, answered by the shared fake account
(`tests/b2_api.py`) with a key the freshness mint delivers -- under a clock
the case pins, so a verdict is a function of the leaf's dates, the newest
dump's stamp and `now`, never of how long the case took (framework/testing.md
§7 item 5). The settings are the ones the component renders
(`components.monitoring.state_backend.settings`), read as the program reads
them, so each boundary is the threshold the cluster runs with.

One case runs the program in the pinned `oci-sdk` image, its interpreter and
its `openssl`, against a listener in the same container that speaks
Postgres's SSL request and then TLS, so the real tool's output is held to the
program's reading of it. That case runs where podman is on `PATH`; anywhere
else it is skipped, naming why, except on CI's runner, where it fails.
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess as sp
import sys
import urllib.error
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import image_busybox
import process_sessions
import pytest
from b2_api import FakeApi
from credentials_command_tree import commands, named_leaves
from cryptography import x509
from fences import prose
from memory_kit import MemoryKit
from section_numbers import sections
from state_dump_box import Box

from kluster import conventions
from kluster.components.monitoring import state_backend
from kluster.components.monitoring import state_backend_check as check
from kluster.lib.state_backend import settings, state
from kluster.scripts.credentials import b2, derived, entries, masters, pki
from kluster.scripts.credentials.kdbx import KdbxStore

ROOT = Path(__file__).resolve().parents[1]
UTC = dt.UTC
NOW = dt.datetime(2026, 11, 19, 6, 23, tzinfo=UTC)
SEED_ENTRY = entries.SEEDS['b2'].entry
#: The leaf that mints the freshness key again, as a refusal naming it is read.
MINT = ('derived', derived.B2_FRESHNESS_DUMPS_ROW, 'mint')


def _rendered(tmp_path: Path, **edits: object) -> check.Settings:
    """The settings the component renders, with `edits` laid over them, as the program reads them."""
    path = tmp_path / state_backend.SETTINGS_FILE
    path.write_text(json.dumps(json.loads(state_backend.settings()) | edits))
    return check.Settings.read(path)


@pytest.fixture
def rendered(tmp_path: Path) -> check.Settings:
    return _rendered(tmp_path)


def _hours(age: dt.timedelta) -> str:
    """An age as a verdict prints it."""
    return f'{age / dt.timedelta(hours=1):.1f} h'


# -- The settings and the relations -----------------------------------------------


def test_the_settings_the_component_renders_are_the_backends_as_the_program_reads_them(
    rendered: check.Settings,
) -> None:
    key = conventions.sealed.B2_FRESHNESS_DUMPS

    assert rendered == check.Settings(
        address=settings.ADDRESS,
        port=settings.PORT,
        alert_margin=settings.EXPIRY_ALERT_MARGIN,
        dump_prefix=b2.DUMP_PREFIX,
        dump_max_age=settings.DUMP_MAX_AGE,
        key_secret=f'{key.namespace}/{key.name}',
    )


@pytest.mark.parametrize(
    ('edit', 'named'),
    [
        ({'address': 'not-an-address'}, 'not-an-address'),
        ({'port': 0}, "'port'"),
        ({'alert_margin_seconds': '30d'}, "'alert_margin_seconds'"),
        ({'dump_prefix': ''}, "'dump_prefix'"),
    ],
    ids=['an address that is none', 'no port', 'a margin that is no number', 'no prefix'],
)
def test_settings_a_run_cannot_read_are_refused_by_the_field(
    tmp_path: Path, edit: dict[str, object], named: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(named)):
        _ = _rendered(tmp_path, **edit)


def test_the_alert_margin_opens_after_the_renewal_margin() -> None:
    # The check is the backstop for an appliance whose stack nobody has run: a
    # backstop that fires before a run of the stack would have named the
    # reissue is the first reporter rather than the last.
    assert dt.timedelta(0) < settings.EXPIRY_ALERT_MARGIN < settings.RENEWAL_MARGIN


def test_the_program_imports_nothing_beside_the_standard_library() -> None:
    """The `oci-sdk` image holds the interpreter and the SDK, and no code of this repository."""
    tree = ast.parse(Path(check.__file__).read_text())
    imported = {
        name.split('.')[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ''])
    }

    assert imported <= set(sys.stdlib_module_names) | {'__future__'}


#: A playbook as the program writes it: a document from the repository's root, and a section.
PLAYBOOK = re.compile(r'(?P<document>docs/\S+\.md) §(?P<section>\d+(?:\.\d+)*)')


def _playbooks_carried() -> set[str]:
    """The module names the program's failed verdicts are built with as their playbook, read off its source."""
    carried: set[str] = set()
    for node in ast.walk(ast.parse(Path(check.__file__).read_text())):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Verdict'):
            continue
        given = [*node.args[2:], *(keyword.value for keyword in node.keywords if keyword.arg == 'playbook')]
        carried |= {value.id for value in given if isinstance(value, ast.Name)}
    return carried


def test_every_playbook_a_verdict_names_is_a_section_that_exists() -> None:
    """A finding whose playbook does not resolve sends its reader to nothing.

    The references are read off the module, so a playbook added later is held
    to this without an edit here, and every failed verdict is built with one
    of them.
    """
    references = {
        name: value for name, value in vars(check).items() if isinstance(value, str) and PLAYBOOK.fullmatch(value)
    }
    carried = _playbooks_carried()

    assert carried, 'no verdict of the program names a playbook'
    assert carried <= set(references), carried - set(references)
    for name, reference in references.items():
        match = PLAYBOOK.fullmatch(reference)
        assert match is not None
        document = ROOT / match['document']
        assert document.is_file(), f'{name}: {reference}'
        assert match['section'] in sections(prose(document.read_text())), f'{name}: {reference}'


def test_the_mint_a_refusal_names_is_a_command_of_the_tree() -> None:
    assert list(MINT) in commands()
    assert named_leaves(f'`{check.MINT}`') == [MINT]


# -- The certificate --------------------------------------------------------------


@pytest.fixture(scope='module')
def authority() -> pki.Authority:
    return pki.Authority.from_pem(pki.generate_ca_key())


def _transcript(*certificates: bytes) -> str:
    """What `openssl s_client -showcerts` prints for a chain: the leaf first, each block after its index line."""
    chain = ''.join(f' {index} s:CN=x\n   i:CN=y\n{pem.decode()}' for index, pem in enumerate(certificates))
    return f'CONNECTED(00000003)\n---\nCertificate chain\n{chain}---\nServer certificate\nsubject=CN=x\n'


def _x509_fields(pem: str) -> str:
    """What `openssl x509 -noout -startdate -enddate -ext subjectAltName -dateopt iso_8601` prints for a certificate.

    The case in the pinned image holds the program's reading of it to the
    image's own `openssl`.
    """
    certificate = x509.load_pem_x509_certificate(pem.encode())
    stamp = '%Y-%m-%d %H:%M:%SZ'
    lines = [
        f'notBefore={certificate.not_valid_before_utc.strftime(stamp)}',
        f'notAfter={certificate.not_valid_after_utc.strftime(stamp)}',
    ]
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return '\n'.join(lines) + '\n'
    addresses = ', '.join(f'IP Address:{it}' for it in san.get_values_for_type(x509.IPAddress))
    return '\n'.join([*lines, 'X509v3 Subject Alternative Name: ', f'    {addresses}']) + '\n'


class Openssl:
    """`openssl` as a case sets it up: `s_client` answers with what the case put there, `x509` reads what it is handed."""

    def __init__(self, stdout: str = '', returncode: int = 0, stderr: str = '', hangs: bool = False) -> None:
        self.stdout: str = stdout
        self.returncode: int = returncode
        self.stderr: str = stderr
        self.hangs: bool = hangs
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], stdin: str) -> sp.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[0] == 'x509':
            return sp.CompletedProcess(args=list(argv), returncode=0, stdout=_x509_fields(stdin), stderr='')
        if self.hangs:
            raise sp.TimeoutExpired(cmd=list(argv), timeout=check.HANDSHAKE_TIMEOUT)
        return sp.CompletedProcess(args=list(argv), returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


def _serving(authority: pki.Authority, *, issued: dt.datetime, address: str = settings.ADDRESS) -> Openssl:
    """A box serving a leaf issued at `issued`, with the CA after it in the chain."""
    leaf = authority.issue_server(address, now=issued)
    return Openssl(_transcript(leaf.cert_pem, authority.certificate(now=issued).cert_pem))


def _left(days: int) -> dt.datetime:
    """The issuance that leaves a leaf `days` of validity at `NOW`."""
    return NOW - pki.LEAF_VALIDITY + dt.timedelta(days=days)


def _numbers(observed: str) -> list[str]:
    return re.findall(r'\d+', observed)


def _certificate(rendered: check.Settings, run: Openssl) -> list[check.Verdict]:
    return check.certificate(rendered, now=NOW, run=run)


def test_the_handshake_is_the_postgres_starttls_one_with_the_chain_printed(rendered: check.Settings) -> None:
    run = Openssl()

    _ = _certificate(rendered, run)

    # `-starttls postgres` because the port speaks Postgres before TLS, and
    # `-showcerts` because the leaf is read off the transcript.
    assert run.calls[0] == [
        's_client',
        '-connect',
        f'{settings.ADDRESS}:{settings.PORT}',
        '-starttls',
        'postgres',
        '-showcerts',
    ]


def test_the_programs_own_openssl_closes_its_stdin_and_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> Any:
        seen.update(kwargs)
        return sp.CompletedProcess(args=argv, returncode=0, stdout='', stderr='')

    monkeypatch.setattr(check.sp, 'run', fake_run)

    _ = check.openssl(['s_client'], '')

    # A handshake that succeeded ends at the end of its input, rather than
    # holding the connection open until the bound.
    assert seen['input'] == ''
    assert 0 < float(str(seen['timeout'])) < float('inf')


@pytest.mark.parametrize('days', [settings.EXPIRY_ALERT_MARGIN.days + 1, 400])
def test_a_leaf_with_more_than_the_margin_left_passes_naming_the_address(
    authority: pki.Authority, rendered: check.Settings, days: int
) -> None:
    (verdict,) = _certificate(rendered, _serving(authority, issued=_left(days)))

    assert verdict.passed
    assert settings.ADDRESS in verdict.observed
    assert str(days) in _numbers(verdict.observed)


def test_a_leaf_inside_the_margin_fails_into_the_reissue_playbook(
    authority: pki.Authority, rendered: check.Settings
) -> None:
    inside = settings.EXPIRY_ALERT_MARGIN.days - 1

    (verdict,) = _certificate(rendered, _serving(authority, issued=_left(inside)))

    assert verdict.playbook == check.REISSUE_PLAYBOOK
    assert str(inside) in _numbers(verdict.observed)
    assert str(settings.EXPIRY_ALERT_MARGIN.days) in _numbers(verdict.observed)


def test_an_expired_leaf_fails_by_its_date(authority: pki.Authority, rendered: check.Settings) -> None:
    run = _serving(authority, issued=_left(-1))

    (verdict,) = _certificate(rendered, run)

    assert verdict.playbook == check.REISSUE_PLAYBOOK
    assert (_left(-1) + pki.LEAF_VALIDITY).date().isoformat() in verdict.observed
    assert 'expired' in verdict.observed


def test_a_leaf_not_yet_valid_fails(authority: pki.Authority, rendered: check.Settings) -> None:
    (verdict,) = _certificate(rendered, _serving(authority, issued=NOW + dt.timedelta(days=1)))

    assert verdict.playbook == check.REISSUE_PLAYBOOK
    assert 'not valid until' in verdict.observed


def test_a_leaf_naming_another_address_fails_naming_both(authority: pki.Authority, rendered: check.Settings) -> None:
    # Every client connects with `sslmode=verify-full` by the address, so a
    # leaf with years left that names another one refuses them all.
    (verdict,) = _certificate(rendered, _serving(authority, issued=_left(400), address='192.0.2.10'))

    assert verdict.playbook == check.REISSUE_PLAYBOOK
    assert '192.0.2.10' in verdict.observed
    assert settings.ADDRESS in verdict.observed


def test_an_expired_leaf_naming_another_address_is_two_findings(
    authority: pki.Authority, rendered: check.Settings
) -> None:
    """Every finding a run has is printed: the log is what routes, and the alert is one either way."""
    verdicts = _certificate(rendered, _serving(authority, issued=_left(-1), address='192.0.2.10'))

    assert [it.playbook for it in verdicts] == [check.REISSUE_PLAYBOOK] * 2
    assert any('expired' in it.observed for it in verdicts)
    assert any('192.0.2.10' in it.observed for it in verdicts)


@pytest.mark.parametrize(
    ('run', 'said'),
    [
        (Openssl(returncode=1, stderr='connect:errno=111\nmore'), 'connect:errno=111'),
        (Openssl(hangs=True), f'{check.HANDSHAKE_TIMEOUT}s'),
        (Openssl(stdout='CONNECTED\n---\n'), 'served no certificate'),
    ],
    ids=['refused', 'no answer within the bound', 'no certificate'],
)
def test_a_box_that_completes_no_handshake_with_a_leaf_fails_into_the_rebuild_playbook(
    rendered: check.Settings, run: Openssl, said: str
) -> None:
    (verdict,) = _certificate(rendered, run)

    assert verdict.playbook == check.REBUILD_PLAYBOOK
    assert said in verdict.observed
    # Nothing is known of a leaf that was not served: no `x509` read follows.
    assert [call[0] for call in run.calls] == ['s_client']


def test_the_certificate_check_says_what_it_checks_before_the_handshake(
    rendered: check.Settings, caplog: pytest.LogCaptureFixture
) -> None:
    said: list[str] = []

    class Recording(Openssl):
        def __call__(self, argv: Sequence[str], stdin: str) -> sp.CompletedProcess[str]:
            said.extend(record.getMessage() for record in caplog.records)
            return super().__call__(argv, stdin)

    with caplog.at_level(logging.INFO, logger=check.log.name):
        _ = _certificate(rendered, Recording(returncode=1))

    assert any(f'{settings.ADDRESS}:{settings.PORT}' in line for line in said)


# -- The dumps --------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(b2.requests, 'get', fake.get)
    monkeypatch.setattr(b2.requests, 'post', fake.post)
    monkeypatch.setattr(
        conventions, 'B2_ACCOUNT', conventions.B2Account(region='us-west-002', account_id=fake.master.key_id)
    )
    return fake


@pytest.fixture
def minted(api: FakeApi) -> tuple[b2.AppKey, str]:
    """The freshness key as its mint delivers it, and the bucket it is confined to."""
    kit: KdbxStore = MemoryKit()
    root = masters.Credential(
        root=masters.ROOTS['b2'], values={'account-id': api.master.key_id, 'key': api.master.secret}
    )
    _ = b2.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY)
    session = b2.Session.from_entry(kit, SEED_ENTRY)
    # The dump bucket as the `state-backend` stack declares it: in the account
    # already, made by no call here.
    bucket_id = 'bucket-dumps'
    api.buckets[bucket_id] = {
        'bucketId': bucket_id,
        'bucketName': settings.B2_BUCKET,
        'bucketType': 'allPrivate',
        'lifecycleRules': [],
    }
    key, _ = b2.mint_freshness_dumps_key(session, bucket_id=bucket_id).deliver(lambda key: key)
    return key, bucket_id


def _key_directory(tmp_path: Path, key_id: str, key: str) -> Path:
    """The directory the Secret opens into, a file per data key, as the mint seals the key's two halves."""
    directory = tmp_path / 'key'
    directory.mkdir()
    sealed = derived._freshness_data(b2.AppKey(key_id=key_id, key=key))  # pyright: ignore[reportPrivateUsage] -- the writer the directory is held to
    assert set(sealed) == set(conventions.sealed.B2_FRESHNESS_DUMPS.keys)
    for name, value in sealed.items():
        _ = (directory / name).write_text(value)
    return directory


def _transport(api: FakeApi) -> check.Transport:
    """The program's calls to B2, answered by the fake account."""

    def send(request: check.Request) -> check.Answer:
        if request.method == 'GET':
            scheme, _, encoded = request.headers['Authorization'].partition(' ')
            assert scheme == 'Basic'
            key_id, _, key = base64.b64decode(encoded).decode().partition(':')
            response = api.get(request.url, auth=(key_id, key), timeout=check.B2_TIMEOUT)
        else:
            assert request.body is not None
            response = api.post(
                request.url, json=json.loads(request.body), headers=dict(request.headers), timeout=check.B2_TIMEOUT
            )
        return check.Answer(response.status_code, response.content)

    return send


def _dump(taken: dt.datetime) -> str:
    """The object the appliance uploads for a dump taken at `taken`."""
    return f'{b2.DUMP_PREFIX}{taken.strftime("%Y%m%dT%H%M%SZ")}.dump.age'


#: An hour either side of the greatest age: a newest dump `FRESH` old passes, and one `STALE` old fails.
FRESH = settings.DUMP_MAX_AGE - dt.timedelta(hours=1)
STALE = settings.DUMP_MAX_AGE + dt.timedelta(hours=1)


def _dumps(
    api: FakeApi, rendered: check.Settings, tmp_path: Path, key: b2.AppKey, *, transport: check.Transport | None = None
) -> list[check.Verdict]:
    directory = _key_directory(tmp_path, key.key_id, key.key)
    return check.dumps(rendered, directory, now=NOW, transport=transport or _transport(api))


def test_a_dump_younger_than_the_age_passes_naming_it_and_its_age(
    api: FakeApi, minted: tuple[b2.AppKey, str], rendered: check.Settings, tmp_path: Path
) -> None:
    key, bucket_id = minted
    api.objects[bucket_id] = [_dump(NOW - dt.timedelta(days=3)), _dump(NOW - FRESH)]

    (verdict,) = _dumps(api, rendered, tmp_path, key)

    assert verdict.passed
    assert _dump(NOW - FRESH) in verdict.observed
    assert _hours(FRESH) in verdict.observed


def test_the_minted_key_lists_the_prefix_and_nothing_else(
    api: FakeApi, minted: tuple[b2.AppKey, str], rendered: check.Settings, tmp_path: Path
) -> None:
    key, bucket_id = minted
    api.objects[bucket_id] = [_dump(NOW)]
    api.listings.clear()

    _ = _dumps(api, rendered, tmp_path, key)

    assert api.listings == [(key.key_id, bucket_id, b2.DUMP_PREFIX)]


def test_a_dump_older_than_the_age_fails_into_the_rebuild_playbook(
    api: FakeApi, minted: tuple[b2.AppKey, str], rendered: check.Settings, tmp_path: Path
) -> None:
    key, bucket_id = minted
    api.objects[bucket_id] = [_dump(NOW - STALE)]

    (verdict,) = _dumps(api, rendered, tmp_path, key)

    assert verdict.playbook == check.REBUILD_PLAYBOOK
    assert _hours(STALE) in verdict.observed


def test_an_empty_prefix_is_its_own_finding(
    api: FakeApi, minted: tuple[b2.AppKey, str], rendered: check.Settings, tmp_path: Path
) -> None:
    key, bucket_id = minted
    api.objects[bucket_id] = ['etcd/snapshot']

    (verdict,) = _dumps(api, rendered, tmp_path, key)

    # No dump holding state has landed: the box refuses to upload one that
    # holds no stack, or its timer never fired. Neither is a dump that
    # stopped, so the message is its own, and it says which prefix.
    assert verdict.playbook == check.REBUILD_PLAYBOOK
    assert f'no dump under {b2.DUMP_PREFIX}' in verdict.observed


def test_a_stranger_is_reported_by_name_beside_a_stale_newest_dump(
    api: FakeApi, minted: tuple[b2.AppKey, str], rendered: check.Settings, tmp_path: Path
) -> None:
    key, bucket_id = minted
    stranger = f'{b2.DUMP_PREFIX}notes.txt'
    api.objects[bucket_id] = [_dump(NOW - STALE), stranger]

    verdicts = _dumps(api, rendered, tmp_path, key)

    assert [it.playbook for it in verdicts] == [check.REBUILD_PLAYBOOK] * 2
    assert any(stranger in it.observed for it in verdicts)
    assert any(_hours(STALE) in it.observed for it in verdicts)


def test_a_stranger_beside_a_young_dump_is_the_one_finding(
    api: FakeApi, minted: tuple[b2.AppKey, str], rendered: check.Settings, tmp_path: Path
) -> None:
    key, bucket_id = minted
    stranger = f'{b2.DUMP_PREFIX}notes.txt'
    api.objects[bucket_id] = [_dump(NOW - FRESH), stranger]

    (verdict,) = _dumps(api, rendered, tmp_path, key)

    assert verdict.playbook == check.REBUILD_PLAYBOOK
    assert stranger in verdict.observed


def test_the_newest_dump_is_found_past_the_first_page(
    api: FakeApi, minted: tuple[b2.AppKey, str], rendered: check.Settings, tmp_path: Path
) -> None:
    key, bucket_id = minted
    api.objects[bucket_id] = [_dump(NOW - dt.timedelta(days=days)) for days in range(6, 0, -1)]
    api.file_page_limit = 2

    (verdict,) = _dumps(api, rendered, tmp_path, key)

    # B2 pages the listing at a size it chooses and names sort oldest first,
    # so a check that read one page would call a six-day-old dump the newest.
    assert verdict.passed
    assert _dump(NOW - dt.timedelta(days=1)) in verdict.observed


def _refusal_says_nothing_of_the_key(
    verdict: check.Verdict, caplog: pytest.LogCaptureFixture, key_id: str, key: str
) -> None:
    assert verdict.playbook == check.KEY_PLAYBOOK
    assert MINT in named_leaves(verdict.observed)
    assert conventions.sealed.B2_FRESHNESS_DUMPS.name in verdict.observed
    for said in (str(verdict), caplog.text):
        assert key_id not in said
        assert key not in said


def test_a_key_b2_refuses_at_the_authorization_goes_to_the_mint_naming_neither_half(
    api: FakeApi,
    minted: tuple[b2.AppKey, str],
    rendered: check.Settings,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key, _ = minted
    retired = b2.AppKey(key_id=key.key_id, key='a-key-b2-no-longer-holds')

    with caplog.at_level(logging.INFO):
        (verdict,) = _dumps(api, rendered, tmp_path, retired)

    _refusal_says_nothing_of_the_key(verdict, caplog, retired.key_id, retired.key)


def test_a_key_b2_refuses_at_the_listing_goes_to_the_mint_naming_neither_half(
    api: FakeApi,
    minted: tuple[b2.AppKey, str],
    rendered: check.Settings,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A key that authorizes and is then refused the prefix -- re-scoped, or
    # retired between the two calls -- is the same repair as one refused at
    # the authorization.
    key, bucket_id = minted
    api.objects[bucket_id] = [_dump(NOW)]
    api.keys[key.key_id].name_prefix = 'elsewhere/'
    api.listings.clear()

    with caplog.at_level(logging.INFO):
        (verdict,) = _dumps(api, rendered, tmp_path, key)

    assert api.listings == []
    _refusal_says_nothing_of_the_key(verdict, caplog, key.key_id, key.key)


def test_a_key_confined_to_no_bucket_goes_to_the_mint_naming_neither_half(
    api: FakeApi, rendered: check.Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    loose = api.add_key('anything', ('listFiles',))

    with caplog.at_level(logging.INFO):
        (verdict,) = _dumps(api, rendered, tmp_path, b2.AppKey(key_id=loose.key_id, key=loose.secret))

    assert 'b2_list_file_names' not in api.calls
    _refusal_says_nothing_of_the_key(verdict, caplog, loose.key_id, loose.secret)


def test_a_key_directory_missing_a_half_goes_to_the_mint(rendered: check.Settings, tmp_path: Path) -> None:
    directory = tmp_path / 'key'
    directory.mkdir()
    _ = (directory / check.KEY_ID_FILE).write_text('an-id')

    def unreached(request: check.Request) -> check.Answer:
        raise AssertionError(f'reached B2 with half a key: {request.url}')

    (verdict,) = check.dumps(rendered, directory, now=NOW, transport=unreached)

    assert verdict.playbook == check.KEY_PLAYBOOK
    assert check.KEY_FILE in verdict.observed


@pytest.mark.parametrize(
    ('failure', 'kind'),
    [
        (urllib.error.URLError('connection refused'), 'URLError'),
        (TimeoutError('timed out'), 'TimeoutError'),
        (check.Answer(503, b'{"code": "service_unavailable"}'), 'HTTP 503'),
    ],
    ids=['no connection', 'no answer within the bound', 'an error of its own'],
)
def test_b2_not_answering_goes_to_the_monitoring_section(
    api: FakeApi,
    minted: tuple[b2.AppKey, str],
    rendered: check.Settings,
    tmp_path: Path,
    failure: Exception | check.Answer,
    kind: str,
) -> None:
    key, _ = minted

    def failing(_request: check.Request) -> check.Answer:
        if isinstance(failure, check.Answer):
            return failure
        raise failure

    (verdict,) = _dumps(api, rendered, tmp_path, key, transport=failing)

    assert verdict.playbook == check.MONITORING_PLAYBOOK
    assert kind in verdict.observed


def test_the_dump_check_says_what_it_checks_before_it_reaches_b2(
    api: FakeApi,
    minted: tuple[b2.AppKey, str],
    rendered: check.Settings,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key, bucket_id = minted
    api.objects[bucket_id] = [_dump(NOW)]
    said: list[str] = []
    answer = _transport(api)

    def recording(request: check.Request) -> check.Answer:
        if not said:
            said.extend(record.getMessage() for record in caplog.records)
        return answer(request)

    with caplog.at_level(logging.INFO, logger=check.log.name):
        _ = _dumps(api, rendered, tmp_path, key, transport=recording)

    assert any(b2.DUMP_PREFIX in line for line in said)


# -- The name a dump is uploaded under ---------------------------------------------------


def test_the_check_reads_the_name_the_box_uploads_under(rendered: check.Settings, tmp_path: Path) -> None:
    """The seam is the box's writer: the objects under the prefix are named by the appliance's own script.

    The script is run as the box runs it, over fakes of what it calls
    (`state_dump_box`), so the name it hands the upload is the one it would
    hand B2, from its own format literal and its own clock.
    """
    box = Box(tmp_path)
    before = dt.datetime.now(UTC).replace(microsecond=0)

    ran = box.run(env={'B2_PREFIX': conventions.STATE_DUMP_PREFIX})

    after = dt.datetime.now(UTC)
    assert ran.returncode == 0, ran.stderr
    (put,) = box.of('curl')[2:]
    sent = put.header('X-Bz-File-Name')
    assert sent is not None
    taken = check.dumped_at(unquote(sent), rendered.dump_prefix)
    # Read, and read as the moment the box stamped -- the bracket is the
    # call itself, so a stall widens it and decides nothing.
    assert taken is not None, sent
    assert before <= taken <= after


def test_the_operators_dump_carries_the_same_stamp(rendered: check.Settings) -> None:
    # `state-backend dump` names a hand-taken dump by the same stamp
    # (`state.dump_name`), which is what makes it interchangeable with a
    # nightly one on disk; this holds the two writers to each other.
    stamp = state.dump_name(NOW).removeprefix(f'{settings.NAME}-').removesuffix('.dump.age')

    assert check.dumped_at(f'{rendered.dump_prefix}{stamp}.dump.age', rendered.dump_prefix) == NOW


def test_a_name_that_is_not_a_dumps_reads_as_none(rendered: check.Settings) -> None:
    prefix = rendered.dump_prefix

    assert check.dumped_at(f'{prefix}readme.txt', prefix) is None
    assert check.dumped_at(f'{prefix}20260916T062300Z.dump', prefix) is None
    assert check.dumped_at('etcd/20260916T062300Z.dump.age', prefix) is None


# -- The run ----------------------------------------------------------------------


def test_a_run_prints_every_verdict_and_exits_one_on_a_finding(
    authority: pki.Authority, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / state_backend.SETTINGS_FILE
    path.write_text(state_backend.settings())
    run = _serving(authority, issued=_left(-1), address='192.0.2.10')

    with caplog.at_level(logging.INFO, logger=check.log.name):
        status = check.main([check.CERTIFICATE, str(path)], now=NOW, run=run)

    failed = [record for record in caplog.records if ': FAILED — ' in record.getMessage()]
    assert status == 1
    assert len(failed) == 2
    assert all(record.levelno == logging.ERROR for record in failed)
    assert all(f'playbook: {check.REISSUE_PLAYBOOK}' in record.getMessage() for record in failed)


def test_a_run_whose_every_verdict_passed_exits_zero(
    authority: pki.Authority, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / state_backend.SETTINGS_FILE
    path.write_text(state_backend.settings())

    with caplog.at_level(logging.INFO, logger=check.log.name):
        status = check.main([check.CERTIFICATE, str(path)], now=NOW, run=_serving(authority, issued=_left(400)))

    assert status == 0
    assert any(record.getMessage().startswith(f'{check.CERTIFICATE}: ok — ') for record in caplog.records)


# -- The certificate check in its image -----------------------------------------------

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_state_backend_check`.
LABEL = f'kluster-test={Path(__file__).stem}'
#: The bounds on a pull, which reaches a registry, and on each other podman
#: command: stop-losses below the case bound, which nothing asserts on.
PULL_TIMEOUT = 45
COMMAND_TIMEOUT = 60
#: How long the container may run: the run's outer bound (framework/testing.md §1.2).
CONTAINER_TIMEOUT = 1200
#: The port the listener takes inside the container's own network namespace.
LISTENER_PORT = 15432

#: What runs in the container: it writes the files it is handed on its
#: standard input, listens on the loopback as the box's pooler does --
#: Postgres's 8-byte SSL request, answered `S`, and then TLS, so a client
#: that sends anything else first fails the handshake -- and runs the
#: program's certificate check against it, exiting with its status.
DRIVER = r"""
import json, os, socket, ssl, struct, subprocess, sys, tempfile, threading

given = json.load(sys.stdin)
work = tempfile.mkdtemp()
for name, text in given['files'].items():
    with open(os.path.join(work, name), 'w') as out:
        out.write(text)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(os.path.join(work, 'leaf.crt'), os.path.join(work, 'leaf.key'))
listener = socket.create_server(('127.0.0.1', given['port']))
SSL_REQUEST = struct.pack('!ii', 8, 80877103)


def serve():
    while True:
        connection, _ = listener.accept()
        with connection:
            if connection.recv(8, socket.MSG_WAITALL) != SSL_REQUEST:
                continue
            connection.sendall(b'S')
            try:
                with context.wrap_socket(connection, server_side=True) as tls:
                    tls.recv(1)
            except OSError:
                pass


threading.Thread(target=serve, daemon=True).start()
done = subprocess.run(
    [sys.executable, '-I', '-B', os.path.join(work, given['program']), 'certificate', os.path.join(work, 'settings.json')]
)
sys.exit(done.returncode)
"""


def _require_podman() -> None:
    if shutil.which('podman') is not None:
        return
    why = 'podman is not on PATH, and the certificate check runs in the pinned oci-sdk image with it'
    # GitHub sets `GITHUB_ACTIONS` on every job's runner, which has podman.
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        pytest.fail(f'{why}; CI runs this case')
    pytest.skip(why)


def _in_image(leaf: pki.Credential, *, margin: dt.timedelta) -> sp.CompletedProcess[bytes]:
    """The certificate check, run by the pinned image's interpreter and `openssl`, against a listener serving `leaf`."""
    _require_podman()
    reference = image_busybox.source(image_busybox.pinned('oci-sdk'))
    if process_sessions.run(['podman', 'image', 'exists', reference], timeout=COMMAND_TIMEOUT).returncode != 0:
        _ = process_sessions.run(['podman', 'pull', '--quiet', reference], timeout=PULL_TIMEOUT, check=True)
    rendered = json.loads(state_backend.settings()) | {
        'address': '127.0.0.1',
        'port': LISTENER_PORT,
        'alert_margin_seconds': int(margin.total_seconds()),
    }
    given = {
        'port': LISTENER_PORT,
        'program': state_backend.PROGRAM_FILE,
        'files': {
            state_backend.PROGRAM_FILE: state_backend.PROGRAM,
            'settings.json': json.dumps(rendered),
            'leaf.crt': leaf.cert_pem.decode(),
            'leaf.key': leaf.key_pem.decode(),
        },
    }
    name = f'kluster-test-state-backend-check-{uuid.uuid4().hex[:12]}'
    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        return process_sessions.run(
            [
                'podman',
                'run',
                '--rm',
                '--interactive',
                '--timeout',
                str(CONTAINER_TIMEOUT),
                '--label',
                LABEL,
                process_sessions.LOG_TO_FILE,
                '--name',
                name,
                '--network',
                'none',
                '--read-only',
                '--user',
                f'{state_backend.CHECK_USER}:{state_backend.CHECK_USER}',
                '--entrypoint',
                'python',
                reference,
                '-I',
                '-B',
                '-c',
                DRIVER,
            ],
            input=json.dumps(given).encode(),
            timeout=COMMAND_TIMEOUT + PULL_TIMEOUT,
        )


@pytest.fixture(scope='module')
def served(authority: pki.Authority) -> pki.Credential:
    """A leaf for the loopback address, issued now: the container's clock is the host's."""
    return authority.issue_server('127.0.0.1')


def _verdict_lines(output: bytes) -> list[str]:
    return [line.split(' ', 3)[-1] for line in output.decode().splitlines() if f'{check.CERTIFICATE}: ' in line]


def test_in_its_image_a_leaf_outside_the_margin_passes(served: pki.Credential) -> None:
    done = _in_image(served, margin=settings.EXPIRY_ALERT_MARGIN)

    assert done.returncode == 0, done.stderr.decode()
    (line,) = _verdict_lines(done.stderr)
    expires = x509.load_pem_x509_certificate(served.cert_pem).not_valid_after_utc.date().isoformat()
    assert line.startswith(f'{check.CERTIFICATE}: ok — the server certificate names 127.0.0.1 and expires on {expires}')


def test_in_its_image_a_leaf_inside_the_margin_fails_into_the_reissue_playbook(served: pki.Credential) -> None:
    done = _in_image(served, margin=pki.LEAF_VALIDITY + dt.timedelta(days=1))

    assert done.returncode == 1, done.stderr.decode()
    (line,) = _verdict_lines(done.stderr)
    assert line.startswith(f'{check.CERTIFICATE}: FAILED — ')
    assert line.endswith(f'alert margin; playbook: {check.REISSUE_PLAYBOOK}')
