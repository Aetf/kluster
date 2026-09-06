"""The appliance's nightly dump — the script, and the unit that runs it.

It is stdlib-only and runs unattended on a box nobody logs into, so the parts
worth pinning are the ones a silent change would break: that every B2 call
goes through `_request`, that the upload carries the checksum and length B2
validates against, that a failing step is raised rather than uploaded as a
truncated object, and that an archive `pg_restore` cannot list never becomes
an object at all.

The same standard reaches the wiring, which is why the last section reads the
Butane template. Where the archive is spooled, where the listing gets its
input, and whether the unit can time out at all are each a choice that fails
silently when it moves: the run still passes, on a box with no watcher, until
the night it does not.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import types
import urllib.request
from pathlib import Path
from typing import IO, Any, cast

import pytest

from kluster.scripts.state_backend import config, state

_SCRIPT = config.DEPLOY_DIR / config.DUMP_SCRIPT


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


#: A `pg_restore --list` output naming one table, header and all. The header
#: alone is the shape a dump of a database that lost its state produces.
HEADER = ';\n; Archive created at 2026-08-26 02:30:00 UTC\n;\n'
LISTING = HEADER + '215; 1259 16388 TABLE public stacks operator\n3057; 0 16388 TABLE DATA public stacks operator\n'

ARCHIVE = b'PGDMP-archive-bytes'

#: One entry of each kind, so a case can build the listing it wants.
DEFINITION = '215; 1259 16388 TABLE public stacks operator\n'
ROWS = '3057; 0 16388 TABLE DATA public stacks operator\n'
OTHER_DEFINITION = '216; 1259 16389 TABLE public leases operator\n'

#: Listings the two copies of the grammar have to answer the same way, and
#: with the same number. The grammar is written twice on purpose — nothing of
#: this repository is installed on the appliance, so the box parses its own
#: listing — and the hazard of a copy is that it drifts. The last two rows are
#: where it did: entries whose schema or name is missing, which one parser
#: could read as a table and the other could not. The row carrying a
#: definition *and* its rows is what makes this a comparison of counts rather
#: than of truthiness — a parser counting entries answers 2 there, one
#: counting names answers 1.
LISTINGS = [
    ('a header and nothing else', HEADER, 0),
    ('a table definition', HEADER + DEFINITION, 1),
    ('table rows', HEADER + ROWS, 1),
    ('one table, definition and rows both', HEADER + DEFINITION + ROWS, 1),
    ('two tables', HEADER + DEFINITION + ROWS + OTHER_DEFINITION, 2),
    ('an entry that is not a table', HEADER + '200; 1255 16390 FUNCTION public f() operator\n', 0),
    ('a definition cut off before its name', HEADER + '215; 1259 16388 TABLE public\n', 0),
    ('a TABLE DATA entry missing its name', HEADER + '3057; 0 16388 TABLE DATA public\n', 0),
]


@pytest.mark.parametrize(('what', 'listing', 'named'), LISTINGS)
def test_the_box_and_the_operator_read_a_listing_the_same_way(what: str, listing: str, named: int) -> None:
    """The claim that the two dumps are verified alike is worth only the parity.

    The box counts the names the operator's side collects, so the two answer
    the same number and not merely the same yes-or-no: a listing either side
    accepted and the other refused would make a nightly object and a
    hand-taken one different artefacts, and a count that drifts is how that
    starts.
    """
    assert state_dump.tables(listing) == named, what
    assert state.tables(listing) == sorted(set(state.tables(listing))), what
    assert len(state.tables(listing)) == named, what


class _Ran:
    """What the script reads off a finished process: a status, and its output."""

    def __init__(self, returncode: int, stdout: str = '', stderr: str = '') -> None:
        self.returncode: int = returncode
        self.stdout: str = stdout
        self.stderr: str = stderr


def _path_of(stream: object) -> Path | None:
    """The file a redirected stream is bound to, if it is bound to one."""
    name = getattr(stream, 'name', None)
    return Path(name) if isinstance(name, str) else None


def _drain(stream: object) -> bytes | None:
    """What a redirected input stream is carrying, read while it is still open."""
    return cast('IO[bytes]', stream).read() if hasattr(stream, 'read') else None


class _Invocation:
    """One subprocess the script started: its argv, and what it was wired to.

    The wiring is recorded at call time and not afterwards, because the
    script closes both files as soon as the call returns -- and it is the
    wiring, not the argv, that decides whether the listing reads the archive
    and where the archive is written.
    """

    def __init__(self, argv: list[str], kwargs: dict[str, Any]) -> None:
        stdin = kwargs.get('stdin')
        self.argv: list[str] = argv
        self.stdin: Path | None = _path_of(stdin)
        self.fed: bytes | None = _drain(stdin)
        self.stdout: Path | None = _path_of(kwargs.get('stdout'))


def _tools(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pg: int = 0,
    listing: str = LISTING,
    list_status: int = 0,
    complaint: str = '',
    age: int = 0,
) -> list[_Invocation]:
    """Stand in for pg_dump, `pg_restore --list` and age, recording each call."""
    seen: list[_Invocation] = []

    def run(argv: list[str], **kwargs: Any) -> _Ran:
        seen.append(_Invocation(argv, kwargs))
        stdout = cast('IO[bytes] | None', kwargs.get('stdout'))
        if len(seen) == 1:
            if stdout is not None:
                _ = stdout.write(ARCHIVE)
            return _Ran(pg)
        if len(seen) == 2:
            return _Ran(list_status, listing, complaint)
        if stdout is not None:
            _ = stdout.write(b'age-encrypted bytes')
        return _Ran(age)

    monkeypatch.setattr(state_dump.sp, 'run', run)
    return seen


@pytest.fixture
def pg_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('PG_ROLE', 'operator')
    monkeypatch.setenv('PG_DATABASE', 'pulumi_state')
    recipients = tmp_path / 'age-recipients.txt'
    _ = recipients.write_text('age1aaa\n\n  age1bbb  \n')
    monkeypatch.setattr(state_dump, 'RECIPIENTS', recipients)
    return tmp_path


def test_dump_lists_the_archive_and_encrypts_to_every_recipient(monkeypatch: pytest.MonkeyPatch, pg_env: Path) -> None:
    seen = _tools(monkeypatch)

    state_dump.dump(pg_env / 'out.age')

    pg, listing, encrypt = seen
    assert pg.argv[:3] == ['podman', 'exec', state_dump.CONTAINER]
    assert '-Fc' in pg.argv and pg.argv[-2:] == ['operator', 'pulumi_state']
    # The listing runs in the same container, reading the archive on standard
    # input rather than through a mount of the spool directory.
    assert listing.argv == ['podman', 'exec', '-i', state_dump.CONTAINER, 'pg_restore', '--list']
    # Blank lines are skipped and surrounding whitespace stripped, or age
    # would be handed a recipient it rejects.
    assert encrypt.argv == ['/opt/bin/age', '--encrypt', '-r', 'age1aaa', '-r', 'age1bbb']


def test_the_listing_is_fed_the_archive_the_dump_just_wrote(monkeypatch: pytest.MonkeyPatch, pg_env: Path) -> None:
    """`-i` in the argv is half of the plumbing; the handle is the other half.

    `pg_restore --list` with nothing on standard input reads an empty stream,
    which it refuses -- so losing the handle turns the check into a step that
    fails every night rather than one that silently passes. What makes it
    worth its own case is that the argv the case above asserts does not
    change when the handle goes.
    """
    seen = _tools(monkeypatch)

    state_dump.dump(pg_env / 'out.age')

    listing = seen[1]
    assert listing.stdin == pg_env / 'state.dump'
    assert listing.fed == ARCHIVE


def test_the_archive_is_spooled_beside_the_ciphertext(monkeypatch: pytest.MonkeyPatch, pg_env: Path) -> None:
    """Both copies of the state live in the directory the caller chose.

    The caller is `main`, whose directory is under `SPOOL` for the reason
    that constant gives; an archive written anywhere else is that reasoning
    silently opted out of, and on this box the default anywhere-else is a
    tmpfs holding a fraction of the state.
    """
    seen = _tools(monkeypatch)

    state_dump.dump(pg_env / 'out.age')

    assert seen[0].stdout == pg_env / 'state.dump'
    assert seen[2].stdout == pg_env / 'out.age'


def test_the_nightly_object_is_listed_before_it_is_uploaded(monkeypatch: pytest.MonkeyPatch, pg_env: Path) -> None:
    """An archive whose table of contents names no table is not a dump.

    That is what a box holds after a replacement nobody followed with a
    restore, and without this check the nightly run uploads it under a
    plausible name — to be discovered by the restore that needed it, up to a
    retention window later.
    """
    _ = _tools(monkeypatch, listing=HEADER)
    destination = pg_env / 'out.age'

    with pytest.raises(SystemExit, match='lists no tables'):
        state_dump.dump(destination)

    assert not destination.exists()


def test_a_listing_that_cannot_be_read_stops_the_run(monkeypatch: pytest.MonkeyPatch, pg_env: Path) -> None:
    # `pg_restore` refusing the archive outright is the same finding as an
    # empty listing, and must not be read as an unusual but passable answer.
    _ = _tools(monkeypatch, list_status=1, listing='')

    with pytest.raises(SystemExit, match='pg_restore --list failed'):
        state_dump.dump(pg_env / 'out.age')


def test_the_refusal_carries_what_pg_restore_said(monkeypatch: pytest.MonkeyPatch, pg_env: Path) -> None:
    """A status is not a diagnosis, and this one cannot be reproduced later.

    The listing runs with its output captured, so its stderr is the only
    account of why the archive was refused; the archive itself goes with the
    run's temporary directory, and the box it happened on is one nobody logs
    in to. Dropped here, the reason is gone.
    """
    said = 'pg_restore: error: did not find magic string in file header'
    _ = _tools(monkeypatch, list_status=1, listing='', complaint=f'{said}\n')

    with pytest.raises(SystemExit, match=said):
        state_dump.dump(pg_env / 'out.age')


def test_the_plaintext_archive_does_not_outlive_the_dump(monkeypatch: pytest.MonkeyPatch, pg_env: Path) -> None:
    # It is the whole state in the clear, and the ciphertext beside it is what
    # the upload reads; keeping both to the end of the run buys nothing.
    _ = _tools(monkeypatch)

    state_dump.dump(pg_env / 'out.age')

    assert [path.name for path in pg_env.iterdir() if path.suffix in {'.dump', '.age'}] == ['out.age']


@pytest.mark.parametrize(('pg', 'age', 'message'), [(1, 0, 'pg_dump failed'), (0, 2, 'age failed')])
def test_dump_raises_when_a_step_fails(
    monkeypatch: pytest.MonkeyPatch, pg_env: Path, pg: int, age: int, message: str
) -> None:
    _ = _tools(monkeypatch, pg=pg, age=age)

    with pytest.raises(SystemExit, match=message):
        state_dump.dump(pg_env / 'out.age')


def test_the_backend_wait_survives_a_hanging_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Postgres binds 5432 before initdb finishes, so the probe hangs.

    A `TimeoutExpired` escaping the loop ends the wait at exactly the moment
    the appliance is coming up, which is what it did the first time it ran.
    """
    import subprocess as sp

    from kluster.scripts.state_backend import provision

    calls: list[int] = []

    def probe(*_args: object, **_kwargs: object) -> sp.CompletedProcess[str]:
        calls.append(1)
        if len(calls) < 3:
            raise sp.TimeoutExpired(cmd='openssl', timeout=30)
        return sp.CompletedProcess(args=[], returncode=0, stdout='', stderr='')

    monkeypatch.setattr(provision.sp, 'run', probe)

    def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(provision.time, 'sleep', instant)

    assert provision.wait_for_backend('192.0.2.10', timeout=600) is True
    assert len(calls) == 3


