"""The operator's two halves of the rebuild path, without a database.

What these tests replace is the Postgres client pair and the `pulumi` CLI;
what they keep is the encryption, which runs against the real `age` binary.
So the round trip is proven by the tool that will do it live, and only the
database is imagined.

The properties held here are the ones a silent change would cost on the day
the box is gone: a dump nobody can open is never written, a file that cannot
list its tables is never called a dump, a restore refuses to land on top of
live state, and it finishes by asking `pulumi` what the backend serves rather
than by asserting that it must.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess as sp
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from memory_kit import MemoryKit

from kluster.scripts.credentials import age, escrow
from kluster.scripts.credentials.kdbx import PATH_ENV, KdbxStore
from kluster.scripts.state_backend import cli, settings, state

age_binary = shutil.which(age.BINARY)
needs_age = pytest.mark.skipif(age_binary is None, reason='age is not on PATH (mise x -- ...)')

pytestmark = needs_age

#: A stand-in for a custom-format archive: the magic real `pg_restore` looks
#: for, a payload that is neither text nor compressible, and a terminator the
#: double insists on — which is what makes truncating one detectable here in
#: the way it is detectable there.
TERMINATOR = b'-the-end-'
ARCHIVE = state.ARCHIVE_MAGIC + b'\x01\x0e\x00' + os.urandom(4096) + TERMINATOR

#: A `pg_restore --list` output, in the shape the tool prints: a comment
#: header, a table definition, its rows, and an entry that is neither.
LISTING = """;
; Archive created at 2026-08-26 02:30:00 UTC
;     dbname: pulumi_state
;
215; 1259 16385 TABLE public stacks ci
2891; 0 16385 TABLE DATA public stacks ci
216; 1259 16390 SEQUENCE public stacks_id_seq ci
"""

URL = (
    'postgres://operator@192.0.2.10:5432/pulumi_state?sslmode=verify-full'
    '&sslrootcert=/nowhere/ca.crt&sslcert=/nowhere/client.crt&sslkey=/nowhere/client.key'
)


def _completed(argv: Sequence[str], *, stdout: str = '', stderr: str = '', code: int = 0) -> sp.CompletedProcess[str]:
    return sp.CompletedProcess(args=list(argv), returncode=code, stdout=stdout, stderr=stderr)


class Double:
    """The Postgres client pair and the `pulumi` CLI, over a byte string.

    `age` is deliberately *not* doubled: it is passed through to the real
    binary, so what these tests exercise is the encryption that will run in
    production and a database that never has to exist.

    The listing refuses anything that is not the archive it dumped, which is
    what the real `pg_restore --list` does to a file whose table of contents
    is cut off — and the stack listing changes only after a restore, because
    a backend that answers the same before and after would let a restore that
    did nothing pass its own verification.
    """

    def __init__(
        self,
        *,
        archive: bytes = ARCHIVE,
        before: Sequence[str] = (),
        after: Sequence[str] = ('dns', 'physical'),
        answers_before: bool = True,
    ) -> None:
        self.archive: bytes = archive
        self.before: list[str] = list(before)
        self.after: list[str] = list(after)
        self.answers_before: bool = answers_before
        self.real: Any = sp.run
        self.calls: list[list[str]] = []
        self.restored: bytes | None = None

    def programs(self) -> list[str]:
        """Which doubled tools ran, in order. `age` is left out: it is real,
        and the escrow opens it as many times as it has generations."""
        return [Path(argv[0]).name for argv in self.calls if Path(argv[0]).name != age.BINARY]

    def argv(self, program: str) -> list[str]:
        return next(argv for argv in self.calls if Path(argv[0]).name == program)

    def _listing(self, argv: Sequence[str]) -> sp.CompletedProcess[str]:
        data = Path(argv[-1]).read_bytes()
        if not (data.startswith(state.ARCHIVE_MAGIC) and data.endswith(TERMINATOR)):
            return _completed(argv, code=1, stderr='pg_restore: error: did not find magic string in file header')
        return _completed(argv, stdout=LISTING)

    def _stacks(self, argv: Sequence[str]) -> sp.CompletedProcess[str]:
        if self.restored is None:
            if not self.answers_before:
                return _completed(argv, code=1, stderr='error: could not read stacks from the backend')
            return _completed(argv, stdout=json.dumps([{'name': name} for name in self.before]))
        return _completed(argv, stdout=json.dumps([{'name': name} for name in self.after]))

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> Any:
        self.calls.append(list(argv))
        match Path(argv[0]).name:
            case state.PG_DUMP:
                destination = next(value for value in argv if value.startswith('--file='))
                _ = Path(destination.removeprefix('--file=')).write_bytes(self.archive)
                return _completed(argv)
            case state.PG_RESTORE if '--list' in argv:
                return self._listing(argv)
            case state.PG_RESTORE:
                self.restored = Path(argv[-1]).read_bytes()
                return _completed(argv)
            case 'pulumi':
                return self._stacks(argv)
            case _:
                return self.real(argv, **kwargs)


@pytest.fixture
def double(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Double]:
    def install(**kwargs: Any) -> Double:
        tools = Double(**kwargs)
        monkeypatch.setattr(state.sp, 'run', tools)
        return tools

    return install


@pytest.fixture
def kit() -> KdbxStore:
    return MemoryKit()


@pytest.fixture
def registry(kit: KdbxStore, tmp_path: Path) -> escrow.Registry:
    """An escrow holding the backup identities and nothing else.

    The labels are read from `escrow.backup_labels()` rather than listed, so
    a test that moves the generation window gets the registry that window
    describes.
    """
    registry = escrow.Registry.open(tmp_path / 'escrow')
    _ = escrow.init(kit, registry)
    for label in escrow.backup_labels():
        _ = escrow.generate(registry, label)
    return registry


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    directory = tmp_path / 'bundle'
    directory.mkdir()
    _ = (directory / 'backend-url').write_text(f'{URL}\n')
    return directory


def _identities(kit: KdbxStore, registry: escrow.Registry) -> list[str]:
    vault = escrow.Vault.open(kit, registry)
    return [vault.recover(label) for label in escrow.backup_labels()]


def _dump(kit: KdbxStore, registry: escrow.Registry, bundle: Path, output: Path | None = None) -> int:
    return cli._dump(kit, registry=registry, bundle_dir=bundle, output=output)  # pyright: ignore[reportPrivateUsage]


def _restore(
    kit: KdbxStore | None,
    registry: escrow.Registry,
    bundle: Path,
    source: Path,
    *,
    identity: Path | None = None,
    force: bool = False,
) -> int:
    return cli._restore(  # pyright: ignore[reportPrivateUsage]
        kit,
        registry=registry,
        bundle_dir=bundle,
        source=source,
        identity=identity,
        force=force,
    )


def test_a_dump_is_the_archive_encrypted_to_the_escrow(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    """The one property the whole command exists for: it opens again.

    Encrypted to the identities the escrow holds — the same ones the box is
    built with — so a dump an operator takes by hand is recoverable by
    whoever holds the kit, exactly like a nightly one.
    """
    tools = double()
    output = tmp_path / 'taken.dump.age'

    assert _dump(kit, registry, bundle, output) == 0

    assert output.read_bytes().startswith(state.AGE_MAGIC)
    opened = tmp_path / 'opened.dump'
    state.decrypt(output, opened, _identities(kit, registry))
    assert opened.read_bytes() == ARCHIVE
    # The dump is taken over the bundle's connection string, which is the
    # same one `mise.toml` turns into PULUMI_BACKEND_URL.
    assert f'--dbname={URL}' in tools.argv(state.PG_DUMP)


def test_the_plaintext_archive_does_not_survive_the_command(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    # The archive is the whole state in the clear. It exists for the two
    # steps that read it and nowhere else, least of all beside the file the
    # operator is about to carry somewhere.
    _ = double()
    output = tmp_path / 'dumps' / 'taken.dump.age'
    output.parent.mkdir()

    assert _dump(kit, registry, bundle, output) == 0

    assert [path.name for path in output.parent.iterdir()] == [output.name]


def test_a_dump_that_cannot_list_its_tables_is_not_written(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    """A truncated archive has a plausible size and a plausible name.

    What it does not have is a readable table of contents, so the command
    that would otherwise hand the operator a file to trust fails instead —
    and leaves nothing behind that could later be mistaken for a dump.
    """
    _ = double(archive=ARCHIVE[: len(ARCHIVE) // 2])
    output = tmp_path / 'taken.dump.age'

    with pytest.raises(state.StateError, match='did not find magic string'):
        _ = _dump(kit, registry, bundle, output)

    assert not output.exists()


def test_an_archive_of_nothing_is_not_a_dump(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    # The other half of the same check: a file `pg_restore` can read but that
    # carries no table is a dump of a database that lost its state.
    tools = double()
    header = ';\n; Archive created at 2026-08-26 02:30:00 UTC\n;\n'
    monkeypatched = tools._listing  # pyright: ignore[reportPrivateUsage]

    def empty(argv: Sequence[str]) -> sp.CompletedProcess[str]:
        answer = monkeypatched(argv)
        return _completed(argv, stdout=header) if answer.returncode == 0 else answer

    tools._listing = empty  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(state.StateError, match='lists no tables'):
        _ = _dump(kit, registry, bundle, tmp_path / 'taken.dump.age')


def test_a_dump_never_overwrites_one(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    _ = double()
    output = tmp_path / 'taken.dump.age'
    _ = output.write_bytes(b'an earlier dump')

    with pytest.raises(state.StateError, match='already exists'):
        _ = _dump(kit, registry, bundle, output)

    assert output.read_bytes() == b'an earlier dump'


def test_a_dump_opens_with_either_live_generation(
    double: Callable[..., Double], monkeypatch: pytest.MonkeyPatch, kit: KdbxStore, tmp_path: Path, bundle: Path
) -> None:
    """The generational pair, which is what makes retention work (§5).

    Every object still in retention has to open with the current key or the
    previous one, because nothing about an object says which it was written
    under.
    """
    monkeypatch.setattr(settings, 'AGE_GENERATION', 2)
    registry = escrow.Registry.open(tmp_path / 'escrow')
    _ = escrow.init(kit, registry)
    labels = escrow.backup_labels()
    assert len(labels) == 2
    for label in labels:
        _ = escrow.generate(registry, label)
    _ = double()
    output = tmp_path / 'taken.dump.age'

    assert _dump(kit, registry, bundle, output) == 0

    vault = escrow.Vault.open(kit, registry)
    for label in labels:
        opened = tmp_path / f'{label.replace("/", "-")}.dump'
        state.decrypt(output, opened, [vault.recover(label)])
        assert opened.read_bytes() == ARCHIVE


def test_a_restore_decrypts_verifies_and_lands_the_archive(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    tools = double()
    dump = tmp_path / 'taken.dump.age'
    assert _dump(kit, registry, bundle, dump) == 0
    tools.calls.clear()

    assert _restore(kit, registry, bundle, dump) == 0

    assert tools.restored == ARCHIVE
    # Listed before it is restored, and the backend is asked what it serves
    # both before and after: the verification is the last word, not the log
    # line above it.
    assert tools.programs() == ['pulumi', state.PG_RESTORE, state.PG_RESTORE, 'pulumi']


def test_a_restore_refuses_a_backend_that_already_serves_stacks(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    """Restoring over live state is how the state is lost, not recovered.

    The rebuild playbooks all restore into a box that has none, so a
    populated backend means the operator is pointing at the wrong one.
    """
    tools = double(before=('dns', 'physical'))
    dump = tmp_path / 'taken.dump.age'
    assert _dump(kit, registry, bundle, dump) == 0
    tools.calls.clear()

    assert _restore(kit, registry, bundle, dump) == 1

    assert tools.restored is None
    assert tools.programs() == ['pulumi']


def test_force_restores_over_them(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    tools = double(before=('dns', 'physical'))
    dump = tmp_path / 'taken.dump.age'
    assert _dump(kit, registry, bundle, dump) == 0

    assert _restore(kit, registry, bundle, dump, force=True) == 0

    assert tools.restored == ARCHIVE


def test_a_backend_that_cannot_answer_yet_is_not_a_reason_to_refuse(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    """The ordinary case: a box provisioned minutes ago, restored into.

    Its database has never had a stack written to it, so the question the
    guard asks has no answer — which must not be read as "there is state
    here", or no rebuild could ever finish.
    """
    tools = double(answers_before=False)
    dump = tmp_path / 'taken.dump.age'
    assert _dump(kit, registry, bundle, dump) == 0

    assert _restore(kit, registry, bundle, dump) == 0

    assert tools.restored == ARCHIVE


def test_a_restore_that_lands_nothing_reports_failure(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    # `pg_restore` succeeded and the backend still serves nothing: the
    # command's own verification is what turns that into a failure.
    _ = double(after=())
    dump = tmp_path / 'taken.dump.age'
    assert _dump(kit, registry, bundle, dump) == 0

    assert _restore(kit, registry, bundle, dump) == 1


def test_a_plain_archive_needs_neither_kit_nor_key(
    double: Callable[..., Double], registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    # What a `pg_dump` taken by hand, or a dump already decrypted elsewhere,
    # looks like. Nothing about it needs opening, so nothing opens the kit.
    tools = double()
    dump = tmp_path / 'state.dump'
    _ = dump.write_bytes(ARCHIVE)

    assert _restore(None, registry, bundle, dump) == 0

    assert tools.restored == ARCHIVE


def test_a_file_that_is_neither_is_refused(
    double: Callable[..., Double], registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    # A plain-SQL dump, a tarball, half a download: named like a dump and
    # readable by nothing on this path.
    _ = double()
    dump = tmp_path / 'state.sql'
    _ = dump.write_text('-- PostgreSQL database dump\n')

    with pytest.raises(state.StateError, match='neither an age file nor'):
        _ = _restore(None, registry, bundle, dump)


def test_a_dump_that_is_not_there_says_so(
    double: Callable[..., Double], registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    # A mistyped path is the most ordinary way to reach this command, and it
    # should read as one rather than as a traceback.
    _ = double()

    with pytest.raises(state.StateError, match='no dump at'):
        _ = _restore(None, registry, bundle, tmp_path / 'never-written.dump.age')


def test_a_truncated_ciphertext_never_becomes_an_archive(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    """A download that stopped early still starts with the age header.

    Nothing else about it is right, and `age` is what notices: the format is
    authenticated, so a restore never gets the chance to feed a half-file to
    a database.
    """
    tools = double()
    dump = tmp_path / 'taken.dump.age'
    assert _dump(kit, registry, bundle, dump) == 0
    _ = dump.write_bytes(dump.read_bytes()[:-64])

    with pytest.raises(state.StateError, match=age.BINARY):
        _ = _restore(kit, registry, bundle, dump)

    assert tools.restored is None


def test_an_armoured_dump_is_recognised_as_one(tmp_path: Path) -> None:
    # The escrow's own ciphertexts are armoured, and a dump someone opened
    # and re-encrypted by hand may be too. Both are age files.
    path = tmp_path / 'armoured.age'
    _ = path.write_text(age.encrypt('not really an archive', [age.generate().public]))

    assert state.encrypted(path)


def test_the_drill_key_restores_without_a_kit(
    double: Callable[..., Double],
    monkeypatch: pytest.MonkeyPatch,
    kit: KdbxStore,
    registry: escrow.Registry,
    bundle: Path,
    tmp_path: Path,
) -> None:
    """The unattended drill (§7.3): one age key in a file, no offline database.

    Driven through `main`, because the property is about the command line:
    naming an identity has to be enough to keep the run from asking for a
    kit that the machine running the drill does not have.
    """
    tools = double()
    dump = tmp_path / 'taken.dump.age'
    assert _dump(kit, registry, bundle, dump) == 0
    key = tmp_path / 'drill.key'
    _ = key.write_text(f'# created by age-keygen\n{_identities(kit, registry)[0]}\n')
    monkeypatch.setenv(PATH_ENV, str(tmp_path / 'there-is-no-kit-here.kdbx'))

    code = cli.main(['restore', str(dump), '--identity-file', str(key), '--bundle', str(bundle)])

    assert code == 0
    assert tools.restored == ARCHIVE


def test_the_wrong_identity_is_a_refusal_rather_than_a_restore(
    double: Callable[..., Double], kit: KdbxStore, registry: escrow.Registry, bundle: Path, tmp_path: Path
) -> None:
    tools = double()
    dump = tmp_path / 'taken.dump.age'
    assert _dump(kit, registry, bundle, dump) == 0
    key = tmp_path / 'stranger.key'
    _ = key.write_text(f'{age.generate().secret}\n')

    with pytest.raises(state.StateError, match=age.BINARY):
        _ = _restore(None, registry, bundle, dump, identity=key)

    assert tools.restored is None


#: What has to be said *before* each step that can take minutes or write to a
#: database, and the word that has to appear in what the run said by then. A
#: log that only speaks on success is indistinguishable from a hang.
ANNOUNCED = {
    state.PG_DUMP: 'dumping',
    state.PG_RESTORE: 'archive',
    'age --encrypt': 'encrypting',
    'age --decrypt': 'decrypting',
    'pulumi': 'stacks',
}


def _step(argv: Sequence[str]) -> str:
    """Which of the steps above this invocation is.

    The escrow opens its own ciphertexts with the same binary, so `age` alone
    does not identify a step: the file-to-file form is the one these commands
    make, and it is the one that names an output.
    """
    program = Path(argv[0]).name
    if program == age.BINARY and '--output' in argv:
        return f'{age.BINARY} {"--encrypt" if "--encrypt" in argv else "--decrypt"}'
    return program


def test_every_slow_step_announces_itself_before_it_starts(
    double: Callable[..., Double],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    kit: KdbxStore,
    registry: escrow.Registry,
    bundle: Path,
    tmp_path: Path,
) -> None:
    caplog.set_level(logging.INFO)
    tools = double()
    said: dict[str, list[str]] = {}

    def watch(argv: Sequence[str], **kwargs: Any) -> Any:
        said.setdefault(_step(argv), list(caplog.messages))
        return tools(argv, **kwargs)

    monkeypatch.setattr(state.sp, 'run', watch)
    dump = tmp_path / 'taken.dump.age'

    assert _dump(kit, registry, bundle, dump) == 0
    assert _restore(kit, registry, bundle, dump, force=True) == 0

    for program, word in ANNOUNCED.items():
        assert any(word in message for message in said[program]), f'{program} ran without announcing itself'


def test_the_listing_is_read_for_tables_rather_than_for_entries() -> None:
    # Both forms count -- the definition and the rows -- and nothing else
    # does, so an archive of sequences and functions is not a dump of state.
    assert state.tables(LISTING) == ['public.stacks']
    assert state.tables(';\n; Archive created at 2026-08-26 02:30:00 UTC\n;\n') == []


def test_the_default_name_is_the_one_the_appliance_uses() -> None:
    import datetime as dt

    name = state.dump_name(dt.datetime(2026, 8, 26, 2, 30, tzinfo=dt.timezone.utc))

    assert name == f'{settings.NAME}-20260826T023000Z.dump.age'


def test_both_commands_are_on_the_command_line() -> None:
    # Cheap, and it is what fails first when a parser and a handler disagree
    # about what an argument is called.
    parsed = cli._parser().parse_args(['dump'])  # pyright: ignore[reportPrivateUsage]
    assert (parsed.action, parsed.output) == ('dump', None)

    parsed = cli._parser().parse_args(['restore', 'a.dump.age', '--force'])  # pyright: ignore[reportPrivateUsage]
    assert (parsed.action, parsed.dump, parsed.identity_file, parsed.force) == (
        'restore',
        Path('a.dump.age'),
        None,
        True,
    )


def test_a_bundle_that_is_not_there_names_the_command_that_writes_one(tmp_path: Path) -> None:
    with pytest.raises(state.StateError, match='state-backend bundle operator'):
        _ = state.backend_url(tmp_path / 'nothing-here')
