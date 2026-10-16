"""The `state-backend` console script: what is left of it once the stack declares the appliance.

The pins check and the pinned SSH login, which make no OCI call; the
readiness wait the playbooks lean on; the dump that needs no kit; and
`main`, which turns every refusal the program can raise into one line.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import importlib.util
import json
import logging
import re
import types
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from email.message import Message
from pathlib import Path
from typing import Any, cast

import pytest

from kluster.lib.state_backend import committed, readiness, settings
from kluster.scripts.state_backend import cli, provision

#: A host key's public half, as the committed file holds it.
PIN = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExamplefbb1a7'

# -- the pins ------------------------------------------------------------------


class _Answer:
    """One `urlopen` answer: a JSON body, or headers and no body."""

    def __init__(self, *, body: object = None, headers: dict[str, str] | None = None) -> None:
        self.headers: dict[str, str] = headers or {}
        self._body: bytes = json.dumps(body).encode() if body is not None else b''

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _registry(monkeypatch: pytest.MonkeyPatch, *answers: _Answer) -> None:
    """The two calls `_image_digest` makes, in order: the token, then the manifest."""
    remaining = list(answers)

    def urlopen(*_args: object, **_kwargs: object) -> _Answer:
        return remaining.pop(0)

    monkeypatch.setattr(provision.urllib.request, 'urlopen', urlopen)


def test_a_manifest_without_a_digest_header_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """`state-backend pins` exists to catch a bad pin; an empty answer must not pass as one.

    A registry that answers the HEAD without `Docker-Content-Digest` used to
    be logged as a resolution to the empty string, so the one command whose
    job is to fail on a bad pin reported success.
    """
    _registry(monkeypatch, _Answer(body={'token': 'a-pull-token'}), _Answer(headers={}))

    with pytest.raises(RuntimeError, match='without a Docker-Content-Digest header'):
        _ = provision._image_digest('docker.io/library/postgres', '17')  # pyright: ignore[reportPrivateUsage]


def test_a_token_response_without_a_token_names_the_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, _Answer(body={'errors': ['nope']}))

    with pytest.raises(RuntimeError, match='the registry pull token for library/postgres has no token'):
        _ = provision._image_digest('docker.io/library/postgres', '17')  # pyright: ignore[reportPrivateUsage]


def test_a_resolved_manifest_answers_its_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(
        monkeypatch,
        _Answer(body={'token': 'a-pull-token'}),
        _Answer(headers={'Docker-Content-Digest': 'sha256:abc'}),
    )

    assert provision._image_digest('docker.io/library/postgres', '17') == 'sha256:abc'  # pyright: ignore[reportPrivateUsage]


def _resolving(monkeypatch: pytest.MonkeyPatch, answers: dict[str, str | urllib.error.HTTPError]) -> list[str]:
    """`_image_digest` answering each reference from `answers`; returns the references asked, in order."""
    asked: list[str] = []

    def image_digest(repository: str, reference: str) -> str:
        assert repository == settings.POSTGRES_REPOSITORY
        asked.append(reference)
        answer = answers[reference]
        if isinstance(answer, urllib.error.HTTPError):
            raise answer
        return answer

    monkeypatch.setattr(provision, '_image_digest', image_digest)
    return asked


def test_a_pinned_digest_the_registry_serves_passes_though_the_tag_has_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _resolving(
        monkeypatch, {settings.POSTGRES_DIGEST: settings.POSTGRES_DIGEST, settings.POSTGRES_TAG: 'sha256:' + 'e' * 64}
    )

    assert provision._postgres_pin_ok()  # pyright: ignore[reportPrivateUsage]
    assert set(asked) == {settings.POSTGRES_DIGEST, settings.POSTGRES_TAG}


def test_a_pinned_digest_the_registry_does_not_serve_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = urllib.error.HTTPError('https://registry-1.docker.io', 404, 'manifest unknown', Message(), None)
    _ = _resolving(monkeypatch, {settings.POSTGRES_DIGEST: missing, settings.POSTGRES_TAG: settings.POSTGRES_DIGEST})

    assert not provision._postgres_pin_ok()  # pyright: ignore[reportPrivateUsage]


def test_a_pin_the_registry_serves_as_another_digest_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    other = 'sha256:' + 'f' * 64
    _ = _resolving(monkeypatch, {settings.POSTGRES_DIGEST: other, settings.POSTGRES_TAG: other})

    assert not provision._postgres_pin_ok()  # pyright: ignore[reportPrivateUsage]


def test_the_digest_a_reference_is_asked_by_is_the_manifest_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []
    answers = [_Answer(body={'token': 'a-pull-token'}), _Answer(headers={'Docker-Content-Digest': 'sha256:abc'})]

    def urlopen(request: object, *_args: object, **_kwargs: object) -> _Answer:
        requested.append(request if isinstance(request, str) else cast('urllib.request.Request', request).full_url)
        return answers.pop(0)

    monkeypatch.setattr(provision.urllib.request, 'urlopen', urlopen)
    _ = provision._image_digest('docker.io/library/postgres', 'sha256:abc')  # pyright: ignore[reportPrivateUsage]

    assert requested[-1] == 'https://registry-1.docker.io/v2/library/postgres/manifests/sha256:abc'


# -- the readiness wait --------------------------------------------------------


def test_the_readiness_probe_closes_its_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the wait can never finish on a workstation.

    `openssl s_client` keeps the connection open reading stdin once the
    handshake is done, so with a terminal inherited from the operator's shell
    a *successful* probe hangs until the timeout and is reported as no answer.
    On a machine that came up in 90 seconds this looked like packets being
    dropped for as long as the operator was willing to wait.

    The clock is the test's, advanced only by the wait's own `sleep`, so the
    one-second budget bounds a fake that never answers to a single probe and
    is not a second of wall time: a process stalled between computing the
    deadline and checking it -- swap, a contended machine -- cannot decide a
    case whose subject is the probe's stdin.
    """
    seen: dict[str, object] = {}
    clock = [0.0]

    def fake_run(argv: list[str], **kwargs: object) -> Any:
        seen.update(kwargs)
        seen['argv'] = argv
        return type('Completed', (), {'returncode': 0, 'stderr': ''})()

    def nap(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(readiness.sp, 'run', fake_run)
    monkeypatch.setattr(readiness.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(readiness.time, 'sleep', nap)

    assert readiness.wait_for_backend('192.0.2.10', timeout=1) is True
    assert seen['stdin'] is readiness.sp.DEVNULL


def test_the_readiness_wait_states_its_condition_before_probing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The longest silence in a provision run is the one after the launch.

    The announcement names the address it waits on, the cadence the wait
    then keeps and the ceiling, which is the wait's `timeout`. The clock the
    wait reads against is the test's, advanced only by the wait's own
    `sleep`, and the fake answers the second probe, so the cadence announced
    is held to the one interval the wait sleeps.
    """
    caplog.set_level(logging.INFO)
    said: list[str] = []
    probes: list[int] = []
    naps: list[float] = []
    clock = [0.0]

    def fake_run(_argv: list[str], **_kwargs: object) -> Any:
        probes.append(1)
        said.extend(caplog.messages)
        return type('Completed', (), {'returncode': 0 if len(probes) > 1 else 1, 'stderr': ''})()

    def nap(seconds: float) -> None:
        naps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(readiness.sp, 'run', fake_run)
    monkeypatch.setattr(readiness.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(readiness.time, 'sleep', nap)

    assert readiness.wait_for_backend('192.0.2.10', timeout=900) is True, f'gave up after {len(probes)} probes'
    [interval] = naps
    # Said before the first probe, so it is among what the first probe saw.
    announcement = next(message for message in said if '192.0.2.10' in message)
    assert readiness.duration(interval) in announcement
    assert readiness.duration(900) in announcement


def test_the_readiness_wait_that_gives_up_says_what_it_last_saw_and_where_to_look(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The wait's failure is the run's last word, and it has to be a lead.

    A port that never answers has two very different causes -- a box still
    starting or broken, and a route dropping the packets -- and the wait
    cannot tell them apart from here. So it reports the last thing the probe
    said, verbatim, and the one command that separates the two from another
    host. A `False` with neither is an operator left to guess.

    The clock is the test's, advanced only by the wait's own `sleep`: a
    one-minute budget here is four refusals of a fake, not a minute of wall
    time.
    """
    caplog.set_level(logging.ERROR)
    clock = [0.0]
    probe: list[str] = []

    def fake_run(argv: list[str], **_kwargs: object) -> Any:
        probe[:] = argv
        return type('Completed', (), {'returncode': 1, 'stderr': '\nconnect: Connection refused\n'})()

    def nap(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(readiness.sp, 'run', fake_run)
    monkeypatch.setattr(readiness.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(readiness.time, 'sleep', nap)

    assert readiness.wait_for_backend('192.0.2.10', timeout=60) is False

    # The probe's own last word, which is the fake's first line of stderr.
    assert any('connect: Connection refused' in message for message in caplog.messages)
    # The command the operator runs is the probe the wait ran, spelled for a
    # shell with the stdin the probe closes -- so that the answer from another
    # host is comparable to the wait's own.
    separating = next(message for message in caplog.messages if ' '.join(probe) in message)
    assert f'{" ".join(probe)} </dev/null' in separating
    # And the other way in, which has to be a command this program carries.
    named = re.findall(r'`state-backend ([^`]+)`', separating)
    assert named
    for words in named:
        _ = cli.build_parser().parse_args(words.split())


# -- ssh, held to the committed host key ------------------------------------------


class _Execed(Exception):
    """What stands in for `os.execvp` never returning."""


@pytest.fixture
def execed(monkeypatch: pytest.MonkeyPatch, credential_files: Path) -> list[list[str]]:
    """`ssh` argv instead of an `ssh` process, with the running checkout's slots the case's own."""
    captured: list[list[str]] = []

    def execvp(file: str, args: Sequence[str]) -> None:
        assert file == 'ssh'
        captured.append(list(args))
        raise _Execed

    monkeypatch.setattr(provision.os, 'execvp', execvp)
    return captured


@pytest.fixture
def host_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The committed host key's public half, pointed away from the checkout's own."""
    path = tmp_path / 'host-key.txt'
    _ = path.write_text(f"# the box's SSH host key, public half\n{PIN}\n")
    monkeypatch.setattr(committed, 'HOST_KEY', path)
    return path


def test_ssh_holds_the_box_to_the_committed_host_key(
    execed: list[list[str]], host_key: Path, credential_files: Path
) -> None:
    """First contact is not trust-on-first-use: a wrong answer is refused.

    Every option here is load-bearing. Without the strict setting the client
    accepts an unknown key and writes it down; without a file of its own it
    would consult whatever this machine already trusts; without the algorithm
    the box's other host key types are answers the pin does not cover; and
    without `-F /dev/null` the client still reads a configuration whose
    `ControlMaster` or `KnownHostsCommand` decides the question instead
    (`test_ssh_pin.py` holds that one against a live client). The pin is the
    committed public half, at the address the repository records: no OCI call
    stands between the operator and the box.
    """
    with pytest.raises(_Execed):
        provision.ssh(['journalctl', '-u', 'postgres'])

    argv = execed[0]
    assert argv[argv.index('-F') + 1] == '/dev/null'
    options = [argv[index + 1] for index, token in enumerate(argv) if token == '-o']
    assert 'StrictHostKeyChecking=yes' in options
    assert 'GlobalKnownHostsFile=/dev/null' in options
    assert 'HostKeyAlgorithms=ssh-ed25519' in options
    assert argv[-4:] == [f'core@{settings.ADDRESS}', 'journalctl', '-u', 'postgres']

    named = [option.removeprefix('UserKnownHostsFile=') for option in options if 'UserKnownHostsFile=' in option]
    assert len(named) == 1
    known_hosts = Path(named[0])
    # The tool's own file, in the checkout's layer -- never the operator's,
    # which the tool neither reads nor writes.
    assert known_hosts.is_relative_to(credential_files)
    assert not known_hosts.is_relative_to(Path.home() / '.ssh')
    assert known_hosts.read_text() == f'{settings.ADDRESS} {PIN}\n'


def test_the_refusal_is_framed_before_the_connection_is_made(
    execed: list[list[str]], host_key: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`os.execvp` replaces the process, so afterwards there is nobody to explain.

    And the explanation has two halves that lead to different actions: the box
    answering is one the stack has not replaced since the key was generated,
    or something is interposed on the path.
    """
    caplog.set_level(logging.INFO)

    with pytest.raises(_Execed):
        provision.ssh([])

    framing = [message for message in caplog.messages if PIN in message]
    assert len(framing) == 1


def test_ssh_with_no_committed_host_key_is_refused_before_it_pins_anything(
    execed: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    credential_files: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silence is the state the pin exists to rule out: one line through `main`, not a traceback.

    That the refusal names the command writing the file is held by
    `test_derived`'s `test_check_refuses_while_no_host_key_file_exists`,
    against the writer's own spelling of the row.
    """
    monkeypatch.setattr(committed, 'HOST_KEY', tmp_path / 'host-key.txt')
    caplog.set_level(logging.ERROR)

    assert cli.main(['ssh']) == 1

    assert execed == []
    assert not credential_files.exists(), 'the refused run wrote into the layer'
    [record] = caplog.records
    assert record.exc_info is None


# -- main ---------------------------------------------------------------------------


#: Where the walk below stops: a module outside this package is somebody
#: else's program, and what it raises is not a refusal of this one.
_OURS = 'kluster'


def _imports(tree: ast.Module, package: str) -> Iterator[str]:
    """Every module under `_OURS` a file names in an import, at any depth.

    A `from a.b import c` names `a.b` and may name the module `a.b.c`; which
    of those exist is settled by importing them, and a name that is not a
    module is an attribute the walk has no use for.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                base = importlib.util.resolve_name('.' * node.level + base, package)
            names = [base, *(f'{base}.{alias.name}' for alias in node.names)]
        else:
            continue
        for name in names:
            if name == _OURS or name.startswith(f'{_OURS}.'):
                yield name


def _closure(start: str) -> dict[str, ast.Module]:
    """Every module `start` reaches through imports, transitively, with its syntax tree."""
    found: dict[str, ast.Module] = {}
    pending = [start]
    while pending:
        name = pending.pop()
        if name in found:
            continue
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            # An imported name that is not a module: an attribute, which the
            # walk has no use for. Any other failure is a real one.
            if exc.name != name:
                raise
            continue
        if module.__file__ is None:
            continue
        tree = ast.parse(Path(module.__file__).read_text())
        found[name] = tree
        pending.extend(_imports(tree, module.__package__ or name))
    return found


def _named(node: ast.expr, module: types.ModuleType) -> object:
    """What a dotted name in `module` refers to at module scope, or None."""
    if isinstance(node, ast.Name):
        return getattr(module, node.id, getattr(builtins, node.id, None))
    if isinstance(node, ast.Attribute):
        owner = _named(node.value, module)
        return None if owner is None else getattr(owner, node.attr, None)
    return None


def _raised(closure: dict[str, ast.Module]) -> dict[type[BaseException], set[str]]:
    """Each exception class this repository defines that the closure raises, and where."""
    raised: dict[type[BaseException], set[str]] = {}
    for name, tree in closure.items():
        module = importlib.import_module(name)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            target = _named(node.exc.func, module)
            assert isinstance(target, type) and issubclass(target, BaseException), (
                f'{name}:{node.lineno} raises {ast.unparse(node.exc.func)}, which is not a class at module scope'
            )
            if target.__module__.startswith(f'{_OURS}.'):
                raised.setdefault(target, set()).add(name)
    return raised


def test_main_turns_every_refusal_the_program_can_raise_into_one_line() -> None:
    """`cli.REFUSALS` is a census of the import closure, held in both directions.

    Forward: every exception class this repository defines that some module
    `cli` reaches can raise is caught, so no refusal surfaces as a traceback.
    Backward: every member of the tuple is raised somewhere in that closure,
    so no member is a name nothing raises. The closure is the import graph
    rather than a call graph, which is what lets the test be written; the
    members that over-approximation adds are named at the tuple.
    """
    raised = _raised(_closure(cli.__name__))
    assert raised, 'the walk found no raise of a repository exception, so it walked nothing'

    uncaught = {cls.__name__: sorted(where) for cls, where in raised.items() if not issubclass(cls, cli.REFUSALS)}
    assert not uncaught, f'raised on a path main reaches and not in cli.REFUSALS: {uncaught}'

    unraised = [member.__name__ for member in cli.REFUSALS if not any(issubclass(cls, member) for cls in raised)]
    assert not unraised, f'in cli.REFUSALS and raised nowhere main reaches: {unraised}'