# -- where a run spools, and what carries that choice -------------------------


def test_the_spool_is_the_disk_rather_than_memory() -> None:
    """`/var/tmp` is a statement about this box, not a synonym for `/tmp`.

    The appliance has 1 GB of memory and a 50 GB boot volume with no separate
    `/var`, and a run holds the whole state twice for its duration: the
    archive, and the ciphertext beside it. Spooled to `/tmp` -- a tmpfs sized
    from memory -- a state that outgrows that takes the dump down with an
    ENOSPC on a box that has tens of gigabytes free.
    """
    assert state_dump.SPOOL == '/var/tmp'


def test_the_run_takes_its_temporary_directory_from_the_spool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The constant is worth only what `main` does with it.

    `dir=` dropped from the `TemporaryDirectory` is `/tmp` again with the
    constant still reading `/var/tmp`, and nothing downstream notices: the
    dump succeeds every night the state fits.
    """
    spool = tmp_path / 'spool'
    spool.mkdir()
    monkeypatch.setattr(state_dump, 'SPOOL', str(spool))
    monkeypatch.setenv('B2_PREFIX', 'kluster/state')
    written: list[Path] = []
    sent: list[tuple[Path, str]] = []

    def dump(destination: Path) -> None:
        written.append(destination)
        _ = destination.write_bytes(b'age-encrypted bytes')

    def upload(path: Path, name: str) -> None:
        sent.append((path, name))

    monkeypatch.setattr(state_dump, 'dump', dump)
    monkeypatch.setattr(state_dump, 'upload', upload)

    assert state_dump.main() == 0

    (destination,) = written
    assert destination.parent.parent == spool
    # What is uploaded is what the dump wrote, under the prefix and a stamp.
    (uploaded, name) = sent[0]
    assert uploaded == destination
    assert name.startswith('kluster/state/') and name.endswith('.dump.age')
    # And the spool is left as it was found.
    assert list(spool.iterdir()) == []


# -- the unit that runs it ----------------------------------------------------

BUTANE = (config.DEPLOY_DIR / config.TEMPLATE).read_text()

#: The notice a failed run leaves, and a good one removes.
MOTD = '/etc/motd.d/10-state-dump.motd'


def _unit(name: str) -> str:
    """One systemd unit's own lines, out of the Butane template.

    Read as text rather than rendered: rendering wants escrow material and the
    `butane` binary, while every property below is written in the template
    itself. The slice stops at the next entry or at the comment introducing
    it, so one unit's rationale is never read as another unit's contents.

    It refuses to return a slice with no unit in it, because the assertion
    that a unit sets no `PrivateTmp=` is one an empty string also satisfies.
    """
    marker = f'    - name: {name}\n'
    assert marker in BUTANE, f'{name} is not one of the appliance units'
    lines: list[str] = []
    for line in BUTANE.split(marker, 1)[1].splitlines():
        if line.startswith('    - name:') or line.startswith('    #'):
            break
        lines.append(line)
    unit = '\n'.join(lines)
    assert '[Service]' in unit, f'{name} was sliced down to something that is not a unit'
    return unit


def test_the_dump_unit_can_time_out() -> None:
    """A `Type=oneshot` unit has no start timeout unless it asks for one.

    Every step of a dump can hang rather than fail -- a `podman exec` into a
    container that has stopped answering, an upload against a socket nothing
    closes -- and a timer does not start a service whose last run is still
    going. So an untimed hang is not one missed night; it is every night
    after it, with the unit sitting in `activating` and nothing raised.
    """
    unit = _unit('state-dump.service')

    assert 'Type=oneshot' in unit
    assert 'TimeoutStartSec=' in unit


def test_a_failed_dump_reaches_the_next_login() -> None:
    """Nothing watches the objects yet, so a failure waits for a human.

    Fedora CoreOS prints `/etc/motd.d/*` at ssh login and `state-backend ssh`
    is how an operator reaches this box, which makes a notice there the one
    channel a failure has. It is worth having only while it means "the last
    run failed", so a successful run takes it down again.
    """
    unit = _unit('state-dump.service')
    assert 'OnFailure=state-dump-failed.service' in unit
    assert 'ExecStartPost=' in unit and MOTD in unit

    notice = _unit('state-dump-failed.service')
    assert MOTD in notice
    # And it says where the rest of the story is.
    assert 'journalctl -u state-dump.service' in notice


def test_the_dump_unit_spools_to_the_hosts_var_tmp() -> None:
    """`PrivateTmp=` would settle `SPOOL`'s question from the other file.

    `disconnected` backs the service's `/var/tmp` with a fresh tmpfs, which on
    a 1 GB box is the memory that constant exists to avoid; plain `yes` keeps
    the host's disk behind it but hands the service a private `/var/tmp`, so
    the directory the script named is not the one it writes to. The spool is a
    statement about this box's disk, and neither form of the setting leaves it
    one.
    """
    assert 'PrivateTmp' not in _unit('state-dump.service')
