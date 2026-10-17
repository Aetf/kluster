"""What `conftest` closes and watches holds for fixtures of every scope, not only for the cases.

A module- or session-scoped fixture is set up before the first case that asks
for it and runs between cases, outside any case's own fixtures, and a module
is collected before any fixture at all. So the closed secret store, the
closed file layer and the pickler watch are each held here in child runs of
small modules under this directory's `conftest`: what collection, a fixture
of each scope and a case meet and leave, in a process whose order is the
child's own.

The secret store is held without the machine's own ever being reached. The
child's `keyring` resolves, wherever it is asked to, to a stand-in named by
`PYTHON_KEYRING_BACKEND`, which leaves a file behind when it is built; the
store being closed means the stand-in is never built at all.

The file layer is held without the real one ever being resolved. The child's
running checkout is one the parent case made under its `tmp_path`, with a copy
of the package and a decoy in every slot, and the child checks that it imported
that copy before anything else runs; the layer being closed means no decoy
comes back to any reader and none is changed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from kluster.lib import bundle, stack_environment
from kluster.lib import workstation as lib_workstation
from kluster.scripts.credentials import masters, workstation

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ROOT = Path(__file__).parent.parent

#: The child's configuration: none of `pyproject.toml`'s, so that it runs its
#: cases in order in one process whatever the parent's options are.
CONFIGURATION = '[pytest]\n'

#: A stop-loss on each child run, below the per-case bound.
TIMEOUT = 50

#: What the child's `keyring` resolves to in place of the machine's own store:
#: a backend that records being built, and refuses everything after.
MACHINE_STORE = """
from pathlib import Path

import keyring.backends.fail


class Machine(keyring.backends.fail.Keyring):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        Path(__file__).with_name('resolved').write_text('the machine store was resolved')
"""

#: The children that read the store from a module's fixture: set up between
#: two cases, after the first case's own fixtures have come and gone, and set
#: up by the module's first case, before any fixture narrower than the session
#: has run.
STORE_CHILDREN = {
    'between-cases': """
import keyring
import keyring.backends.fail
import pytest


@pytest.fixture(scope='module')
def store() -> object:
    return keyring.get_keyring()


def test_a_case_comes_and_goes() -> None:
    pass


def test_a_module_fixture_meets_the_closed_store(store: object) -> None:
    assert type(store) is keyring.backends.fail.Keyring, type(store)
""",
    'first-case': """
import keyring
import keyring.backends.fail
import pytest


@pytest.fixture(scope='module')
def store() -> object:
    return keyring.get_keyring()


def test_a_module_fixture_meets_the_closed_store(store: object) -> None:
    assert type(store) is keyring.backends.fail.Keyring, type(store)
""",
}

#: Children whose fixtures and cases change the pickler, each with what its
#: run must name and must not name.
PICKLER_CHILDREN: dict[str, tuple[str, list[str], list[str]]] = {
    # A module fixture that leaves the pickler changed at its set-up, before
    # any case of the module has begun.
    'module-leaks-at-set-up': (
        """
import pickle

import pytest


@pytest.fixture(scope='module')
def serializes_and_leaks() -> None:
    pickle._Pickler.left_by_a_module_fixture = staticmethod(lambda: None)


def test_a_case_takes_the_fixture(serializes_and_leaks: None) -> None:
    pass
""",
        ["module-scoped fixture 'serializes_and_leaks' left the pickler changed", 'left_by_a_module_fixture'],
        [],
    ),
    # A module fixture that patches for its life and restores at its
    # teardown: nothing is left.
    'module-patches-for-its-life': (
        """
import pickle

import pytest


@pytest.fixture(scope='module')
def patched_for_its_life():
    original = pickle._Pickler.dump
    pickle._Pickler.dump = lambda self, obj: original(self, obj)
    yield
    pickle._Pickler.dump = original


def test_a_case_takes_the_fixture(patched_for_its_life: None) -> None:
    pass
""",
        [],
        ['left the pickler changed'],
    ),
    # Two cases that each leave one attribute changed: each is named.
    'two-cases-leak-one-attribute': (
        """
import pickle


def test_the_first_leaks() -> None:
    pickle._Pickler.left_twice = staticmethod(lambda: None)


def test_the_second_leaks() -> None:
    pickle._Pickler.left_twice = staticmethod(lambda: None)
""",
        ['test_the_first_leaks', 'test_the_second_leaks', 'the case left the pickler changed'],
        [],
    ),
    # A session fixture first set up inside a module fixture's life, leaking
    # at its set-up: the session fixture is named, and the module one is not.
    'session-leaks-inside-a-module-life': (
        """
