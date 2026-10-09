"""The `state-backend` console script: what is left of it once the stack declares the appliance.

The pins check and the pinned SSH login, which make no OCI call; the
readiness wait the playbooks lean on; `adopt`, the cutover's one write into
the stack's configuration, driven against fakes of the OCI SDK's listings and
of B2's bucket listing; the dump that needs no kit; and `main`, which turns
every refusal the program can raise into one line.
"""

# The OCI SDK ships no stubs; the same waiver `adopt.py` itself carries. The
# fakes below answer its listings with its own `Response`.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import ast
import builtins
import importlib
import importlib.util
import json
import logging
import types
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from email.message import Message
from pathlib import Path
from typing import Any, cast

import oci
import pytest
from fake_pulumi import RecordedPulumi

from kluster.lib import stack_environment
from kluster.lib.state_backend import adoption, committed, readiness, settings
from kluster.scripts.credentials import b2, derived, pulumi_config, workstation
from kluster.scripts.state_backend import adopt, cli, config, provision

#: A host key's public half, as the committed file holds it.
PIN = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExamplefbb1a7'

#: An address that is not the one the repository records.
ELSEWHERE = '192.0.2.99'


def _returning(value: Any) -> Callable[..., Any]:
    """A typed stand-in: a bare lambda leaves its parameters unannotated."""

    def stub(*_args: object, **_kwargs: object) -> Any:
        return value

    return stub


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

    The ceiling in the announcement is the wait's `timeout`, and the clock
    the wait reads it against is the test's, advanced only by the wait's own
    `sleep`: fifteen minutes here is sixty probes of a fake that never
    answers, not a second of wall time.
    """
    caplog.set_level(logging.INFO)
    said: list[str] = []
    probes: list[int] = []
    clock = [0.0]

    def fake_run(_argv: list[str], **_kwargs: object) -> Any:
        probes.append(1)
        said.extend(caplog.messages)
        return type('Completed', (), {'returncode': 0, 'stderr': ''})()

    def nap(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(readiness.sp, 'run', fake_run)
    monkeypatch.setattr(readiness.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(readiness.time, 'sleep', nap)

    assert readiness.wait_for_backend('192.0.2.10', timeout=900) is True, f'gave up after {len(probes)} probes'
    announcement = next(message for message in said if 'waiting' in message)
    # The condition, the retry cadence, why it is slow, and the ceiling.
    assert '192.0.2.10' in announcement
    assert 'every 15s' in announcement
    assert 'minutes' in announcement
    assert '15m00s' in announcement


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

    assert 'last attempt said: connect: Connection refused' in caplog.messages
    separating = next(message for message in caplog.messages if 'broken box from a broken route' in message)
    # The command the operator runs is the probe the wait ran, spelled for a
    # shell with the stdin the probe closes -- so that the answer from another
    # host is comparable to the wait's own.
    assert f'{" ".join(probe)} </dev/null' in separating
    assert 'state-backend ssh' in separating


# -- ssh, held to the committed host key ------------------------------------------


class _Execed(Exception):
    """What stands in for `os.execvp` never returning."""


@pytest.fixture
def execed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    """`ssh` argv instead of an `ssh` process, with the slot under `tmp_path`."""
    captured: list[list[str]] = []

    def execvp(file: str, args: Sequence[str]) -> None:
        assert file == 'ssh'
        captured.append(list(args))
        raise _Execed

    monkeypatch.setattr(provision.os, 'execvp', execvp)
    monkeypatch.setattr(workstation, 'directory', lambda: tmp_path / '.credentials')
    return captured


@pytest.fixture
def host_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The committed host key's public half, pointed away from the checkout's own."""
    path = tmp_path / 'host-key.txt'
    _ = path.write_text(f"# the box's SSH host key, public half\n{PIN}\n")
    monkeypatch.setattr(committed, 'HOST_KEY', path)
    return path


def test_ssh_holds_the_box_to_the_committed_host_key(execed: list[list[str]], host_key: Path, tmp_path: Path) -> None:
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
    # The tool's own file, in the slot that holds this box's client bundle --
    # never the operator's, which the tool neither reads nor writes.
    assert known_hosts == tmp_path / '.credentials' / workstation.BUNDLE / config.KNOWN_HOSTS_FILE
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

    framing = [message for message in caplog.messages if 'interposed' in message]
    assert len(framing) == 1
    assert 'replaced' in framing[0]
    assert PIN in framing[0]


