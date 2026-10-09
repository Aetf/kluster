"""What `conftest` closes and watches holds for fixtures of every scope, not only for the cases.

A module- or session-scoped fixture is set up before the first case that asks
for it and runs between cases, outside any case's own fixtures. So the closed
secret store and the pickler watch are each held here in child runs of small
modules under this directory's `conftest`: what a fixture of each scope meets
and leaves, and what a case leaves, in a process whose order is the child's
own.

The secret store is held without the machine's own ever being reached. The
child's `keyring` resolves, wherever it is asked to, to a stand-in named by
`PYTHON_KEYRING_BACKEND`, which leaves a file behind when it is built; the
store being closed means the stand-in is never built at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def _child(directory: Path, module: str) -> subprocess.CompletedProcess[str]:
    """Run `module` as the only test file of a child pytest under this directory's `conftest`.

    The child's `keyring` resolves, wherever it is asked to, to the stand-in
    `MACHINE_STORE`, with its configuration and data directories under
    `directory`: so no child of this module can reach the machine's own
    store, whatever the harness does, and `_resolved` says whether one tried.
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
            'PYTHONPATH': os.pathsep.join((str(ROOT / 'tests'), str(directory))),
            'PYTHON_KEYRING_BACKEND': 'machine_store.Machine',
            'XDG_CONFIG_HOME': str(directory / 'config'),
            'XDG_DATA_HOME': str(directory / 'data'),
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