import pickle

import pytest


@pytest.fixture(scope='module')
def module_wide() -> None:
    pass


@pytest.fixture(scope='session')
def session_leaks() -> None:
    pickle._Pickler.left_by_a_session_fixture = staticmethod(lambda: None)


def test_the_module_begins(module_wide: None) -> None:
    pass


def test_the_session_fixture_arrives(module_wide: None, session_leaks: None) -> None:
    pass
""",
        ["session-scoped fixture 'session_leaks' left the pickler changed"],
        ["'module_wide' left"],
    ),
}


def _child(
    directory: Path, module: str, *, env: Mapping[str, str] | None = None, path: Sequence[Path] = ()
) -> subprocess.CompletedProcess[str]:
    """Run `module` as the only test file of a child pytest under this directory's `conftest`.

    The child's `keyring` resolves, wherever it is asked to, to the stand-in
    `MACHINE_STORE`, with its configuration and data directories under
    `directory`: so no child of this module can reach the machine's own
    store, whatever the harness does, and `_resolved` says whether one tried.
    `env` is added to the child's environment, and `path` goes ahead of the
    rest of its `PYTHONPATH`, which is how a child is given a package of the
    case's own to import.
    """
    (directory / 'test_child.py').write_text(module)
    (directory / 'pytest.ini').write_text(CONFIGURATION)
    (directory / 'machine_store.py').write_text(MACHINE_STORE)
    return subprocess.run(
        [
            sys.executable,
            '-m',
            'pytest',
            '-c',
            str(directory / 'pytest.ini'),
            '-p',
            'conftest',
            '-p',
            'no:cacheprovider',
            '-q',
            str(directory / 'test_child.py'),
        ],
        cwd=directory,
        env={
            **os.environ,
            'PYTHONPATH': os.pathsep.join((*map(str, path), str(ROOT / 'tests'), str(directory))),
            'PYTHON_KEYRING_BACKEND': 'machine_store.Machine',
            'XDG_CONFIG_HOME': str(directory / 'config'),
            'XDG_DATA_HOME': str(directory / 'data'),
            **(env or {}),
        },
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


def _resolved(directory: Path) -> bool:
    """Whether the child built the stand-in for the machine's store."""
    return (directory / 'resolved').exists()


@pytest.mark.parametrize('child', STORE_CHILDREN)
def test_a_module_fixture_meets_the_closed_store_and_the_machines_is_never_resolved(child: str, tmp_path: Path) -> None:
    run = _child(tmp_path, STORE_CHILDREN[child])

    assert run.returncode == 0, run.stdout + run.stderr
    assert not _resolved(tmp_path), 'the child resolved the machine store'


@pytest.mark.parametrize('child', PICKLER_CHILDREN)
def test_a_pickler_change_is_named_on_what_made_it_and_on_nothing_else(child: str, tmp_path: Path) -> None:
    module, named, unnamed = PICKLER_CHILDREN[child]

    run = _child(tmp_path, module)

    assert run.returncode == (1 if named else 0), run.stdout + run.stderr
    for text in named:
        assert text in run.stdout, (text, run.stdout)
    for text in unnamed:
        assert text not in run.stdout, (text, run.stdout)
    assert not _resolved(tmp_path), 'the child resolved the machine store'