def test_ssh_with_no_committed_host_key_is_refused_before_it_pins_anything(
    execed: list[list[str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
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
    assert not (tmp_path / '.credentials' / workstation.BUNDLE / config.KNOWN_HOSTS_FILE).exists()
    [record] = caplog.records
    assert record.exc_info is None


# -- adopt ------------------------------------------------------------------------


COMPARTMENT = 'ocid1.compartment.oc1..appliance'
NAMESPACE = 'the-namespace'


def _resource(kind: str, name: str, *, state: str = 'AVAILABLE', **fields: object) -> Any:
    """One listed resource: what `adopt` reads off a listing, and nothing more."""
    return types.SimpleNamespace(
        id=f'ocid1.{kind}.oc1..{name}-{state.lower()}', display_name=name, lifecycle_state=state, **fields
    )


class _Listing:
    """A list call of the SDK's shape, answering one page per call and the cursor to the next."""

    def __init__(self, *pages: list[Any]) -> None:
        self.pages: list[list[Any]] = list(pages)
        self.asked: list[dict[str, object]] = []
        # The pagination helper names the call it follows in its log line.
        self.__name__ = 'list_call'

    def __call__(self, *args: object, page: str | None = None, **kwargs: object) -> oci.response.Response:
        self.asked.append({'args': args, **kwargs})
        index = int(page or 0)
        headers = {'opc-next-page': str(index + 1)} if index + 1 < len(self.pages) else {}
        return oci.response.Response(200, headers, self.pages[index], None)


@pytest.fixture
def existing() -> adopt.Clients:
    """What the appliance's script built, under the names the stack declares, beside what it terminated."""
    vcn = _resource('vcn', f'{settings.NAME}-vcn')
    network = types.SimpleNamespace(
        # A listing keeps what was terminated: a page of them ahead of the
        # one that serves.
        list_vcns=_Listing([_resource('vcn', f'{settings.NAME}-vcn', state='TERMINATED') for _ in range(2)], [vcn]),
        list_internet_gateways=_Listing([_resource('internetgateway', f'{settings.NAME}-igw')]),
        list_subnets=_Listing([_resource('subnet', f'{settings.NAME}-subnet')]),
        list_public_ips=_Listing(
            [_resource('publicip', f'{settings.NAME}-ip', ip_address=settings.ADDRESS)],
        ),
    )
    buckets: list[str] = []

    def get_bucket(namespace: str, name: str) -> object:
        buckets.append(f'{namespace}/{name}')
        return object()

    storage = types.SimpleNamespace(
        get_namespace=_returning(types.SimpleNamespace(data=NAMESPACE)),
        get_bucket=get_bucket,
    )
    return adopt.Clients(
        network=network,
        object_storage=storage,
        buckets=lambda name: [b2.Bucket(bucket_id='bucket-dumps')] if name == settings.B2_BUCKET else [],
    )


def test_adopt_finds_every_resource_the_stack_keeps_but_the_box_and_the_image(existing: adopt.Clients) -> None:
    found = adopt.find(existing, COMPARTMENT)

    assert set(found) == set(adoption.ADOPTABLE)
    assert found == {
        adoption.VCN: f'ocid1.vcn.oc1..{settings.NAME}-vcn-available',
        adoption.GATEWAY: f'ocid1.internetgateway.oc1..{settings.NAME}-igw-available',
        adoption.SUBNET: f'ocid1.subnet.oc1..{settings.NAME}-subnet-available',
        # The id the bucket's importer takes, not an OCID.
        adoption.IMAGE_BUCKET: f'n/{NAMESPACE}/b/{settings.IMAGE_BUCKET}',
        adoption.ADDRESS: f'ocid1.publicip.oc1..{settings.NAME}-ip-available',
        adoption.DUMP_BUCKET: 'bucket-dumps',
    }
    # Every lookup is confined to the appliance's compartment.
    assert existing.network.list_vcns.asked[0]['args'] == (COMPARTMENT,)
    assert existing.network.list_public_ips.asked[0]['compartment_id'] == COMPARTMENT


def test_adopt_reads_each_id_the_component_imports(existing: adopt.Clients) -> None:
    # What `adopt` writes is what the component reads: the configuration's
    # object names each resource the way `adoption.read` takes it.
    assert adoption.read(adopt.find(existing, COMPARTMENT)) == adopt.find(existing, COMPARTMENT)


@pytest.mark.parametrize('left_out', adoption.ADOPTABLE)
def test_a_partial_adoption_is_refused_naming_what_is_missing(left_out: str) -> None:
    # A name left out is a resource the program would create beside the one
    # that exists: a second VCN, or a second reserved address the gate cannot
    # see, since a create has no refreshed state to read.
    partial = {name: f'an-id-for-{name}' for name in adoption.ADOPTABLE if name != left_out}

    with pytest.raises(adoption.Refused, match=f'no id for {left_out}:'):
        _ = adoption.read(partial)


def test_the_image_and_the_box_are_never_adopted() -> None:
    # The engine refuses to replace a resource whose declaration still
    # carries an import id, and an imported image differs from its
    # declaration; an imported box would record its keys in the clear.
    assert 'image' not in adoption.ADOPTABLE
    with pytest.raises(adoption.Refused, match="names 'image'"):
        _ = adoption.read({**dict.fromkeys(adoption.ADOPTABLE, 'an-id'), 'image': 'ocid1.image.oc1..fcos'})


def test_two_live_holders_of_a_name_are_refused_naming_both(existing: adopt.Clients) -> None:
    existing.network.list_subnets.pages = [
        [
            _resource('subnet', f'{settings.NAME}-subnet'),
            _resource('subnet', f'{settings.NAME}-subnet', state='UPDATING'),
        ]
    ]

    with pytest.raises(
        adopt.Refused, match=rf'2 live resources carry the name {settings.NAME}-subnet \(.*-available, .*-updating\)'
    ):
        _ = adopt.find(existing, COMPARTMENT)


def test_a_name_nothing_live_carries_is_refused(existing: adopt.Clients) -> None:
    existing.network.list_internet_gateways.pages = [
        [_resource('internetgateway', f'{settings.NAME}-igw', state='TERMINATED')]
    ]

    with pytest.raises(adopt.Refused, match=f'no live internet gateway carries the name {settings.NAME}-igw'):
        _ = adopt.find(existing, COMPARTMENT)


def test_a_reservation_at_another_address_is_refused_naming_both(existing: adopt.Clients) -> None:
    # The address every bundle and the certificate name is recorded, not
    # looked up: a reservation elsewhere is a decision for the repository.
    existing.network.list_public_ips.pages = [[_resource('publicip', f'{settings.NAME}-ip', ip_address=ELSEWHERE)]]

    with pytest.raises(adopt.Refused, match=f'{ELSEWHERE}.*{settings.ADDRESS}'):
        _ = adopt.find(existing, COMPARTMENT)


def _initialized(checkout: Path) -> pulumi_config.Stack:
    path = checkout / stack_environment.CHECKPOINTS / '.pulumi' / 'stacks' / 'kluster-py'
    path.mkdir(parents=True)
    _ = (path / f'{derived.STATE_BACKEND_STACK}.json').write_text('{}')
    return adopt.stack(checkout)


def test_adopt_writes_the_ids_in_the_clear_under_one_key(existing: adopt.Clients, tmp_path: Path) -> None:
    runner = RecordedPulumi(stacks=[derived.STATE_BACKEND_STACK])
    configuration = pulumi_config.Stack(
        name=derived.STATE_BACKEND_STACK,
        directory=_initialized(tmp_path).directory,
        environment=pulumi_config.BackendEnvironment(operator=lambda: 'an-operator-passphrase'),
        run=runner,
    )

    ids = adopt.adopt(configuration, existing, COMPARTMENT)

    assert json.loads(runner.config[adoption.KEY]) == ids
    # Ids authorize nothing, so they are plain: a reviewer reads them in the
    # committed file.
    assert not [args for args in runner.invocations if '--secret' in args]


def test_adopt_is_refused_before_the_stack_has_a_checkpoint(tmp_path: Path) -> None:
    with pytest.raises(adopt.Refused, match=f'operator-stack {derived.STATE_BACKEND_STACK} pulumi stack init'):
        _ = adopt.stack(tmp_path)


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
    two members that over-approximation adds are named at the tuple.
    """
    raised = _raised(_closure(cli.__name__))
    assert raised, 'the walk found no raise of a repository exception, so it walked nothing'

    uncaught = {cls.__name__: sorted(where) for cls, where in raised.items() if not issubclass(cls, cli.REFUSALS)}
    assert not uncaught, f'raised on a path main reaches and not in cli.REFUSALS: {uncaught}'

    unraised = [member.__name__ for member in cli.REFUSALS if not any(issubclass(cls, member) for cls in raised)]
    assert not unraised, f'in cli.REFUSALS and raised nowhere main reaches: {unraised}'
