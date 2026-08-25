"""The appliance's dump script, exercised without an appliance.

It is stdlib-only and runs unattended on a box nobody logs into, so the parts
worth pinning are the ones a silent change would break: that every B2 call
goes through `_request`, that the upload carries the checksum and length B2
validates against, and that a failing half of the pg_dump | age pipeline is
raised rather than uploaded as a truncated object.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import types
import urllib.request
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parent.parent / 'deploy' / 'state-backend' / 'state-dump.py'


def _load() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location('state_dump', _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state_dump = _load()


class _Call:
    def __init__(self, request: urllib.request.Request, timeout: int) -> None:
        self.request: urllib.request.Request = request
        self.timeout: int = timeout


class _FakeUrlopen:
    """Serve canned JSON, recording each request the script makes."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses: list[dict[str, Any]] = responses
        self.calls: list[_Call] = []

    def __call__(self, request: urllib.request.Request, timeout: int = 0) -> io.BytesIO:
        self.calls.append(_Call(request, timeout))
        payload = self.responses[len(self.calls) - 1]
        return io.BytesIO(json.dumps(payload).encode())


ACCOUNT = {
    'apiInfo': {'storageApi': {'apiUrl': 'https://api999.backblazeb2.com'}},
    'authorizationToken': 'account-token',
}
UPLOAD_TARGET = {
    'uploadUrl': 'https://pod-000.backblazeb2.com/b2api/v3/b2_upload_file',
    'authorizationToken': 'upload-token',
}


@pytest.fixture
def b2_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('B2_KEY_ID', 'key-id')
    monkeypatch.setenv('B2_KEY', 'key-secret')
    monkeypatch.setenv('B2_BUCKET_ID', 'bucket-id')


def test_request_parses_json_and_honours_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeUrlopen([{'ok': True}])
    monkeypatch.setattr(state_dump.urllib.request, 'urlopen', fake)

    result = state_dump._request(  # pyright: ignore[reportPrivateUsage]
        'https://example.invalid/x',
        headers={'Authorization': 'Basic abc'},
        data=b'body',
        timeout=42,
    )

    assert result == {'ok': True}
    (call,) = fake.calls
    assert call.timeout == 42
    assert call.request.full_url == 'https://example.invalid/x'
    assert call.request.data == b'body'
    assert call.request.get_header('Authorization') == 'Basic abc'


def test_upload_walks_authorize_then_get_url_then_put(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, b2_env: None
) -> None:
    body = b'age-encrypted bytes'
    path = tmp_path / 'state.dump.age'
    _ = path.write_bytes(body)

    fake = _FakeUrlopen([ACCOUNT, UPLOAD_TARGET, {'fileId': '4_z'}])
    monkeypatch.setattr(state_dump.urllib.request, 'urlopen', fake)

    state_dump.upload(path, 'kluster/state/20260825T023000Z.dump.age')

    authorize, get_url, put = fake.calls
    assert authorize.request.full_url == state_dump.AUTHORIZE_URL
    # Basic auth is the key id and secret, not a token.
    assert authorize.request.get_header('Authorization') == 'Basic a2V5LWlkOmtleS1zZWNyZXQ='

    # The api url comes from the authorize response, never hardcoded.
    assert get_url.request.full_url == 'https://api999.backblazeb2.com/b2api/v3/b2_get_upload_url'
    assert get_url.request.get_header('Authorization') == 'account-token'
    # Addressed by id: the writeFiles-only key cannot resolve a bucket name.
    body_sent = get_url.request.data
    assert isinstance(body_sent, bytes)
    assert json.loads(body_sent) == {'bucketId': 'bucket-id'}

    assert put.request.full_url == UPLOAD_TARGET['uploadUrl']
    assert put.request.get_header('Authorization') == 'upload-token'
    assert put.request.data == body
    assert put.request.get_header('X-bz-content-sha1') == hashlib.sha1(body).hexdigest()
    assert put.request.get_header('Content-length') == str(len(body))
    # A large dump outlives the default deadline.
    assert put.timeout == 600


def test_upload_percent_encodes_the_object_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, b2_env: None) -> None:
    path = tmp_path / 'state.dump.age'
    _ = path.write_bytes(b'x')
    fake = _FakeUrlopen([ACCOUNT, UPLOAD_TARGET, {}])
    monkeypatch.setattr(state_dump.urllib.request, 'urlopen', fake)

    state_dump.upload(path, 'kluster state/2026 08 25.dump.age')

    # Slashes stay: they are the object's path separator in B2.
    assert fake.calls[2].request.get_header('X-bz-file-name') == 'kluster%20state/2026%2008%2025.dump.age'


class _FakeProc:
    def __init__(self, status: int, stdout: io.BytesIO | None = None) -> None:
        self.status: int = status
        self.stdout: io.BytesIO | None = stdout

    def wait(self) -> int:
        return self.status


def _pipeline(monkeypatch: pytest.MonkeyPatch, *, pg: int, age: int) -> list[list[str]]:
    """Stand in for `pg_dump | age`, recording the argv of both halves."""
    seen: list[list[str]] = []

    def popen(argv: list[str], **_: object) -> _FakeProc:
        seen.append(argv)
        return _FakeProc(pg, io.BytesIO(b'dump')) if len(seen) == 1 else _FakeProc(age)

    monkeypatch.setattr(state_dump.sp, 'Popen', popen)
    return seen


@pytest.fixture
def pg_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('PG_ROLE', 'operator')
    monkeypatch.setenv('PG_DATABASE', 'pulumi_state')
    recipients = tmp_path / 'age-recipients.txt'
    _ = recipients.write_text('age1aaa\n\n  age1bbb  \n')
    monkeypatch.setattr(state_dump, 'RECIPIENTS', recipients)
    return tmp_path


def test_dump_encrypts_to_every_recipient(monkeypatch: pytest.MonkeyPatch, pg_env: Path) -> None:
    seen = _pipeline(monkeypatch, pg=0, age=0)

    state_dump.dump(pg_env / 'out.age')

    pg_argv, age_argv = seen
    assert pg_argv[:3] == ['podman', 'exec', state_dump.CONTAINER]
    assert '-Fc' in pg_argv and pg_argv[-2:] == ['operator', 'pulumi_state']
    # Blank lines are skipped and surrounding whitespace stripped, or age
    # would be handed a recipient it rejects.
    assert age_argv == ['/opt/bin/age', '--encrypt', '-r', 'age1aaa', '-r', 'age1bbb']


@pytest.mark.parametrize(('pg', 'age', 'message'), [(1, 0, 'pg_dump failed'), (0, 2, 'age failed')])
def test_dump_raises_when_either_half_fails(
    monkeypatch: pytest.MonkeyPatch, pg_env: Path, pg: int, age: int, message: str
) -> None:
    _ = _pipeline(monkeypatch, pg=pg, age=age)

    with pytest.raises(SystemExit, match=message):
        state_dump.dump(pg_env / 'out.age')