#: The child that reads and writes the running checkout's file layer by every
#: route a suite has: at collection, from a module's fixture, from cases,
#: through a process a case starts, through `credential_files`, and inside
#: `file_layer.live()`. Its running checkout is `DECOY_CHECKOUT`, whose every
#: slot holds a decoy naming itself.
FILE_LAYER_CHILD = """
import os
import subprocess
from pathlib import Path

import file_layer
import kluster
import pytest

from kluster.lib import stack_environment
from kluster.lib import workstation as lib_workstation
from kluster.scripts.credentials import masters, workstation
from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore

CHECKOUT = Path(os.environ['DECOY_CHECKOUT']).resolve()
DECOYS = CHECKOUT / '.credentials'

# Before anything resolves a slot: a package imported from anywhere but the
# made checkout would make every case below a statement about another one.
assert Path(kluster.__file__).resolve().is_relative_to(CHECKOUT), kluster.__file__

COLLECTED = workstation.bundle_dir()

B2_KEY = masters.ROOTS[masters.B2].field(masters.B2_KEY)


def _held() -> dict[str, dict[str, str | None]]:
    return {member: masters.stored(root) for member, root in masters.ROOTS.items()}


@pytest.fixture(scope='module')
def held_from_a_module_fixture() -> dict[str, dict[str, str | None]]:
    return _held()


def test_a_slot_resolved_at_collection_is_not_in_the_decoy_layer() -> None:
    assert not COLLECTED.resolve().is_relative_to(DECOYS), COLLECTED


def test_a_module_fixture_finds_no_root(held_from_a_module_fixture: dict[str, dict[str, str | None]]) -> None:
    assert all(layer is None for fields in held_from_a_module_fixture.values() for layer in fields.values())


def test_the_roots_the_kit_and_the_operator_passphrase_are_absent() -> None:
    assert all(layer is None for fields in _held().values() for layer in fields.values())
    with pytest.raises(KdbxError):
        KdbxStore.from_env()
    with pytest.raises(stack_environment.NoOperatorPassphrase):
        stack_environment.operator_passphrase(lib_workstation.repo_root(), ask=lambda _question: None)


def test_a_write_into_the_layer_is_refused() -> None:
    with pytest.raises(OSError):
        workstation.write(workstation.root_path(B2_KEY.file), 'written by the child')


def test_a_process_a_case_starts_follows_no_path_into_the_layer() -> None:
    run = subprocess.run(['sh', '-c', 'cat "$PGSSLKEY"'], capture_output=True, text=True, timeout=10, check=False)
    assert 'decoy' not in run.stdout, run.stdout


def test_a_case_that_asks_for_credential_files_has_a_layer_of_its_own(credential_files: Path) -> None:
    slot = workstation.write(workstation.root_path(B2_KEY.file), 'the case its own')
    assert slot.is_relative_to(credential_files)
    assert masters.stored(masters.ROOTS[masters.B2])[masters.B2_KEY] == masters.FILE


def test_the_next_case_finds_the_layer_closed_again() -> None:
    assert all(layer is None for fields in _held().values() for layer in fields.values())


def test_inside_live_the_decoys_answer() -> None:
    with file_layer.live():
        assert masters.stored(masters.ROOTS[masters.B2])[masters.B2_KEY] == masters.FILE
        assert os.environ.get('KLUSTER_KDBX') == str(DECOYS / workstation.KIT)
    assert 'KLUSTER_KDBX' not in os.environ
"""


def _tree(root: Path) -> dict[Path, bytes]:
    """Every file under `root`, and what it holds."""
    return {path.relative_to(root): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


def test_the_running_checkouts_file_layer_is_closed_to_every_reader(tmp_path: Path) -> None:
    """No reader in a child run gets a decoy back from the running checkout's layer, and none is changed.

    The child's running checkout is one this case made: a copy of the
    package beside an empty `mise.toml`, and a decoy in each slot -- every
    field of every account root, the kit, the operator passphrase and the
    client bundle's key -- with `PGSSLKEY` and `KLUSTER_KDBX` naming theirs,
    as mise renders them. Every path here is under `tmp_path`, so even a run
    with the closure gone reads decoys and nothing else.
    """
    checkout = tmp_path / 'checkout'
    _ = shutil.copytree(
        ROOT / 'src' / 'kluster', checkout / 'src' / 'kluster', ignore=shutil.ignore_patterns('__pycache__')
    )
    (checkout / 'mise.toml').write_text('')
    layer = lib_workstation.slots(checkout)
    key = layer / stack_environment.BUNDLE_SLOT / bundle.KEY_FILE
    kit = layer / workstation.KIT
    decoys = [
        *(layer / workstation.ROOTS / field.file for root in masters.ROOTS.values() for field in root.fields),
        kit,
        layer / stack_environment.OPERATOR_PASSPHRASE_SLOT,
        key,
    ]
    for decoy in decoys:
        decoy.parent.mkdir(parents=True, exist_ok=True)
        _ = decoy.write_text(f'decoy {decoy.name}\n')
    planted = _tree(checkout)
    work = tmp_path / 'child'
    work.mkdir()

    run = _child(
        work,
        FILE_LAYER_CHILD,
        env={
            'DECOY_CHECKOUT': str(checkout),
            bundle.KEY_ENV: str(key),
            'KLUSTER_KDBX': str(kit),
            'PYTHONDONTWRITEBYTECODE': '1',
        },
        path=[checkout / 'src'],
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert _tree(checkout) == planted
    assert not _resolved(work), 'the child resolved the machine store'
